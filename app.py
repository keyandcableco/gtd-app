import sqlite3, uuid, os
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template, g, send_file

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("GTD_DB", os.path.join(APP_DIR, "gtd.db"))

WHEN_TAGS = ["1-Now", "2-Next", "3-Soon", "4-Later", "5-Someday"]
TAG_TYPES = ["who", "what", "where"]

app = Flask(__name__)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
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


@app.route("/")
def index():
    return render_template("index.html", when_tags=WHEN_TAGS)


@app.route("/api/tags")
def api_tags():
    db = get_db()
    rows = db.execute("SELECT type, name FROM tags ORDER BY type, name").fetchall()
    out = {t: [] for t in TAG_TYPES}
    for r in rows:
        out[r["type"]].append(r["name"])
    return jsonify(out)


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
    if q:
        clauses.append("n.body LIKE ?")
        params.append(f"%{q}%")

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


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("GTD_PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
