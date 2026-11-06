"""Today: the day's short list (/api/today, /today).

Run from the repo folder:  .venv/bin/python -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

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

    def test_page(self):
        r = self.c.get("/today")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Today", r.data)


if __name__ == "__main__":
    unittest.main()
