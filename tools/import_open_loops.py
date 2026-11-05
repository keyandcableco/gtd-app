"""
import_open_loops.py - move the open items of the Open Loops page into GTD (one-off, 2026-11-05).

Open Loops was the pinned claude.ai page where Claude sessions left open work for
Greg; GTD took over as the one list. Each unticked item becomes a note:
- body: the item's bold line; details: the rest, plus its group's note;
- source "Open Loops: <group> (<its tag>)", ref "openloops:<data-id>", so running
  this again updates those notes instead of adding them twice;
- When: a red-dot item is Now; the groups Greg is working through are Next, the
  project groups Soon, "Carried over" (the Oct 10 and 15 lists) Someday;
- What: Haiku picks the project tags for each group's items (claude-haiku-5-5,
  one call a group), falling back to the group's usual tag.

Meant to run once. A rerun skips every item already in GTD (open or done), so
nothing Greg changed since is overwritten; --update sends them again, which
overwrites his edits to them. A note he deleted would come back on a rerun.

  .venv/bin/python tools/import_open_loops.py PAGE.html TICKS_DIR [--url URL] [--dry-run]

PAGE.html is the page as Artifact read saves it; TICKS_DIR holds its "ticks"
documents (ArtifactData list with out_dir), one <data-id>.json each.
"""
import argparse
import glob
import html
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tagger  # noqa: E402

PROJECT_TAGS = ["Repairs", "Shop app", "Business", "Minichord", "Plates", "minicontrol",
                "PCBs", "OpenChord", "TRS MIDI", "Claude setup"]
NEXT_GROUPS = {"Randy Rickard pickup", "Garage shop", "Shop app", "Setup review", "Money, customers & tax",
               "Drafts waiting to send", "Decisions Claude is waiting on", "Security & system",
               "Repair jobs and boards"}
USUAL = {  # a group's usual What, when Haiku can't be asked
    "Randy Rickard pickup": "Repairs", "Garage shop": "Workshop", "Shop app": "Shop app",
    "Setup review": "Claude setup", "Money, customers & tax": "Business", "Drafts waiting to send": "Business",
    "On the bench": "Minichord", "Juno-106 chip clones": "PCBs", "TRS MIDI out": "TRS MIDI",
    "Push, release & repo cleanup": "Minichord", "Security & system": "Claude setup", "OpenChord": "OpenChord",
    "inkkeys keypad": "Maker", "Display sniffer": "Repairs", "Repair jobs and boards": "Repairs",
    "Glance clock": "Maker", "Firmware and the device": "Minichord", "Hand tests on the instrument": "Minichord",
    "Advanced manual": "Minichord", "minicontrol and the describer": "minicontrol",
    "Upstream and Ben": "Minichord", "Boards, files and data safety": "PCBs", "Git housekeeping": "Claude setup",
}


def text(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def parse(page):
    """[(group, subgroup, group note, [item...])]; item = {id, urgent, body, details, tag}."""
    out = []
    for sec in re.split(r"<section data-name=", page)[1:]:
        name = html.unescape(sec.split('"')[1])
        sec = sec.split("</section>")[0]
        note = re.search(r'<p class="note">(.*?)</p>', sec, re.S)
        parts = re.split(r"<summary>", sec)
        for i, part in enumerate(parts):
            sub = text(part.split("<span")[0].split("</summary>")[0]) if i else ""
            items = []
            for m in re.finditer(r'<li class="item([^"]*)" data-id="([^"]+)">(.*?)</li>', part, re.S):
                inner = m.group(3)
                b = re.search(r"<b>(.*?)</b>", inner, re.S)
                d = re.search(r'<span class="d">(.*?)</span>\s*<span class="src">', inner, re.S)
                src = re.search(r'<span class="src">(.*?)</span>', inner, re.S)
                items.append({"id": m.group(2), "urgent": "urgent" in m.group(1),
                              "body": text(b.group(1)).rstrip(" .") if b else text(inner),
                              "details": text(d.group(1)).lstrip("— ").strip() if d else "",
                              "tag": text(src.group(1)) if src else ""})
            if items:
                out.append((name, sub, text(note.group(1)) if note and i == 0 else "", items))
    return out


def ask_what(group, items, what_tags):
    """{item id: [What tags]} from Haiku, or {} if it can't be asked."""
    if not tagger.available():
        return {}
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "string", "enum": [i["id"] for i in items]},
                                         "what": {"type": "array", "items": {"type": "string", "enum": what_tags}}},
        "required": ["id", "what"], "additionalProperties": False}}},
        "required": ["items"], "additionalProperties": False}
    listing = "\n".join(f"- {i['id']}: {i['body']}. {i['details'][:300]}" for i in items)
    try:
        r = tagger._get_client().with_options(timeout=60).messages.create(
            model=tagger.MODEL, max_tokens=4000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            system="Greg runs Key & Cable, a repair shop for synths and Omnichords, makes minichord accessories "
                   "(harp plates, firmware, the minicontrol app), designs PCBs and runs a small web shop. Give "
                   "each of his to-do items the area tags (\"what\") it belongs to, one or two, from the list "
                   "only. Repairs: customer instruments and bench repair work. Shop app: his workbench app. "
                   "Business: money, tax, orders, customers, email. Minichord: its firmware and the device. "
                   "Plates: harp plates. minicontrol: the app, presets, the Lab. PCBs: KiCad boards and "
                   "JLC orders. Claude setup: his computers, the Pi, security, settings and tooling. Website: "
                   "keyandcable.com. Blog: posts and write-ups (Reddit, YouTube count as Blog).",
            messages=[{"role": "user", "content": f"Group: {group}\n\n{listing}"}])
        found = json.loads(next(b.text for b in r.content if b.type == "text"))["items"]
        return {x["id"]: x["what"] for x in found}
    except Exception as e:
        print(f"  (Haiku couldn't tag {group}: {e}; using its usual tag)", file=sys.stderr)
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("page")
    ap.add_argument("ticks")
    ap.add_argument("--url", default=os.environ.get("GTD_URL", "http://100.81.145.49:8005"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--update", action="store_true", help="send items already in GTD again (overwrites edits)")
    a = ap.parse_args()

    done = set()
    for f in glob.glob(os.path.join(a.ticks, "*.json")):
        doc = json.load(open(f))
        if (doc.get("data") or doc).get("done"):
            done.add(os.path.basename(f)[:-5])
    existing = json.load(urllib.request.urlopen(a.url.rstrip("/") + "/api/tags", timeout=30))["what"]
    what_tags = sorted(set(existing) | set(PROJECT_TAGS))
    have = set() if a.update else {n["ref"] for n in json.load(
        urllib.request.urlopen(a.url.rstrip("/") + "/api/notes?done=all", timeout=30)) if n.get("ref")}

    filed = skipped = 0
    for group, sub, gnote, items in parse(open(a.page).read()):
        todo = [i for i in items if i["id"] not in done and "openloops:" + i["id"] not in have]
        skipped += len(items) - len(todo)
        if not todo:
            continue
        label = f"{group} / {sub}" if sub else group
        picked = ask_what(label, todo, what_tags)
        usual = USUAL.get(sub) or USUAL.get(group)
        for i in todo:
            when = ("now" if i["urgent"] else "someday" if group == "Carried over"
                    else "next" if group in NEXT_GROUPS else "soon")
            details = i["details"] + (f"\n\nAbout this group ({group}): {gnote}" if gnote else "")
            note = {"body": i["body"], "details": details.strip(), "when_tag": when,
                    "what": picked.get(i["id"]) or ([usual] if usual else []),
                    "source": f"Open Loops: {label}" + (f" ({i['tag']})" if i["tag"] else ""),
                    "ref": "openloops:" + i["id"]}
            if a.dry_run:
                print(f"{when:8} {', '.join(note['what']):24} {note['body'][:80]}")
            else:
                req = urllib.request.Request(a.url.rstrip("/") + "/api/notes", method="POST",
                                             data=json.dumps(note).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=30).read()
            filed += 1
    print(f"{'would file' if a.dry_run else 'filed'} {filed} notes; {skipped} ticked-off or already filed items left out",
          file=sys.stderr)


if __name__ == "__main__":
    main()
