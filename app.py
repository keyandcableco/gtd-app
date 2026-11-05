import sqlite3, uuid, os, re, logging, threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template, g, send_file

import tagger
import transcribe

log = logging.getLogger("gtd")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("GTD_DB", os.path.join(APP_DIR, "gtd.db"))
DUMPS_DIR = os.environ.get("GTD_DUMPS", os.path.join(APP_DIR, "dumps"))  # brain-dump recordings

WHEN_TAGS = ["1-Now", "2-Next", "3-Soon", "4-Later", "5-Someday"]
TAG_TYPES = ["who", "what", "where"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # the Pi's nginx allows the same


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            body TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            when_tag TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(type, name)
        );
        CREATE TABLE IF NOT EXISTS note_tags (
            note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (note_id, tag_id)
        );
        -- Every capture as it came in (typed, or a recording and its
        -- transcript), and the notes it was filed as.
        -- status: queued / transcribing (recordings), filed, failed.
        CREATE TABLE IF NOT EXISTS dumps (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            audio TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            reader TEXT NOT NULL DEFAULT '',
            note_ids TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        );
        """
    )
    # migrate older DBs that predate the note column
    cols = [r[1] for r in db.execute("PRAGMA table_info(notes)").fetchall()]
    if "note" not in cols:
        db.execute("ALTER TABLE notes ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    db.commit()
    db.close()


def get_or_create_tag(db, ttype, name):
    name = name.strip()
    if not name or ttype not in TAG_TYPES:
        return None
    row = db.execute(
        "SELECT id FROM tags WHERE type=? AND name=?", (ttype, name)
    ).fetchone()
    if row:
        return row["id"]
    cur = db.execute("INSERT INTO tags(type, name) VALUES(?,?)", (ttype, name))
    return cur.lastrowid


def serialize_note(db, row):
    tags = db.execute(
        """SELECT t.type, t.name FROM tags t
           JOIN note_tags nt ON nt.tag_id = t.id
           WHERE nt.note_id = ? ORDER BY t.type, t.name""",
        (row["id"],),
    ).fetchall()
    grouped = {t: [] for t in TAG_TYPES}
    for tg in tags:
        grouped[tg["type"]].append(tg["name"])
    return {
        "id": row["id"],
        "body": row["body"],
        "note": row["note"],
        "when_tag": row["when_tag"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        **grouped,
    }


def set_note_tags(db, note_id, who, what, where):
    db.execute("DELETE FROM note_tags WHERE note_id=?", (note_id,))
    for ttype, names in (("who", who), ("what", what), ("where", where)):
        for name in names or []:
            tid = get_or_create_tag(db, ttype, name)
            if tid:
                db.execute(
                    "INSERT OR IGNORE INTO note_tags(note_id, tag_id) VALUES(?,?)",
                    (note_id, tid),
                )


def all_tags(db):
    out = {t: [] for t in TAG_TYPES}
    for r in db.execute("SELECT type, name FROM tags ORDER BY type, name").fetchall():
        out[r["type"]].append(r["name"])
    return out


@app.route("/")
def index():
    return render_template("index.html", when_tags=WHEN_TAGS, haiku=tagger.available())


@app.route("/api/tags")
def api_tags():
    return jsonify(all_tags(get_db()))


@app.route("/api/notes")
def api_notes():
    db = get_db()
    clauses = ["deleted = 0"]
    params = []
    when = request.args.get("when")
    if when:
        clauses.append("when_tag = ?")
        params.append(when)

    # tag filters: repeatable ?who=, ?what=, ?where=
    tag_filters = []
    for ttype in TAG_TYPES:
        for name in request.args.getlist(ttype):
            tag_filters.append((ttype, name))
    base = "SELECT n.* FROM notes n"
    for i, (ttype, name) in enumerate(tag_filters):
        base += (
            f" JOIN note_tags nt{i} ON nt{i}.note_id = n.id"
            f" JOIN tags t{i} ON t{i}.id = nt{i}.tag_id AND t{i}.type=? AND t{i}.name=?"
        )
        params = [ttype, name] + params
    q = request.args.get("q")
    if q:  # the note line too, so a project's name finds its steps
        clauses.append("(n.body LIKE ? OR n.note LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]

    sql = base + " WHERE " + " AND ".join(clauses)
    # sort: When ascending (1..5), then newest first
    sql += " ORDER BY CASE WHEN n.when_tag IS NULL THEN 1 ELSE 0 END, n.when_tag, n.updated_at DESC"
    rows = db.execute(sql, params).fetchall()
    return jsonify([serialize_note(db, r) for r in rows])


@app.route("/api/notes", methods=["POST"])
def create_note():
    db = get_db()
    data = request.get_json(force=True)
    nid = data.get("id") or str(uuid.uuid4())
    ts = now_iso()
    db.execute(
        "INSERT INTO notes(id, body, note, when_tag, created_at, updated_at, deleted) VALUES(?,?,?,?,?,?,0)",
        (nid, data.get("body", "").strip(), data.get("note", "").strip(),
         data.get("when_tag"), ts, ts),
    )
    set_note_tags(db, nid, data.get("who"), data.get("what"), data.get("where"))
    db.commit()
    row = db.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    return jsonify(serialize_note(db, row)), 201


# ---- voice capture: "<note text> tag with Now and Workshop" ----
TAG_PHRASE_RE = re.compile(
    r"^(?P<body>.*)\btag(?:s|ged)?\s+(?:with\s+)?(?P<tags>.+?)[\s.,!?]*$",
    re.I | re.S,
)


def _when_key(w):
    """'1-Now' -> 'now' so spoken 'now' matches."""
    return re.sub(r"^\d+\s*-\s*", "", w).strip().lower()


def parse_capture(db, text):
    """Split spoken text into (body, when_tag, {who,what,where}, unmatched).

    Tag names are matched against the existing When values and tags
    (longest first, so names containing 'and' still work). If nothing in the
    trailing 'tag with ...' phrase matches, the whole text is kept as the body.
    """
    text = text.strip()
    m = TAG_PHRASE_RE.match(text)
    if not m:
        return text, None, {t: [] for t in TAG_TYPES}, []
    body, rest = m.group("body").strip(" ,.;:-"), m.group("tags").lower()

    cands = [("when", _when_key(w), w) for w in WHEN_TAGS]
    for r in db.execute("SELECT type, name FROM tags").fetchall():
        cands.append((r["type"], r["name"].strip().lower(), r["name"]))
    cands = [c for c in cands if c[1]]
    cands.sort(key=lambda c: len(c[1]), reverse=True)

    when, tags = None, {t: [] for t in TAG_TYPES}
    for kind, key, name in cands:
        pat = r"(?<!\w)" + re.escape(key) + r"(?!\w)"
        if re.search(pat, rest):
            rest = re.sub(pat, " ", rest, count=1)
            if kind == "when":
                when = when or name
            elif name not in tags[kind]:
                tags[kind].append(name)
    leftover = [w for w in re.split(r"[\s,]+", rest) if w and w != "and"]
    matched_any = when or any(tags.values())
    if not matched_any:
        return text, None, {t: [] for t in TAG_TYPES}, []
    return body, when, tags, leftover


def note_line(n, maybe=True):
    """The note line for one of Haiku's notes: its details, then what it's
    part of (a project's steps say so; ones Haiku thought up say that too),
    then any tags it suggests that don't exist yet."""
    parts = [n["note"]]
    if n["kind"] == "project":
        parts.append("project")
    elif n["kind"] == "step":
        parts.append("Haiku's step" + (f" for: {n['project']}" if n["project"] else ""))
    elif n["project"]:
        parts.append(f"part of: {n['project']}")
    if maybe and n["new_tags"]:
        parts.append("maybe tag: " + ", ".join(f"{t['type']} {t['name']}" for t in n["new_tags"]))
    return "; ".join(p for p in parts if p)


def read_notes(db, text, wait):
    """([{body, note, when_tag, who, what, where}], reader): Haiku's notes in
    the text, or the text as one note by the "tag with ..." rules when Haiku
    isn't set up, fails or finds nothing to file."""
    try:
        if tagger.available():
            found = tagger.read(text, WHEN_TAGS, all_tags(db), wait=wait)
            if found:
                return [dict(n, note=note_line(n)) for n in found], "haiku"
            log.warning("Haiku found no notes in a capture; filing it as one")
    except Exception as e:  # never lose a capture: the rules take over
        log.warning("Haiku couldn't read a capture, using the rules: %s", e)
    body, when, tags, leftover = parse_capture(db, text)
    note = ("unmatched tags: " + " ".join(leftover)) if leftover else ""
    return [{"body": body, "note": note, "when_tag": when, **tags}], "rules"


def file_notes(db, text, wait):
    """Read the text into notes and save them: (note ids, reader). Haiku is
    asked before anything is written, so no write waits on it."""
    notes, reader = read_notes(db, text, wait)
    ids, ts = [], now_iso()
    for n in notes:
        nid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO notes(id, body, note, when_tag, created_at, updated_at, deleted) VALUES(?,?,?,?,?,?,0)",
            (nid, n["body"], n["note"], n["when_tag"], ts, ts),
        )
        set_note_tags(db, nid, n["who"], n["what"], n["where"])
        ids.append(nid)
    db.commit()
    return ids, reader


def summarize(notes):
    """A short line for a Tasker flash."""
    if len(notes) == 1:
        n = notes[0]
        labels = ([n["when_tag"]] if n["when_tag"] else []) + [x for t in TAG_TYPES for x in n[t]]
        return f"{n['body']} [{', '.join(labels)}]" if labels else n["body"]
    line = f"{len(notes)} notes: " + "; ".join(n["body"] for n in notes)
    return line if len(line) <= 200 else line[:199] + "…"


def add_dump(db, status, audio="", text="", reader="", note_ids=()):
    did, ts = str(uuid.uuid4()), now_iso()
    db.execute(
        "INSERT INTO dumps(id, created_at, updated_at, status, audio, text, reader, note_ids) VALUES(?,?,?,?,?,?,?,?)",
        (did, ts, ts, status, audio, text, reader, ",".join(note_ids)),
    )
    db.commit()
    return did


def set_dump(db, did, **cols):
    cols["updated_at"] = now_iso()
    db.execute(f"UPDATE dumps SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), did))
    db.commit()


@app.route("/api/capture", methods=["POST"])
def api_capture():
    """Accepts JSON {"text": ...} or a raw text/plain body (easiest from Tasker).
    One to-do or a whole brain dump: Haiku files it as however many notes it
    holds. The first note's fields are also at the top level, as before."""
    data = request.get_json(silent=True)
    text = data.get("text", "") if isinstance(data, dict) else request.get_data(as_text=True)
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        return jsonify({"error": "empty"}), 400
    db = get_db()
    ids, reader = file_notes(db, text, wait=35)
    did = add_dump(db, "filed", text=text, reader=reader, note_ids=ids)
    notes = [serialize_note(db, db.execute("SELECT * FROM notes WHERE id=?", (i,)).fetchone()) for i in ids]
    return jsonify({**notes[0], "notes": notes, "summary": summarize(notes), "reader": reader, "dump": did}), 201


# ---- brain-dump recordings: saved now, written out by Whisper and filed in the background ----
AUDIO_EXTS = {"audio/mp4": ".m4a", "audio/m4a": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
              "audio/3gpp": ".3gp", "audio/amr": ".amr", "audio/ogg": ".ogg", "audio/opus": ".opus",
              "audio/webm": ".webm", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3",
              "audio/flac": ".flac"}


@app.route("/api/dump", methods=["POST"])
def api_dump():
    """A recording, as the raw body (Tasker's HTTP Request with a file to
    send) or a multipart upload. Answers at once; the notes come once
    Whisper has written it out (a few minutes per minute of talk on the Pi)."""
    upload = next(iter(request.files.values()), None)
    if upload:
        data, name, ctype = upload.read(), upload.filename or "", upload.mimetype
    else:
        data, name, ctype = request.get_data(), "", request.mimetype
    if not data:
        return jsonify({"error": "empty"}), 400
    ext = os.path.splitext(name)[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        ext = AUDIO_EXTS.get(ctype, ".audio")
    os.makedirs(DUMPS_DIR, exist_ok=True)
    audio = f"{uuid.uuid4()}{ext}"
    with open(os.path.join(DUMPS_DIR, audio), "wb") as f:
        f.write(data)
    did = add_dump(get_db(), "queued", audio=audio)
    _wake.set()
    summary = ("Got it. Filing it once it's written out." if _worker_started else
               "Saved, but speech-to-text is off here, so it's waiting.")
    return jsonify({"id": did, "summary": summary}), 202


@app.route("/api/dumps")
def api_dumps():
    """The latest captures, newest first, with the notes each became (those
    not deleted since)."""
    db = get_db()
    rows = db.execute("SELECT * FROM dumps ORDER BY created_at DESC LIMIT ?",
                      (min(int(request.args.get("n", 5)), 50),)).fetchall()
    out = []
    for r in rows:
        notes = []
        for nid in filter(None, r["note_ids"].split(",")):
            n = db.execute("SELECT id, body, when_tag FROM notes WHERE id=? AND deleted=0", (nid,)).fetchone()
            if n:
                notes.append(dict(n))
        out.append({"id": r["id"], "created_at": r["created_at"], "status": r["status"],
                    "recording": bool(r["audio"]), "text": r["text"], "reader": r["reader"],
                    "error": r["error"], "notes": notes, "made": len(r["note_ids"].split(",")) if r["note_ids"] else 0})
    return jsonify({"dumps": out, "speech_to_text": _worker_started})


def process_next_dump(db):
    """Write out and file the oldest waiting recording. False when none is waiting."""
    row = db.execute("SELECT * FROM dumps WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
    if row is None:
        return False
    did = row["id"]
    set_dump(db, did, status="transcribing", error="")
    try:
        text = transcribe.transcribe_file(os.path.join(DUMPS_DIR, row["audio"]),
                                          transcribe.prompt_for(all_tags(db)))
        if not text:
            set_dump(db, did, status="failed", error="Whisper heard no words in the recording")
            return True
        set_dump(db, did, text=text)
        ids, reader = file_notes(db, text, wait=180)  # nobody's waiting: give Haiku time
        set_dump(db, did, status="filed", reader=reader, note_ids=",".join(ids))
    except Exception as e:
        log.exception("brain dump %s failed", did)
        set_dump(db, did, status="failed", error=str(e)[:300] or type(e).__name__)
    return True


_wake = threading.Event()
_worker_started = False


def _dump_worker():
    while True:
        try:
            db = connect()
            try:
                busy = process_next_dump(db)
            finally:
                db.close()
            if not busy:
                transcribe.unload()  # give the memory back until the next recording
                _wake.wait(timeout=300)
                _wake.clear()
        except Exception:  # never let the worker die: log, back off, carry on
            log.exception("brain-dump worker")
            _wake.wait(timeout=30)
            _wake.clear()


def start_worker():
    """Start the recording worker once (not without Whisper: recordings wait)."""
    global _worker_started
    if _worker_started or not transcribe.available():
        return
    db = connect()
    db.execute("UPDATE dumps SET status='queued' WHERE status='transcribing'")  # cut off by a restart
    db.commit()
    db.close()
    threading.Thread(target=_dump_worker, name="brain-dump-worker", daemon=True).start()
    _worker_started = True


@app.route("/api/suggest", methods=["POST"])
def api_suggest():
    """The page's Tag it button: Haiku's notes in {"text": ...}, not saved."""
    data = request.get_json(silent=True)
    text = data.get("text") if isinstance(data, dict) else ""
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        return jsonify({"error": "empty"}), 400
    try:
        found = tagger.read(text, WHEN_TAGS, all_tags(get_db()))
        return jsonify({"notes": [dict(n, note=note_line(n, maybe=False)) for n in found]})
    except Exception as e:
        log.warning("Haiku couldn't read the text box: %s", e)
        return jsonify({"error": str(e)[:300] or type(e).__name__}), 503


@app.route("/api/notes/<nid>", methods=["PUT"])
def update_note(nid):
    db = get_db()
    data = request.get_json(force=True)
    row = db.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    db.execute(
        "UPDATE notes SET body=?, note=?, when_tag=?, updated_at=? WHERE id=?",
        (data.get("body", row["body"]).strip(), data.get("note", row["note"]).strip(),
         data.get("when_tag"), now_iso(), nid),
    )
    set_note_tags(db, nid, data.get("who"), data.get("what"), data.get("where"))
    db.commit()
    row = db.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    return jsonify(serialize_note(db, row))


@app.route("/api/notes/<nid>", methods=["DELETE"])
def delete_note(nid):
    db = get_db()
    db.execute(
        "UPDATE notes SET deleted=1, updated_at=? WHERE id=?", (now_iso(), nid)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/export")
def export_db():
    return send_file(DB_PATH, as_attachment=True, download_name="gtd.db")


# Under gunicorn too (it imports app:app), so a new table reaches the live database.
init_db()
start_worker()

if __name__ == "__main__":
    port = int(os.environ.get("GTD_PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
