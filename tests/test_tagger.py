"""Haiku tagging (tagger.py) and the routes that use it, with Haiku stubbed out.

Run from the repo folder:  .venv/bin/python -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
_tmp = tempfile.TemporaryDirectory()
os.environ["GTD_DB"] = os.path.join(_tmp.name, "test.db")
os.environ.pop("ANTHROPIC_API_KEY", None)

import app as gtd  # noqa: E402
import tagger  # noqa: E402

WHEN = gtd.WHEN_TAGS
TAGS = {"who": ["Kids", "Sruti"], "what": ["Cars", "Home Maintenance", "Workshop"], "where": ["Home", "HomeDepot"]}


def answer(**kw):
    a = {"body": "", "note": "", "when": "", "who": [], "what": [], "where": [], "new_tags": []}
    a.update(kw)
    return a


class ToFields(unittest.TestCase):
    def test_keeps_only_tags_and_whens_that_exist(self):
        f = tagger.to_fields(answer(body="  Clean   the gutters ", when="2-Next", who=["sruti", "Bob"],
                                    what=["Home Maintenance", "home maintenance"], where=["Garage"]),
                             "clean the gutters", WHEN, TAGS)
        self.assertEqual(f["body"], "Clean the gutters")
        self.assertEqual(f["when_tag"], "2-Next")
        self.assertEqual(f["who"], ["Sruti"])
        self.assertEqual(f["what"], ["Home Maintenance"])
        self.assertEqual(f["where"], [])
        self.assertEqual(tagger.to_fields(answer(when="6-Never"), "x", WHEN, TAGS)["when_tag"], None)

    def test_new_tags_never_repeat_existing_ones(self):
        f = tagger.to_fields(answer(body="x", new_tags=[{"type": "who", "name": "Dad"},
                                                        {"type": "who", "name": "dad"},
                                                        {"type": "where", "name": "homedepot"},
                                                        {"type": "how", "name": "Fast"},
                                                        {"type": "where", "name": "  "}]), "x", WHEN, TAGS)
        self.assertEqual(f["new_tags"], [{"type": "who", "name": "Dad"}])

    def test_empty_body_keeps_what_was_said(self):
        self.assertEqual(tagger.to_fields(answer(), "call Mom", WHEN, TAGS)["body"], "call Mom")


class Schema(unittest.TestCase):
    def test_tag_lists_are_enums_and_an_empty_type_is_left_out(self):
        s = tagger.output_schema(WHEN, {"who": [], "what": ["Cars"], "where": ["Home"]})
        self.assertNotIn("who", s["properties"])
        self.assertEqual(s["properties"]["what"]["items"]["enum"], ["Cars"])
        self.assertEqual(s["properties"]["when"]["enum"], [*WHEN, ""])
        self.assertEqual(sorted(s["required"]), sorted(s["properties"]))
        self.assertIn("- who: (none yet)", tagger.system_prompt(WHEN, {"who": [], "what": ["Cars"]}))

    def test_read_without_a_key_raises(self):
        with self.assertRaises(RuntimeError):
            tagger.read("anything", WHEN, TAGS)


class Routes(unittest.TestCase):
    def setUp(self):
        if os.path.exists(os.environ["GTD_DB"]):
            os.remove(os.environ["GTD_DB"])
        gtd.init_db()
        self.c = gtd.app.test_client()
        for t, names in TAGS.items():  # the tags exist, as they would after earlier notes
            for n in names:
                self.c.post("/api/notes", json={"body": "seed", t: [n]})

    def notes(self):
        return [n for n in self.c.get("/api/notes").get_json() if n["body"] != "seed"]

    def test_capture_without_a_key_uses_the_spoken_tag_rules(self):
        r = self.c.post("/api/capture", data="order flex PCBs tag with Now and Workshop and Bob",
                        content_type="text/plain")
        out = r.get_json()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(out["reader"], "rules")
        self.assertEqual((out["body"], out["when_tag"], out["what"]), ("order flex PCBs", "1-Now", ["Workshop"]))
        self.assertEqual(out["note"], "unmatched tags: bob")

    def test_capture_with_haiku_saves_its_reading(self):
        fields = tagger.to_fields(answer(body="Pick up wood screws", note="for the shed", when="2-Next",
                                         who=["Kids"], where=["HomeDepot"],
                                         new_tags=[{"type": "who", "name": "Dad"}]), "", WHEN, TAGS)
        with mock.patch.object(tagger, "available", return_value=True), \
                mock.patch.object(tagger, "read", return_value=fields) as read:
            out = self.c.post("/api/capture", json={"text": "pick up wood screws for the shed this week"}).get_json()
        self.assertEqual(read.call_args.args[2], TAGS)  # Haiku is given the tracker's own tags
        self.assertEqual(out["reader"], "haiku")
        self.assertEqual(out["note"], "for the shed; maybe tag: who Dad")
        self.assertEqual(out["summary"], "Pick up wood screws [2-Next, Kids, HomeDepot]")
        saved = self.notes()[0]
        self.assertEqual((saved["when_tag"], saved["who"], saved["where"]), ("2-Next", ["Kids"], ["HomeDepot"]))
        self.assertNotIn("Dad", self.c.get("/api/tags").get_json()["who"])  # never made into a tag

    def test_capture_falls_back_to_the_rules_when_haiku_fails(self):
        with mock.patch.object(tagger, "available", return_value=True), \
                mock.patch.object(tagger, "read", side_effect=RuntimeError("APIConnectionError")):
            out = self.c.post("/api/capture", data="mow the lawn tag with next", content_type="text/plain").get_json()
        self.assertEqual((out["reader"], out["body"], out["when_tag"]), ("rules", "mow the lawn", "2-Next"))
        self.assertEqual(len(self.notes()), 1)

    def test_suggest_fills_the_form_without_saving(self):
        fields = tagger.to_fields(answer(body="Call Mom", new_tags=[{"type": "who", "name": "Mom"}]), "", WHEN, TAGS)
        with mock.patch.object(tagger, "read", return_value=fields):
            out = self.c.post("/api/suggest", json={"text": "call mom"}).get_json()
        self.assertEqual((out["body"], out["new_tags"]), ("Call Mom", [{"type": "who", "name": "Mom"}]))
        self.assertEqual(self.notes(), [])

    def test_suggest_says_why_it_couldnt(self):
        r = self.c.post("/api/suggest", json={"text": "call mom"})
        self.assertEqual((r.status_code, r.get_json()["error"]), (503, "no Anthropic API key set"))
        self.assertEqual(self.c.post("/api/suggest", json={"text": "  "}).status_code, 400)

    def test_tag_it_button_shows_only_with_a_key(self):
        self.assertNotIn(b'id="tagBtn"', self.c.get("/").data)
        with mock.patch.object(tagger, "available", return_value=True):
            self.assertIn(b'id="tagBtn"', self.c.get("/").data)


if __name__ == "__main__":
    unittest.main()
