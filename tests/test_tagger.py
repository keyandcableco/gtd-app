"""Haiku tagging (tagger.py), brain dumps and the routes that use them, with
Haiku and Whisper stubbed out.

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
os.environ["GTD_DUMPS"] = os.path.join(_tmp.name, "dumps")
os.environ["WHISPER_MODEL"] = "off"  # no worker thread: tests run process_next_dump themselves
os.environ.pop("ANTHROPIC_API_KEY", None)

import app as gtd  # noqa: E402
import tagger  # noqa: E402
import transcribe  # noqa: E402

WHEN = gtd.WHEN_TAGS
TAGS = {"who": ["Kids", "Sruti"], "what": ["Cars", "Home Maintenance", "Workshop"], "where": ["Home", "HomeDepot"]}


def raw(**kw):
    """One note as Haiku answers it."""
    a = {"kind": "said", "body": "", "note": "", "when": "", "who": [], "what": [], "where": [],
         "new_tags": [], "project": ""}
    a.update(kw)
    return a


def notes(*raws, tags=TAGS):
    return tagger.to_notes({"notes": list(raws)}, WHEN, tags)


class ToNotes(unittest.TestCase):
    def test_keeps_only_tags_and_whens_that_exist(self):
        [n] = notes(raw(body="  Clean   the gutters ", when="2-Next", who=["sruti", "Bob"],
                        what=["Home Maintenance", "home maintenance"], where=["Garage"]))
        self.assertEqual(n["body"], "Clean the gutters")
        self.assertEqual(n["when_tag"], "2-Next")
        self.assertEqual((n["who"], n["what"], n["where"]), (["Sruti"], ["Home Maintenance"], []))
        self.assertIsNone(notes(raw(body="x", when="6-Never"))[0]["when_tag"])

    def test_an_exact_name_wins_over_one_that_differs_by_case(self):
        tags = {"where": ["Home", "home"]}
        self.assertEqual(notes(raw(body="x", where=["Home"]), tags=tags)[0]["where"], ["Home"])
        self.assertEqual(notes(raw(body="x", where=["home"]), tags=tags)[0]["where"], ["home"])

    def test_new_tags_never_repeat_existing_ones_of_their_type(self):
        [n] = notes(raw(body="x", new_tags=[{"type": "who", "name": "Dad"}, {"type": "who", "name": "dad"},
                                            {"type": "where", "name": "homedepot"}, {"type": "how", "name": "Fast"},
                                            {"type": "where", "name": "  "}, {"type": "who", "name": "Workshop"}]))
        self.assertEqual(n["new_tags"], [{"type": "who", "name": "Dad"}, {"type": "who", "name": "Workshop"}])

    def test_a_step_haiku_thought_up_is_never_now(self):
        step, said = notes(raw(kind="step", body="Sketch it", when="1-Now", project="Arcade console"),
                           raw(kind="said", body="Order parts", when="1-Now", project="Arcade console"))
        self.assertEqual((step["when_tag"], said["when_tag"]), ("2-Next", "1-Now"))

    def test_notes_without_text_are_dropped(self):
        self.assertEqual(notes(raw(body="  ")), [])
        self.assertEqual(notes(raw(kind="project", body="P", project="P"))[0]["project"], "")


class Schema(unittest.TestCase):
    def test_tag_lists_are_enums_and_an_empty_type_is_left_out(self):
        s = tagger.output_schema(WHEN, {"who": [], "what": ["Cars"], "where": ["Home"]})
        note = s["properties"]["notes"]["items"]
        self.assertNotIn("who", note["properties"])
        self.assertEqual(note["properties"]["what"]["items"]["enum"], ["Cars"])
        self.assertEqual(note["properties"]["when"]["enum"], [*WHEN, ""])
        self.assertEqual(sorted(note["required"]), sorted(note["properties"]))
        self.assertIn("- who: (none yet)", tagger.system_prompt(WHEN, {"who": [], "what": ["Cars"]}))

    def test_read_without_a_key_raises(self):
        with self.assertRaises(RuntimeError):
            tagger.read("anything", WHEN, TAGS)

    def test_read_gives_up_after_its_wait(self):
        def slow(*a):
            import time
            time.sleep(3)
        with mock.patch.object(tagger, "available", return_value=True), \
                mock.patch.object(tagger, "_ask_haiku", side_effect=slow), \
                mock.patch("concurrent.futures.Future.result",
                           side_effect=tagger.concurrent.futures.TimeoutError):
            with self.assertRaisesRegex(RuntimeError, "no answer from Haiku"):
                tagger.read("x", WHEN, TAGS, wait=1)


class NoteLine(unittest.TestCase):
    def test_says_what_a_note_is_part_of(self):
        def line(**kw):
            return gtd.note_line(notes(raw(body="x", **kw))[0])
        self.assertEqual(line(note="12 mm"), "12 mm")
        self.assertEqual(line(kind="project"), "project")
        self.assertEqual(line(kind="step", project="Arcade console"), "Haiku's step for: Arcade console")
        self.assertEqual(line(kind="said", project="Arcade console", new_tags=[{"type": "who", "name": "Dad"}]),
                         "part of: Arcade console; maybe tag: who Dad")


class Routes(unittest.TestCase):
    def setUp(self):
        if os.path.exists(os.environ["GTD_DB"]):
            os.remove(os.environ["GTD_DB"])
        __import__("shutil").rmtree(os.environ["GTD_DUMPS"], ignore_errors=True)
        gtd.init_db()
        self.c = gtd.app.test_client()
        for t, names in TAGS.items():  # the tags exist, as they would after earlier notes
            for n in names:
                self.c.post("/api/notes", json={"body": "seed", t: [n]})

    def notes(self, **q):
        return [n for n in self.c.get("/api/notes", query_string=q).get_json() if n["body"] != "seed"]

    def haiku(self, *raws, **kw):
        found = notes(*raws) if raws else []
        return mock.patch.multiple(tagger, available=mock.Mock(return_value=True),
                                   read=mock.Mock(return_value=found, **kw))

    def test_capture_without_a_key_uses_the_spoken_tag_rules(self):
        r = self.c.post("/api/capture", data="order flex PCBs tag with Now and Workshop and Bob",
                        content_type="text/plain")
        out = r.get_json()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(out["reader"], "rules")
        self.assertEqual((out["body"], out["when_tag"], out["what"]), ("order flex PCBs", "1-Now", ["Workshop"]))
        self.assertEqual(out["note"], "unmatched tags: bob")

    def test_a_brain_dump_is_filed_as_several_notes_and_its_words_are_kept(self):
        said = "clean the gutters this weekend and someday build a minichord arcade console, and ask Dad about it"
        with self.haiku(raw(body="Clean the gutters", when="2-Next", what=["Home Maintenance"], where=["Home"]),
                        raw(kind="project", body="Minichord arcade console", when="5-Someday"),
                        raw(kind="step", body="Sketch the layout", when="1-Now", project="Minichord arcade console"),
                        raw(body="Ask Dad about the console", project="Minichord arcade console",
                            new_tags=[{"type": "who", "name": "Dad"}])):
            out = self.c.post("/api/capture", data=said, content_type="text/plain").get_json()
        self.assertEqual(out["reader"], "haiku")
        self.assertEqual(len(out["notes"]), 4)
        self.assertTrue(out["summary"].startswith("4 notes: Clean the gutters; Minichord arcade console;"))
        self.assertEqual(out["body"], "Clean the gutters")  # the first note, at the top level as before
        by_body = {n["body"]: n for n in self.notes()}
        self.assertEqual(by_body["Sketch the layout"]["when_tag"], "2-Next")
        self.assertEqual(by_body["Sketch the layout"]["note"], "Haiku's step for: Minichord arcade console")
        self.assertEqual(by_body["Ask Dad about the console"]["note"],
                         "part of: Minichord arcade console; maybe tag: who Dad")
        self.assertNotIn("Dad", self.c.get("/api/tags").get_json()["who"])  # never made into a tag
        # the project's name finds its steps
        self.assertEqual(len(self.notes(q="arcade console")), 3)
        [dump] = self.c.get("/api/dumps").get_json()["dumps"]
        self.assertEqual((dump["text"], dump["status"], dump["made"], dump["recording"]), (said, "filed", 4, False))

    def test_capture_falls_back_to_the_rules_when_haiku_fails_or_finds_nothing(self):
        with self.haiku(side_effect=RuntimeError("APIConnectionError")):
            out = self.c.post("/api/capture", data="mow the lawn tag with next", content_type="text/plain").get_json()
        self.assertEqual((out["reader"], out["body"], out["when_tag"]), ("rules", "mow the lawn", "2-Next"))
        with self.haiku():
            out = self.c.post("/api/capture", data="hmm, nothing really", content_type="text/plain").get_json()
        self.assertEqual((out["reader"], out["body"]), ("rules", "hmm, nothing really"))
        self.assertEqual(len(self.notes()), 2)

    def test_capture_still_files_it_when_the_sdk_is_broken(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), \
                mock.patch.dict(sys.modules, {"anthropic": None}):  # import raises
            out = self.c.post("/api/capture", data="mow the lawn", content_type="text/plain").get_json()
            self.assertNotIn(b'id="tagBtn"', self.c.get("/").data)
        self.assertEqual((out["reader"], out["body"]), ("rules", "mow the lawn"))

    def test_a_recording_waits_then_is_written_out_and_filed(self):
        r = self.c.post("/api/dump", data=b"fake m4a bytes", content_type="audio/mp4")
        self.assertEqual(r.status_code, 202)
        self.assertIn("speech-to-text is off", r.get_json()["summary"])
        [dump] = self.c.get("/api/dumps").get_json()["dumps"]
        self.assertEqual((dump["status"], dump["recording"]), ("queued", True))
        [audio] = os.listdir(os.environ["GTD_DUMPS"])
        self.assertTrue(audio.endswith(".m4a"))

        db = gtd.connect()
        with mock.patch.object(transcribe, "transcribe_file", return_value="call Mom about Thanksgiving") as tf, \
                self.haiku(raw(body="Call Mom about Thanksgiving", when="2-Next")):
            self.assertTrue(gtd.process_next_dump(db))
            self.assertFalse(gtd.process_next_dump(db))  # nothing left waiting
        self.assertTrue(tf.call_args.args[0].endswith(audio))
        self.assertIn("Sruti", tf.call_args.args[1])  # tag names as Whisper's spelling hint
        [dump] = self.c.get("/api/dumps").get_json()["dumps"]
        self.assertEqual((dump["status"], dump["text"], [n["body"] for n in dump["notes"]]),
                         ("filed", "call Mom about Thanksgiving", ["Call Mom about Thanksgiving"]))

    def test_a_recording_that_cant_be_written_out_is_marked_failed(self):
        self.c.post("/api/dump", data={"audio": (__import__("io").BytesIO(b"x"), "dump.ogg")})
        db = gtd.connect()
        with mock.patch.object(transcribe, "transcribe_file", side_effect=ValueError("bad audio")):
            gtd.process_next_dump(db)
        self.c.post("/api/dump", data=b"y", content_type="application/octet-stream")
        with mock.patch.object(transcribe, "transcribe_file", return_value=""):
            gtd.process_next_dump(db)
        new, old = self.c.get("/api/dumps").get_json()["dumps"]
        self.assertEqual((old["status"], old["error"]), ("failed", "bad audio"))
        self.assertEqual((new["status"], new["error"]), ("failed", "Whisper heard no words in the recording"))
        self.assertEqual(sorted(os.path.splitext(a)[1] for a in os.listdir(os.environ["GTD_DUMPS"])),
                         [".audio", ".ogg"])
        self.assertEqual(self.c.post("/api/dump", data=b"").status_code, 400)

    def test_a_mislabelled_upload_is_still_kept(self):
        r = self.c.post("/api/dump", data=b"m4a bytes", content_type="application/x-www-form-urlencoded")
        self.assertEqual(r.status_code, 202)
        self.assertEqual(len(os.listdir(os.environ["GTD_DUMPS"])), 1)

    def test_a_long_dump_says_how_many_notes_were_left_out(self):
        with mock.patch.object(gtd, "MAX_NOTES", 3), self.haiku(*[raw(body=f"note {i}") for i in range(5)]):
            self.c.post("/api/capture", data="a long ramble", content_type="text/plain")
        [dump] = self.c.get("/api/dumps").get_json()["dumps"]
        self.assertEqual((dump["made"], len(self.notes())), (3, 3))
        self.assertEqual(dump["error"], "Only the first 3 of 5 notes were filed; the rest are in the words above.")

    def test_a_failure_mid_filing_files_nothing_and_can_be_tried_again(self):
        self.c.post("/api/dump", data=b"x", content_type="audio/mp4")
        db = gtd.connect()
        real = gtd.set_note_tags
        calls = []

        def second_one_breaks(*a):
            calls.append(1)
            if len(calls) == 2:
                raise sqlite3_error("disk I/O error")
            return real(*a)
        sqlite3_error = __import__("sqlite3").OperationalError
        with mock.patch.object(transcribe, "transcribe_file", return_value="a and b") as tf, \
                mock.patch.object(gtd, "set_note_tags", side_effect=second_one_breaks), \
                self.haiku(raw(body="A"), raw(body="B")):
            gtd.process_next_dump(db)
        [dump] = self.c.get("/api/dumps").get_json()["dumps"]
        self.assertEqual((dump["status"], dump["text"], self.notes()), ("failed", "a and b", []))

        self.assertEqual(self.c.post(f"/api/dumps/{dump['id']}/retry").status_code, 200)
        with mock.patch.object(transcribe, "transcribe_file") as tf2, self.haiku(raw(body="A"), raw(body="B")):
            gtd.process_next_dump(db)
        tf2.assert_not_called()  # the transcript was kept: not written out again
        self.assertEqual(sorted(n["body"] for n in self.notes()), ["A", "B"])
        self.assertEqual(self.c.post(f"/api/dumps/{dump['id']}/retry").status_code, 404)  # filed now

    def test_a_recording_gets_haiku_three_tries_before_the_one_note_fallback(self):
        self.c.post("/api/dump", data=b"x", content_type="audio/mp4")
        found = notes(raw(body="Clean the gutters"))
        with mock.patch.object(transcribe, "transcribe_file", return_value="clean the gutters"), \
                mock.patch.object(gtd.time, "sleep") as sleep, \
                mock.patch.multiple(tagger, available=mock.Mock(return_value=True),
                                    read=mock.Mock(side_effect=[RuntimeError("overloaded"), RuntimeError("overloaded"),
                                                                found])):
            gtd.process_next_dump(gtd.connect())
        self.assertEqual(sleep.call_count, 2)
        [dump] = self.c.get("/api/dumps").get_json()["dumps"]
        self.assertEqual((dump["status"], dump["reader"]), ("filed", "haiku"))

    def test_a_recording_cut_off_twice_waits_for_try_again(self):
        for _ in range(2):
            self.c.post("/api/dump", data=b"x", content_type="audio/mp4")
        db = gtd.connect()
        first, second = [r["id"] for r in db.execute("SELECT id FROM dumps ORDER BY created_at")]
        db.execute("UPDATE dumps SET status='transcribing', attempts=2 WHERE id=?", (first,))
        db.execute("UPDATE dumps SET status='transcribing', attempts=1 WHERE id=?", (second,))
        db.commit()
        gtd.requeue_interrupted(db)
        status = {r["id"]: r["status"] for r in db.execute("SELECT id, status FROM dumps")}
        self.assertEqual((status[first], status[second]), ("failed", "queued"))
        out = self.c.get("/api/dumps").get_json()
        self.assertEqual(out["pending"], 1)

    def test_dumps_list_takes_only_a_sensible_count(self):
        for n in ("abc", "-1", "0", "999"):
            self.assertEqual(self.c.get(f"/api/dumps?n={n}").status_code, 200)

    def test_suggest_returns_the_notes_without_saving(self):
        with self.haiku(raw(body="Call Mom", new_tags=[{"type": "who", "name": "Mom"}]),
                        raw(kind="step", body="Buy a pie dish", project="Thanksgiving")):
            out = self.c.post("/api/suggest", json={"text": "call mom"}).get_json()
        first, second = out["notes"]
        self.assertEqual((first["body"], first["note"], first["new_tags"]),
                         ("Call Mom", "", [{"type": "who", "name": "Mom"}]))  # maybe-tags are chips on the page
        self.assertEqual(second["note"], "Haiku's step for: Thanksgiving")
        self.assertEqual(self.notes(), [])

    def test_suggest_says_why_it_couldnt(self):
        r = self.c.post("/api/suggest", json={"text": "call mom"})
        self.assertEqual((r.status_code, r.get_json()["error"]), (503, "no Anthropic API key set"))
        self.assertEqual(self.c.post("/api/suggest", json={"text": "  "}).status_code, 400)
        self.assertEqual(self.c.post("/api/suggest", json={"text": 123}).status_code, 400)
        self.assertEqual(self.c.post("/api/capture", json={"text": 123}).status_code, 400)

    def test_done_details_source_and_ref(self):
        add = lambda **kw: self.c.post("/api/notes", json=kw).get_json()
        n = add(body="Order flex PCBs", details="JLC, 0.12 mm", when_tag="next", what=["PCBs"],
                source="Claude: test", ref="test:1")
        self.assertEqual((n["when_tag"], n["details"], n["source"], n["what"]),
                         ("2-Next", "JLC, 0.12 mm", "Claude: test", ["PCBs"]))
        again = self.c.post("/api/notes", json={"body": "Order flex PCBs now", "ref": "test:1"})
        self.assertEqual((again.status_code, again.get_json()["id"]), (200, n["id"]))  # updated, not a second one
        self.assertEqual(again.get_json()["what"], ["PCBs"])  # fields not sent are kept
        self.assertEqual(len(self.notes()), 1)

        r = self.c.put(f"/api/notes/{n['id']}", json={"done": True}).get_json()
        self.assertTrue(r["done_at"])
        self.assertEqual((r["body"], r["details"], r["when_tag"]), ("Order flex PCBs now", "JLC, 0.12 mm", "2-Next"))
        self.assertEqual(self.notes(), [])
        self.assertEqual([x["id"] for x in self.notes(done="1")], [n["id"]])
        self.assertEqual(len(self.notes(done="all")), 1)
        self.c.put(f"/api/notes/{n['id']}", json={"done": False})
        self.assertEqual([x["source"] for x in self.notes(source="Claude")], ["Claude: test"])
        self.assertEqual(self.notes(source="Tasker"), [])
        self.assertEqual(len(self.notes(q="0.12 mm")), 1)  # search covers details

    def test_bad_input_is_refused_not_quietly_applied(self):
        n = self.c.post("/api/notes", json={"body": "x", "when_tag": "next"}).get_json()
        put = lambda body: self.c.put(f"/api/notes/{n['id']}", json=body)
        self.assertEqual(put({"when_tag": "nxt"}).status_code, 400)
        self.assertEqual(put({"done": "false"}).status_code, 400)
        self.assertEqual(put({"what": "Minichord"}).status_code, 400)
        self.assertEqual(self.c.post("/api/notes", json={"body": "y", "when_tag": "tomorrow"}).status_code, 400)
        self.assertEqual(self.notes()[0]["when_tag"], "2-Next")  # untouched
        self.assertIsNone(put({"when_tag": ""}).get_json()["when_tag"])  # "" clears it on purpose
        self.c.post("/api/notes", json={"body": "z", "source": "Open Loops: x"})
        self.assertEqual(self.notes(source="Open_Loops"), [])  # _ is a letter here, not a wildcard

    def test_captures_say_where_they_came_from(self):
        self.c.post("/api/capture", data="mow the lawn", content_type="text/plain")
        self.assertEqual(self.notes()[0]["source"], "Capture")

    def test_tag_it_button_shows_only_with_a_key(self):
        self.assertNotIn(b'id="tagBtn"', self.c.get("/").data)
        with mock.patch.object(tagger, "available", return_value=True):
            self.assertIn(b'id="tagBtn"', self.c.get("/").data)


if __name__ == "__main__":
    unittest.main()
