import sqlite3, uuid, os, re, logging, threading, time
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
            error TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0
        );
        -- The day's short list (the 5 AM brief): notes picked from the
        -- tracker, in order, each with why it's on today's list. Sending a
        -- day again replaces its list.
        CREATE TABLE IF NOT EXISTS today_days (
            day TEXT PRIMARY KEY,
            headline TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS today_items (
            day TEXT NOT NULL REFERENCES today_days(day) ON DELETE CASCADE,
            pos INTEGER NOT NULL,
            section TEXT NOT NULL,
            note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT '',
            why TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (day, pos)
        );
        """
    )
    # migrate older DBs that predate a column
    cols = [r[1] for r in db.execute("PRAGMA table_info(notes)").fetchall()]
    for col, decl in (
        ("note", "TEXT NOT NULL DEFAULT ''"),
        # the long part, folded shut under the note (Claude's handoffs: files, commits, what to say when done)
        ("details", "TEXT NOT NULL DEFAULT ''"),
        ("done_at", "TEXT"),                       # ticked off; NULL while open
        ("source", "TEXT NOT NULL DEFAULT ''"),    # where it came from: "Claude: <session>", "Open Loops: ..."
        ("ref", "TEXT NOT NULL DEFAULT ''"),       # a sender's own id, so sending it again updates it
        ("on_day", "TEXT"),                        # the day it comes up (GTD's tickler), YYYY-MM-DD; NULL for none
    ):
        if col not in cols:
            try:
                db.execute(f"ALTER TABLE notes ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:  # another worker added it first
                pass
    db.execute("CREATE INDEX IF NOT EXISTS notes_ref ON notes(ref) WHERE ref != ''")
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
        "details": row["details"],
        "when_tag": row["when_tag"],
        "done_at": row["done_at"],
        "source": row["source"],
        "ref": row["ref"],
        "on_day": row["on_day"],
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
    done = request.args.get("done", "0")  # 0: open notes (the default), 1: done ones, all: both
    if done == "0":
        clauses.append("n.done_at IS NULL")
    elif done == "1":
        clauses.append("n.done_at IS NOT NULL")
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
    if q:  # the note line and details too, so a project's name finds its steps
        clauses.append("(n.body LIKE ? OR n.note LIKE ? OR n.details LIKE ?)")
        params += [f"%{q}%"] * 3
    source = request.args.get("source")  # a prefix: "Claude" finds every session's
    if source:
        clauses.append("n.source LIKE ? ESCAPE '\\'")
        params.append(source.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")

    sql = base + " WHERE " + " AND ".join(clauses)
    # sort: done ones newest first; open ones by When ascending (1..5), then newest first
    if done == "1":
        sql += " ORDER BY n.done_at DESC"
    else:
        sql += " ORDER BY CASE WHEN n.when_tag IS NULL THEN 1 ELSE 0 END, n.when_tag, n.updated_at DESC"
    rows = db.execute(sql, params).fetchall()
    return jsonify([serialize_note(db, r) for r in rows])


def _text(data, key, default=""):
    v = data.get(key, default)
    return v.strip() if isinstance(v, str) else default


class BadRequest(Exception):
    pass


@app.errorhandler(BadRequest)
def bad_request(e):
    return jsonify({"error": str(e)}), 400


def _when(data):
    """A When from the request: "2-Next", or just "next"; None for none
    (null or ""). Anything else is refused, never quietly cleared."""
    w = data.get("when_tag")
    if w is None or (isinstance(w, str) and not w.strip()):
        return None
    w = w.strip() if isinstance(w, str) else w
    found = w if w in WHEN_TAGS else next((x for x in WHEN_TAGS if isinstance(w, str) and _when_key(x) == w.lower()), None)
    if found is None:
        raise BadRequest(f"unknown When {w!r}: use one of {', '.join(WHEN_TAGS)}")
    return found


DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse_day(s):
    """The date in "YYYY-MM-DD", or None when it isn't a real one."""
    if not isinstance(s, str) or not DAY_RE.fullmatch(s):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _on_day(data):
    """A note's day from the request: "YYYY-MM-DD"; None for none (null or
    ""). Anything else is refused, never quietly cleared."""
    d = data.get("on_day")
    if d is None or (isinstance(d, str) and not d.strip()):
        return None
    d = d.strip() if isinstance(d, str) else d
    if parse_day(d) is None:
        raise BadRequest(f"on_day must be a real date, YYYY-MM-DD (or empty for none), not {d!r}")
    return d


def _tag_lists(data):
    for t in TAG_TYPES:
        v = data.get(t)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            raise BadRequest(f"{t} must be a list of tag names")


@app.route("/api/notes", methods=["POST"])
def create_note():
    """A new note. With a "ref" (the sender's own id for it, e.g. the
    importer's "openloops:rr-qs8-notes"), sending it again updates the
    note with that ref instead of adding a second one."""
    db = get_db()
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"error": "send a JSON object"}), 400
    _tag_lists(data)
    ref = _text(data, "ref")
    if ref:
        row = db.execute("SELECT id FROM notes WHERE ref=? AND deleted=0", (ref,)).fetchone()
        if row:
            return update_note(row["id"])
    nid = data.get("id") if isinstance(data.get("id"), str) and data.get("id") else str(uuid.uuid4())
    ts = now_iso()
    db.execute(
        "INSERT INTO notes(id, body, note, details, when_tag, on_day, source, ref, created_at, updated_at, deleted)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,0)",
        (nid, _text(data, "body"), _text(data, "note"), _text(data, "details"), _when(data), _on_day(data),
         _text(data, "source"), ref, ts, ts),
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


MAX_NOTES = 30       # one capture never files more; the rest stay in its words, and the dump says so
RETRY_SECONDS = 30   # between Haiku tries for a recording (nobody's waiting on it)


def read_notes(db, text, wait, tries=1):
    """([{body, note, when_tag, who, what, where}], reader): Haiku's notes in
    the text, or the text as one note by the "tag with ..." rules when Haiku
    isn't set up, fails every try or finds nothing to file."""
    for attempt in range(tries if tagger.available() else 0):
        try:
            found = tagger.read(text, WHEN_TAGS, all_tags(db), wait=wait)
            if found:
                return [dict(n, note=note_line(n)) for n in found], "haiku"
            log.warning("Haiku found no notes in a capture; filing it as one")
            break
        except Exception as e:  # never lose a capture: the rules take over
            log.warning("Haiku couldn't read a capture (try %d of %d): %s", attempt + 1, tries, e)
            if attempt + 1 < tries:
                time.sleep(RETRY_SECONDS)
    body, when, tags, leftover = parse_capture(db, text)
    note = ("unmatched tags: " + " ".join(leftover)) if leftover else ""
    return [{"body": body, "note": note, "when_tag": when, **tags}], "rules"


def file_notes(db, text, wait, tries=1, source="Capture"):
    """Read the text into notes and write them, not yet committed: (note ids,
    reader, how many were left out over MAX_NOTES). Haiku is asked before
    anything is written, so no write waits on it, and the caller commits the
    notes together with the dump that records them."""
    notes, reader = read_notes(db, text, wait, tries)
    ids, ts = [], now_iso()
    for n in notes[:MAX_NOTES]:
        nid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO notes(id, body, note, when_tag, source, created_at, updated_at, deleted)"
            " VALUES(?,?,?,?,?,?,?,0)",
            (nid, n["body"], n["note"], n["when_tag"], source, ts, ts),
        )
        set_note_tags(db, nid, n["who"], n["what"], n["where"])
        ids.append(nid)
    return ids, reader, len(notes) - len(ids)


def left_out(dropped, kept):
    return (f"Only the first {kept} of {kept + dropped} notes were filed; the rest are in the words above."
            if dropped else "")


def summarize(notes):
    """A short line for a Tasker flash."""
    if len(notes) == 1:
        n = notes[0]
        labels = ([n["when_tag"]] if n["when_tag"] else []) + [x for t in TAG_TYPES for x in n[t]]
        return f"{n['body']} [{', '.join(labels)}]" if labels else n["body"]
    line = f"{len(notes)} notes: " + "; ".join(n["body"] for n in notes)
    return line if len(line) <= 200 else line[:199] + "…"


def add_dump(db, status, audio="", text="", reader="", note_ids=(), error=""):
    """A dumps row, not yet committed."""
    did, ts = str(uuid.uuid4()), now_iso()
    db.execute(
        "INSERT INTO dumps(id, created_at, updated_at, status, audio, text, reader, note_ids, error)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (did, ts, ts, status, audio, text, reader, ",".join(note_ids), error),
    )
    return did


def set_dump(db, did, **cols):
    """Change a dumps row, not yet committed."""
    cols["updated_at"] = now_iso()
    db.execute(f"UPDATE dumps SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), did))


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
    ids, reader, dropped = file_notes(db, text, wait=35)
    did = add_dump(db, "filed", text=text, reader=reader, note_ids=ids, error=left_out(dropped, len(ids)))
    db.commit()
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
    # Only a multipart upload is parsed: touching request.files on any other
    # body (a mislabelled form type, say) would use it up.
    upload = next(iter(request.files.values()), None) if request.mimetype == "multipart/form-data" else None
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
    db = get_db()
    did = add_dump(db, "queued", audio=audio)
    db.commit()
    _wake.set()
    summary = ("Got it. Filing it once it's written out." if _worker_started else
               "Saved, but speech-to-text is off here, so it's waiting.")
    return jsonify({"id": did, "summary": summary}), 202


@app.route("/api/dumps")
def api_dumps():
    """The latest captures, newest first, with the notes each became (those
    not deleted since)."""
    db = get_db()
    try:
        n = max(1, min(int(request.args.get("n", 5)), 50))
    except ValueError:
        n = 5
    rows = db.execute("SELECT * FROM dumps ORDER BY created_at DESC LIMIT ?", (n,)).fetchall()
    pending = db.execute("SELECT COUNT(*) FROM dumps WHERE status IN ('queued', 'transcribing')").fetchone()[0]
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
    return jsonify({"dumps": out, "pending": pending, "speech_to_text": _worker_started})


@app.route("/api/dumps/<did>/retry", methods=["POST"])
def retry_dump(did):
    """Try a failed recording again (after a failed model download, say)."""
    db = get_db()
    cur = db.execute("UPDATE dumps SET status='queued', error='', attempts=0, updated_at=?"
                     " WHERE id=? AND status='failed' AND audio != ''", (now_iso(), did))
    db.commit()
    if not cur.rowcount:
        return jsonify({"error": "not a failed recording"}), 404
    _wake.set()
    return jsonify({"ok": True})


def process_next_dump(db):
    """Write out and file the oldest waiting recording. False when none is waiting.
    The transcript is kept as soon as it's made, so a recording cut off while
    Haiku reads it isn't written out again; its notes and its "filed" land in
    one commit, so they can't be filed twice or half-filed."""
    row = db.execute("SELECT * FROM dumps WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
    if row is None:
        return False
    did = row["id"]
    set_dump(db, did, status="transcribing", error="", attempts=row["attempts"] + 1)
    db.commit()
    try:
        text = row["text"]
        if not text:
            text = transcribe.transcribe_file(os.path.join(DUMPS_DIR, row["audio"]),
                                              transcribe.prompt_for(all_tags(db)))
            if not text:
                set_dump(db, did, status="failed", error="Whisper heard no words in the recording")
                db.commit()
                return True
            set_dump(db, did, text=text)
            db.commit()
        # Nobody's waiting: give Haiku time, and a few tries before the one-note fallback.
        ids, reader, dropped = file_notes(db, text, wait=180, tries=3, source="Recording")
        set_dump(db, did, status="filed", reader=reader, note_ids=",".join(ids), error=left_out(dropped, len(ids)))
        db.commit()
    except Exception as e:
        db.rollback()  # no half-filed notes
        log.exception("brain dump %s failed", did)
        set_dump(db, did, status="failed", error=str(e)[:300] or type(e).__name__)
        db.commit()
    return True


def requeue_interrupted(db):
    """Recordings cut off mid-way by a restart go back in the queue, except one
    that's already been cut off twice: it may be what's crashing the app (out
    of memory, a bad file), so it waits for Try again."""
    db.execute("UPDATE dumps SET status='failed', error=? WHERE status='transcribing' AND attempts >= 2",
               ("Stopped twice while being written out (the app restarted or ran out of memory).",))
    db.execute("UPDATE dumps SET status='queued' WHERE status='transcribing'")
    db.commit()


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
    requeue_interrupted(db)
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
    """Change a note: only the fields sent change (the page sends all but
    on_day, so editing there keeps a note's day). "done": true ticks it off,
    false opens it again."""
    db = get_db()
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"error": "send a JSON object"}), 400
    row = db.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    _tag_lists(data)
    if "done" in data and not isinstance(data["done"], bool):
        raise BadRequest("done must be true or false")
    done_at = row["done_at"]
    if "done" in data:
        done_at = (done_at or now_iso()) if data["done"] else None
    db.execute(
        "UPDATE notes SET body=?, note=?, details=?, when_tag=?, on_day=?, source=?, done_at=?, updated_at=? WHERE id=?",
        (_text(data, "body", row["body"]), _text(data, "note", row["note"]),
         _text(data, "details", row["details"]),
         _when(data) if "when_tag" in data else row["when_tag"],
         _on_day(data) if "on_day" in data else row["on_day"],
         _text(data, "source", row["source"]), done_at, now_iso(), nid),
    )
    if any(t in data for t in TAG_TYPES):
        old = serialize_note(db, row)
        set_note_tags(db, nid, *(data[t] if t in data else old[t] for t in TAG_TYPES))
    db.commit()
    row = db.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    return jsonify(serialize_note(db, row))


# ---- Today: the day's short list, picked each morning ----
TODAY_SECTIONS = [("attention", "Needs attention"), ("step", "One step per project"), ("quick", "Ten-minute wins")]
DAY_RANGE = 31  # a list more than a month from today is a typo, and would shadow every real one


@app.route("/today")
def today_page():
    return render_template("today.html")


def today_list(db, day=None):
    """A day's list, or the newest one. Each item carries its note, ticks and
    all; a note deleted since drops out."""
    row = (db.execute("SELECT * FROM today_days WHERE day=?", (day,)).fetchone() if day else
           db.execute("SELECT * FROM today_days ORDER BY day DESC LIMIT 1").fetchone())
    if row is None:
        return {"day": None, "headline": "", "sections": []}
    sections = []
    for key, label in TODAY_SECTIONS:
        items = []
        for it in db.execute(
                "SELECT i.title, i.why, n.* FROM today_items i JOIN notes n ON n.id = i.note_id"
                " WHERE i.day=? AND i.section=? AND n.deleted=0 ORDER BY i.pos", (row["day"], key)):
            items.append({**serialize_note(db, it), "title": it["title"] or it["body"], "why": it["why"]})
        if items:
            sections.append({"key": key, "label": label, "items": items})
    return {"day": row["day"], "headline": row["headline"], "updated_at": row["updated_at"], "sections": sections}


@app.route("/api/today")
def api_today():
    """?day=YYYY-MM-DD for that day's list; the newest one without it."""
    return jsonify(today_list(get_db(), request.args.get("day")))


@app.route("/api/today/<day>", methods=["PUT"])
def set_today(day):
    """Replace a day's list: {"headline": ..., "items": [{"section": "attention"
    | "step" | "quick", "note_id": <a whole note id>, "title": short, optional,
    "why": one line}]}. Every item must be a note in the tracker."""
    when = parse_day(day)
    if when is None:
        raise BadRequest("day must be a real date, YYYY-MM-DD")
    if abs((when - datetime.now().date()).days) > DAY_RANGE:
        raise BadRequest(f"day must be within {DAY_RANGE} days of today")
    data = request.get_json(force=True)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or not data["items"]:
        raise BadRequest('send {"headline": ..., "items": [...]} with at least one item')
    db = get_db()
    keys = [k for k, _ in TODAY_SECTIONS]
    rows = []
    for i, it in enumerate(data["items"]):
        if not isinstance(it, dict):
            raise BadRequest(f"item {i + 1} isn't an object")
        if it.get("section") not in keys:
            raise BadRequest(f"item {i + 1}: section must be one of {', '.join(keys)}")
        nid = it.get("note_id")
        if not isinstance(nid, str) or not db.execute(
                "SELECT 1 FROM notes WHERE id=? AND deleted=0", (nid,)).fetchone():
            raise BadRequest(f"item {i + 1}: no note {nid!r} in the tracker")
        rows.append((day, i, it["section"], nid, _text(it, "title"), _text(it, "why")))
    db.execute("DELETE FROM today_items WHERE day=?", (day,))
    db.execute("INSERT INTO today_days(day, headline, updated_at) VALUES(?,?,?)"
               " ON CONFLICT(day) DO UPDATE SET headline=excluded.headline, updated_at=excluded.updated_at",
               (day, _text(data, "headline"), now_iso()))
    db.executemany("INSERT INTO today_items(day, pos, section, note_id, title, why) VALUES(?,?,?,?,?,?)", rows)
    db.commit()
    return jsonify(today_list(db, day))


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
