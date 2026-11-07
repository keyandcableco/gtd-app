"""Today: the day's short list and the night's (/api/today, /today), and a note's day
(on_day, GTD's tickler) that puts it on the list.

Run from the repo folder:  .venv/bin/python -m unittest discover tests
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
# Its own scratch database, never one GTD_DB points at in the shell; run with
# the other tests, it shares the one the first of them set up.
if "app" not in sys.modules:
    _tmp = tempfile.TemporaryDirectory()
    os.environ["GTD_DB"] = os.path.join(_tmp.name, "test.db")
    os.environ["GTD_DUMPS"] = os.path.join(_tmp.name, "dumps")
    os.environ["WHISPER_MODEL"] = "off"
    os.environ.pop("ANTHROPIC_API_KEY", None)

import app as gtd  # noqa: E402

D0 = date.today().isoformat()
D1 = (date.today() - timedelta(days=1)).isoformat()


class Today(unittest.TestCase):
    def setUp(self):
        self.c = gtd.app.test_client()
        db = gtd.connect()
        db.execute("DELETE FROM today_items")
        db.execute("DELETE FROM today_days")
        db.commit()
        db.close()
        self.a = self.note("Sign and send the DR-1 package", what=["Business"], when_tag="1-Now")
        self.b = self.note("Count the AY-3-8910 chips")

    def note(self, body, **kw):
        r = self.c.post("/api/notes", json={"body": body, **kw})
        self.assertEqual(r.status_code, 201)
        return r.get_json()["id"]

    def put(self, day, items, headline="Twelve days, Greg."):
        return self.c.put(f"/api/today/{day}", json={"headline": headline, "items": items})

    def test_empty(self):
        self.assertEqual(self.c.get("/api/today").get_json()["day"], None)

    def test_set_and_read_in_order(self):
        r = self.put(D0, [
            {"section": "quick", "note_id": self.b, "why": "red toolbox"},
            {"section": "attention", "note_id": self.a, "title": "Send the DR-1", "why": "due Nov 18"},
        ])
        self.assertEqual(r.status_code, 200)
        res = self.c.get("/api/today").get_json()
        self.assertEqual(res["day"], D0)
        self.assertEqual(res["headline"], "Twelve days, Greg.")
        self.assertEqual([s["key"] for s in res["sections"]], ["attention", "quick"])  # section order, not send order
        first = res["sections"][0]["items"][0]
        self.assertEqual((first["title"], first["why"], first["what"]), ("Send the DR-1", "due Nov 18", ["Business"]))
        self.assertEqual(res["sections"][1]["items"][0]["title"], "Count the AY-3-8910 chips")  # the note's text

    def test_newest_day_and_replace(self):
        self.put(D1, [{"section": "quick", "note_id": self.b}])
        self.put(D0, [{"section": "attention", "note_id": self.a}])
        self.put(D0, [{"section": "step", "note_id": self.b}], headline="Again")
        res = self.c.get("/api/today").get_json()
        self.assertEqual((res["day"], res["headline"]), (D0, "Again"))
        self.assertEqual([(s["key"], len(s["items"])) for s in res["sections"]], [("step", 1)])
        old = self.c.get(f"/api/today?day={D1}").get_json()
        self.assertEqual(old["sections"][0]["items"][0]["id"], self.b)

    def test_tick_shows_and_deleted_drops_out(self):
        self.put(D0, [{"section": "attention", "note_id": self.a}, {"section": "quick", "note_id": self.b}])
        self.c.put(f"/api/notes/{self.a}", json={"done": True})
        self.c.delete(f"/api/notes/{self.b}")
        res = self.c.get("/api/today").get_json()
        self.assertEqual(len(res["sections"]), 1)
        self.assertTrue(res["sections"][0]["items"][0]["done_at"])

    def test_refusals_leave_the_list_alone(self):
        self.put(D0, [{"section": "attention", "note_id": self.a}])
        far = (date.today() + timedelta(days=90)).isoformat()
        ok = [{"section": "quick", "note_id": self.b}]
        for day, items in (("Nov 6", ok), ("2026-02-31", ok), (far, ok), ("\u0662\u0660\u0662\u0666-11-06", ok),
                           (D0, []),
                           (D0, [{"section": "later", "note_id": self.a}]),
                           (D0, [{"section": "quick", "note_id": "nope"}]),
                           (D0, ["x"])):
            self.assertEqual(self.put(day, items).status_code, 400, (day, items))
        self.assertEqual(self.c.put(f"/api/today/{D0}", json={"headline": "x"}).status_code, 400)
        res = self.c.get("/api/today").get_json()
        self.assertEqual(res["sections"][0]["items"][0]["id"], self.a)

    def test_night_beside_the_day(self):
        self.put(D0, [{"section": "attention", "note_id": self.a}])
        r = self.c.put(f"/api/today/{D0}/night", json={"headline": "Quiet hours",
                                                      "items": [{"section": "cleanup", "note_id": self.b}]})
        self.assertEqual(r.status_code, 200)
        res = self.c.get("/api/today").get_json()  # the night's is the newest
        self.assertEqual((res["day"], res["slot"], res["label"]), (D0, "night", "Tonight"))
        self.assertEqual([s["label"] for s in res["sections"]], ["Clean-up and housekeeping"])
        day = self.c.get(f"/api/today?day={D0}").get_json()  # its morning's, untouched
        self.assertEqual((day["slot"], day["headline"], day["sections"][0]["items"][0]["id"]),
                         ("day", "Twelve days, Greg.", self.a))
        self.assertEqual(self.c.get(f"/api/today?day={D0}&slot=night").get_json()["headline"], "Quiet hours")
        self.put(D1, [{"section": "quick", "note_id": self.b}])
        self.assertEqual(self.c.get("/api/today?slot=day").get_json()["day"], D0)
        self.assertEqual(self.c.put(f"/api/today/{D0}/evening", json={"items": [
            {"section": "quick", "note_id": self.b}]}).status_code, 400)
        self.assertEqual(self.c.get("/api/today?slot=evening").status_code, 400)

    def test_old_database_gets_the_slot(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.path.join(tmp.name, "old.db")
        db = sqlite3.connect(old)
        db.executescript("""
            CREATE TABLE notes (id TEXT PRIMARY KEY, body TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                when_tag TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE today_days (day TEXT PRIMARY KEY, headline TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
            CREATE TABLE today_items (day TEXT NOT NULL REFERENCES today_days(day) ON DELETE CASCADE,
                pos INTEGER NOT NULL, section TEXT NOT NULL, note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT '', why TEXT NOT NULL DEFAULT '', PRIMARY KEY (day, pos));
            INSERT INTO notes(id, body, created_at, updated_at) VALUES ('n1', 'Send the DR-1', 'x', 'x');
            INSERT INTO today_days VALUES ('2026-11-06', 'Twelve days', 'x');
            INSERT INTO today_items VALUES ('2026-11-06', 0, 'attention', 'n1', '', 'due Nov 18');
        """)
        db.commit()
        db.close()
        with mock.patch.object(gtd, "DB_PATH", old):
            gtd.init_db()
            gtd.init_db()  # a second worker finds it done
            db = gtd.connect()
            res = gtd.today_list(db)
            db.close()
        self.assertEqual((res["day"], res["slot"], res["headline"]), ("2026-11-06", "day", "Twelve days"))
        self.assertEqual(res["sections"][0]["items"][0]["why"], "due Nov 18")

    def test_page(self):
        r = self.c.get("/today")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Today", r.data)


class OnDay(unittest.TestCase):
    def setUp(self):
        self.c = gtd.app.test_client()

    def note(self, **kw):
        r = self.c.post("/api/notes", json={"body": "John picks up the Juno", **kw})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()

    def get(self, nid):
        return next(n for n in self.c.get("/api/notes?done=all").get_json() if n["id"] == nid)

    def put(self, nid, **kw):
        return self.c.put(f"/api/notes/{nid}", json=kw)

    def test_none_by_default(self):
        self.assertIsNone(self.note()["on_day"])

    def test_set_and_listed(self):
        n = self.note(on_day=" 2026-11-09 ")
        self.assertEqual(n["on_day"], "2026-11-09")
        self.assertEqual(self.get(n["id"])["on_day"], "2026-11-09")  # the server hides nothing: gtd list does

    def test_page_edit_keeps_the_day(self):
        n = self.note(on_day="2026-11-09")
        r = self.put(n["id"], body="John picks up the Juno Monday", note="", details="", when_tag="2-Next",
                     who=["John"], what=[], where=[])  # what the page sends
        self.assertEqual(r.get_json()["on_day"], "2026-11-09")
        self.assertEqual(self.put(n["id"], done=True).get_json()["on_day"], "2026-11-09")

    def test_change_and_clear(self):
        n = self.note(on_day="2026-11-09")
        self.assertEqual(self.put(n["id"], on_day="2026-11-10").get_json()["on_day"], "2026-11-10")
        self.assertIsNone(self.put(n["id"], on_day="").get_json()["on_day"])
        self.put(n["id"], on_day="2026-11-10")
        self.assertIsNone(self.put(n["id"], on_day=None).get_json()["on_day"])

    def test_ref_again_updates_the_day(self):
        n = self.note(ref="test:juno", on_day="2026-11-09")
        r = self.c.post("/api/notes", json={"body": "John picks up the Juno", "ref": "test:juno", "on_day": "2026-11-11"})
        self.assertEqual((r.get_json()["id"], r.get_json()["on_day"]), (n["id"], "2026-11-11"))

    def test_refusals_change_nothing(self):
        n = self.note(on_day="2026-11-09")
        for bad in ("Mon Nov 9", "2026-02-31", "20261109", "2026-11-9", "٢٠٢٦-11-09",
                    20261109, ["2026-11-09"]):
            self.assertEqual(self.put(n["id"], on_day=bad, body="changed").status_code, 400, bad)
            self.assertEqual(self.c.post("/api/notes", json={"body": "x", "on_day": bad}).status_code, 400, bad)
        self.assertEqual((self.get(n["id"])["on_day"], self.get(n["id"])["body"]), ("2026-11-09", "John picks up the Juno"))

    def test_on_the_today_list(self):
        n = self.note(on_day=D0)
        self.c.put(f"/api/today/{D0}", json={"headline": "Juno day, Greg.",
                                            "items": [{"section": "attention", "note_id": n["id"]}]})
        item = self.c.get(f"/api/today?day={D0}").get_json()["sections"][0]["items"][0]
        self.assertEqual(item["on_day"], D0)

    def test_older_database_gets_the_column(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE notes (id TEXT PRIMARY KEY, body TEXT NOT NULL DEFAULT '', note TEXT NOT NULL"
                       " DEFAULT '', when_tag TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
                       " deleted INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT INTO notes(id, body, created_at, updated_at) VALUES('old', 'kept', 'x', 'x')")
            db.commit()
            db.close()
            was, gtd.DB_PATH = gtd.DB_PATH, path
            try:
                gtd.init_db()
            finally:
                gtd.DB_PATH = was
            db = sqlite3.connect(path)
            self.assertEqual(db.execute("SELECT body, on_day FROM notes").fetchone(), ("kept", None))
            db.close()


if __name__ == "__main__":
    unittest.main()
