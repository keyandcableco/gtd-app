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
LATER, LATER2 = ((date.today() + timedelta(days=k)).isoformat() for k in (13, 14))


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

    def test_afternoon_between_morning_and_night(self):
        self.put(D0, [{"section": "attention", "note_id": self.a}])
        noon = {"headline": "Shops close at 6", "items": [{"section": "step", "note_id": self.b}]}
        self.assertEqual(self.c.put(f"/api/today/{D0}/afternoon", json=noon).status_code, 200)
        res = self.c.get("/api/today").get_json()
        self.assertEqual((res["slot"], res["label"], res["headline"]), ("afternoon", "This afternoon", "Shops close at 6"))
        self.c.put(f"/api/today/{D0}/night", json={"items": [{"section": "quick", "note_id": self.a}]})
        self.assertEqual(self.c.get("/api/today").get_json()["slot"], "night")
        self.put(D1, [{"section": "quick", "note_id": self.b}])  # yesterday's morning is older than all three
        self.assertEqual(self.c.get("/api/today").get_json()["slot"], "night")
        self.assertEqual(self.c.get(f"/api/today?day={D0}").get_json()["headline"], "Twelve days, Greg.")
        later = (date.today() + timedelta(days=2)).isoformat()
        r = self.c.post(f"/api/today/{D0}/afternoon/defer", json={"note_id": self.b, "on_day": later})
        self.assertEqual(r.status_code, 200)  # the afternoon list takes Later too
        self.assertEqual(r.get_json()["list"]["slot"], "afternoon")
        self.assertEqual([n["on_day"] for n in self.c.get("/api/notes?done=all").get_json() if n["id"] == self.b], [later])

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


class Defer(unittest.TestCase):
    """Later: an item put off to a day, and another note in its place."""

    def setUp(self):
        self.c = gtd.app.test_client()
        db = gtd.connect()
        for t in ("today_items", "today_days", "note_tags", "notes"):
            db.execute(f"DELETE FROM {t}")
        db.commit()
        db.close()
        self.a = self.note("Send the DR-1", what=["Business"], when_tag="1-Now")
        self.b = self.note("Pick the mailing address", what=["Plates"], when_tag="2-Next")
        self.c.put(f"/api/today/{D0}", json={"headline": "Greg.", "items": [
            {"section": "attention", "note_id": self.a, "why": "due Nov 18"},
            {"section": "step", "note_id": self.b, "why": "CAN-SPAM"}]})

    def note(self, body, **kw):
        r = self.c.post("/api/notes", json={"body": body, **kw})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["id"]

    def defer(self, nid, on_day=LATER, slot="", **kw):
        return self.c.post(f"/api/today/{D0}{slot}/defer", json={"note_id": nid, "on_day": on_day, **kw})

    def items(self, slot=""):
        res = self.c.get(f"/api/today?day={D0}" + ("&slot=night" if slot else "")).get_json()
        return [(s["key"], i["id"], i["why"]) for s in res["sections"] for i in s["items"]]

    def test_dated_and_replaced_in_place(self):
        later = self.note("Find out why Google can't index the site", what=["Website"], when_tag="2-Next",
                          note="step 1 of 5: Get found in search (cdc560c1)")
        r = self.defer(self.b)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["stand_in"]["id"], later)
        self.assertEqual(self.items(), [("attention", self.a, "due Nov 18"),
                                        ("step", later, "step 1 of 5: Get found in search (cdc560c1)")])
        b = next(n for n in self.c.get("/api/notes?done=all").get_json() if n["id"] == self.b)
        self.assertEqual(b["on_day"], LATER)

    def test_what_it_passes_over(self):
        self.c.put(f"/api/notes/{self.note('Ticked', when_tag='1-Now')}", json={"done": True})
        self.note("Dated past the list", when_tag="1-Now", on_day="2099-01-01")
        self.note("Someday", when_tag="5-Someday")
        self.note("Later", when_tag="4-Later")
        self.note("No When")
        self.note("Get found in search", when_tag="1-Now", note="project: 5 steps, in order in Details")
        self.note("Step two", when_tag="1-Now", note="step 2 of 5: Get found in search (cdc560c1)")
        self.note("Step one, a Later one", when_tag="4-Later", note="step 1 of 5: Get found in search (cdc560c1)")
        self.note("Gated", when_tag="1-Now", note="after the Wi-Fi rename (545180ae)")
        self.note("Waiting", when_tag="1-Now", note="waits on the boot-switch bench test")
        self.note("Steps behind", when_tag="1-Now", note="later step of: Release the joystick harp")
        r = self.defer(self.b)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.get_json()["stand_in"])
        self.assertEqual(self.items(), [("attention", self.a, "due Nov 18")])  # just one shorter

    def test_a_new_project_first_then_when_then_oldest(self):
        same = self.note("Another business one", what=["Business"], when_tag="1-Now")
        old = self.note("Old PCB one", what=["PCBs"], when_tag="3-Soon")
        newer = self.note("Newer PCB one", what=["PCBs"], when_tag="3-Soon")
        self.assertEqual(self.defer(self.b).get_json()["stand_in"]["id"], old)  # a Soon of a new project beats a Now
        self.assertEqual(self.defer(old).get_json()["stand_in"]["id"], newer)
        self.assertEqual(self.defer(newer).get_json()["stand_in"]["id"], same)  # none new left: shares Business

    def test_untagged_isnt_a_new_project(self):
        self.note("Old untagged", when_tag="1-Now")
        new = self.note("PCB one", what=["PCBs"], when_tag="3-Soon")
        self.assertEqual(self.defer(self.b).get_json()["stand_in"]["id"], new)

    def test_a_deadline_stand_in_goes_under_steps(self):
        spare = self.note("Spare", what=["PCBs"], when_tag="2-Next")
        self.defer(self.a)
        self.assertEqual(self.items(), [("step", spare, ""), ("step", self.b, "CAN-SPAM")])
        mine = self.note("Claude's pick")
        r = self.defer(spare, **{"with": mine, "section": "quick"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.items()[1:], [("quick", mine, "")])
        self.assertEqual(self.defer(mine, **{"with": spare, "section": "later"}).status_code, 400)

    def test_a_second_copy_goes_too(self):
        self.c.put(f"/api/today/{D0}", json={"headline": "Greg.", "items": [
            {"section": "attention", "note_id": self.a}, {"section": "quick", "note_id": self.a}]})
        self.assertEqual(self.defer(self.a).status_code, 200)
        self.assertEqual(self.items(), [("step", self.b, "")])  # in the first's place; the copy just goes

    def test_night_skips_places(self):
        self.c.put(f"/api/today/{D0}/night", json={"headline": "Quiet.", "items": [
            {"section": "step", "note_id": self.b}]})
        self.c.put(f"/api/notes/{self.a}", json={"done": True})  # else it fits tonight too
        self.note("Buy screws", what=["Repairs"], when_tag="1-Now", where=["HomeDepot"])
        desk = self.note("Draft the quote", what=["Repairs"], when_tag="2-Next")
        r = self.defer(self.b, slot="/night")
        self.assertEqual(r.get_json()["stand_in"]["id"], desk)
        self.assertEqual(self.items("night"), [("step", desk, "")])
        self.assertEqual(self.items()[1][1], self.b)  # the morning's list keeps it

    def test_with_names_the_stand_in(self):
        mine = self.note("Claude's pick", when_tag="4-Later", note="its own line")
        r = self.defer(self.b, **{"with": mine, "title": "Do Claude's pick", "why": "fits tonight"})
        self.assertEqual(r.status_code, 200, r.get_json())
        res = r.get_json()["list"]
        item = res["sections"][1]["items"][0]
        self.assertEqual((item["id"], item["title"], item["why"]), (mine, "Do Claude's pick", "fits tonight"))
        other = self.note("Another pick")
        self.c.post(f"/api/today/{D0}/defer", json={"note_id": mine, "on_day": LATER2, "with": other})
        self.assertEqual(self.items()[1][1:], (other, ""))  # no why sent: its note line, here none

    def test_refusals_change_nothing(self):
        spare = self.note("Spare", when_tag="1-Now")
        ticked = self.note("Ticked")
        self.c.put(f"/api/notes/{ticked}", json={"done": True})
        for nid, kw in ((self.b, {"on_day": D0}), (self.b, {"on_day": D1}), (self.b, {"on_day": "Dec 20"}),
                        (self.b, {"on_day": ""}), (spare, {}), ("nope", {}),
                        (self.b, {"with": self.a}), (self.b, {"with": ticked}), (self.b, {"with": "nope"}),
                        (self.b, {"with": 7})):
            r = self.c.post(f"/api/today/{D0}/defer", json={"note_id": nid, **{"on_day": LATER, **kw}})
            self.assertEqual(r.status_code, 400, (nid, kw))
        self.assertEqual(self.defer(self.b, slot="/evening").status_code, 400)
        self.c.put(f"/api/notes/{self.a}", json={"done": True})
        self.assertEqual(self.defer(self.a).status_code, 400)  # ticked off already
        self.c.put(f"/api/notes/{self.a}", json={"done": False})
        self.assertEqual(self.items(), [("attention", self.a, "due Nov 18"), ("step", self.b, "CAN-SPAM")])
        self.assertIsNone(next(n for n in self.c.get("/api/notes").get_json() if n["id"] == self.b)["on_day"])


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
