"""
tagger.py - Claude Haiku files a brain dump as tracker notes.

Greg says or types anything from one to-do ("order flex PCBs tag with now") to
minutes of rambling, and Haiku gives back the notes in it, each with its text,
a short detail line, a When, and Who/What/Where tags picked only from the tags
already in the tracker (Haiku never makes a new one; a tag Greg asks for that
isn't there, or a person or place that might make a good one, comes back under
new_tags for him to add himself).

A big idea - something that takes several steps over weeks - comes back as a
project note plus its next few steps (Getting Things Done: a project and its
next actions). Steps Haiku thought up are kind "step"; they're never Now, and
app.py marks them in the note line so Greg can drop the ones he doesn't want.

Used by POST /api/capture (Tasker text), the brain-dump worker (recordings,
after Whisper) and POST /api/suggest (the page's Tag it button). Nothing here
is required: with no ANTHROPIC_API_KEY, no internet, an error or a refusal,
app.py falls back to the "tag with ..." word matching and files the text as
one note. Only the text and the tag names go to Anthropic.
"""
import concurrent.futures
import json
import os

MODEL = "claude-haiku-5-5"
MAX_NOTES = 20  # one dump never files more than this

TAG_TYPES = ["who", "what", "where"]
KINDS = ["said", "project", "step"]

# What each When means, by the word after the number ("1-Now" -> "now").
WHEN_HINTS = {
    "now": "right away: today, tonight, urgent, as soon as possible",
    "next": "the next things to get to: tomorrow, this week, this weekend",
    "soon": "in the next few weeks, this month",
    "later": "no rush: next month or later, when there's time",
    "someday": "maybe one day: an idea, a wish, a project to think about",
}

_client = None
# The call runs here so the caller's wait is a hard limit: the SDK's own
# timeout covers each step (connect, send, reply) but not the DNS lookup.
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="haiku")


def _api_key():
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def available():
    """True when a key is set and the SDK is installed."""
    if not _api_key():
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:  # missing, or a broken install: the page and capture carry on without it
        return False


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=_api_key(), max_retries=0)
    return _client


def _when_word(w):
    """'1-Now' -> 'now'."""
    return w.split("-", 1)[-1].strip().lower()


def system_prompt(when_tags, tags):
    whens = [f"  - {w}: {WHEN_HINTS.get(_when_word(w), _when_word(w))}" for w in when_tags]
    listed = [f"- {t}: {', '.join(tags.get(t) or []) or '(none yet)'}" for t in TAG_TYPES]
    return "\n".join([
        "You file Greg's brain dump in his to-do tracker (Getting Things Done style). He said or typed it: "
        "anything from one short to-do to minutes of rambling about what's on his mind. Spoken dumps come "
        "from speech recognition, so expect missing punctuation, run-on sentences and misheard words. \"I\" "
        "and \"me\" are Greg. He runs Key & Cable, a small repair shop for synths and Omnichords, and makes "
        "minichord accessories; his words include minichord, Omnichord, Teensy, PCB and JLC.",
        "",
        "Make one note for each separate thing to do or remember. A single to-do is one note: never split one "
        "task into pieces. Thinking aloud that isn't a task gets no note of its own, unless it's an idea worth "
        "keeping; its useful details go in the note line of the task they belong to.",
        "",
        "Big ideas: when Greg talks about something that takes several sittings over days or weeks (build a "
        "thing, start a project, redo a room), make a note for the idea itself (kind \"project\") and notes "
        "for its next 2 to 4 concrete steps, in order, each one doable in a sitting. Steps Greg said are kind "
        "\"said\"; steps you thought up are kind \"step\", and must follow plainly from what he said: no "
        "details he'd decide himself. Spread the steps out: the first one Next (or when he said), then Soon, "
        "then Later. An ordinary task is never a project.",
        "",
        "For each note:",
        "- kind: \"said\" for something Greg said, \"project\" for a big idea, \"step\" for a step you "
        "thought up.",
        "- body: the thing to do or remember, as one short plain line in Greg's words. Leave out lead-ins "
        "(\"remind me to\", \"I need to\", \"add a note\") and the words about when or about tags (\"that's "
        "for next week\", \"tag it workshop\"). Keep names, part numbers, sizes and models exactly as said. "
        "Fix a misheard word only when the context makes the right one certain (\"minicord\" is minichord).",
        "- note: details that aren't the task itself (measurements, quantities, a part number, a reason, "
        "who suggested it), short and in Greg's words, never repeating the body. Empty if there are none.",
        "- when: how soon, from what Greg said. Empty when nothing said gives a time; never guess one from "
        "how important the task sounds. A When named outright (\"tag it now\", \"someday\") always counts. "
        "A step you thought up is never Now.",
        *whens,
        "- who, what, where: tags from the lists below only, each only where it clearly fits: who is a "
        "person the task is for, with or about; what is the area of life or work it belongs to; where is "
        "where it gets done or bought. A tag Greg names outright counts when it's on the list. Leave a "
        "list empty rather than stretch a tag to fit.",
        "- new_tags: a tag Greg asks for that isn't on the lists, and a person or place in the note that "
        "isn't on the who or where list and that he's likely to have other tasks with or at: family named "
        "by role (Dad, Mom), a friend, a store. Not a customer whose instrument or order the task is about. "
        "Never one already on the lists. Usually empty.",
        "- project: for a step of a big idea (said or thought up), the body of its project note, word for "
        "word. Empty otherwise, and for the project note itself.",
        "",
        "Tags in the tracker:",
        *listed,
    ])


def output_schema(when_tags, tags):
    props = {
        "kind": {"type": "string", "enum": KINDS},
        "body": {"type": "string"},
        "note": {"type": "string"},
        "when": {"type": "string", "enum": [*when_tags, ""]},
        "new_tags": {"type": "array", "items": {
            "type": "object",
            "properties": {"type": {"type": "string", "enum": TAG_TYPES}, "name": {"type": "string"}},
            "required": ["type", "name"], "additionalProperties": False}},
        "project": {"type": "string"},
    }
    for t in TAG_TYPES:
        if tags.get(t):  # an empty enum isn't allowed; a type with no tags yet is left out
            props[t] = {"type": "array", "items": {"type": "string", "enum": list(tags[t])}}
    note = {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    return {"type": "object", "properties": {"notes": {"type": "array", "items": note}},
            "required": ["notes"], "additionalProperties": False}


def _ask_haiku(text, when_tags, tags, wait):
    """Haiku's raw answer (a dict), or raises RuntimeError with a short reason."""
    import anthropic
    try:
        response = _get_client().with_options(timeout=wait).messages.create(
            model=MODEL,
            max_tokens=8000,
            output_config={"effort": "low", "format": {"type": "json_schema",
                                                       "schema": output_schema(when_tags, tags)}},
            system=system_prompt(when_tags, tags),
            messages=[{"role": "user", "content": f"Greg's brain dump:\n{text}"}],
        )
    except anthropic.APIError as e:
        raise RuntimeError(f"{type(e).__name__}: {e}"[:300]) from e
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"Haiku stopped early ({response.stop_reason})")
    answer = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(answer)
    except ValueError as e:
        raise RuntimeError("Haiku's answer wasn't valid JSON") from e


def _clean(raw, when_tags, tags):
    """One note of Haiku's answer -> the tracker's fields, keeping only Whens and tags that exist."""
    def known(t, names):
        have = tags.get(t) or []
        by_lower = {n.lower(): n for n in have}
        out = []
        for name in names or []:
            name = str(name).strip()
            n = name if name in have else by_lower.get(name.lower())  # "Home" before "home"
            if n and n not in out:
                out.append(n)
        return out

    new_tags = []
    for nt in raw.get("new_tags") or []:
        t, name = nt.get("type"), " ".join(str(nt.get("name", "")).split())
        if t in TAG_TYPES and name and name.lower() not in {n.lower() for n in tags.get(t) or []} \
                and (t, name.lower()) not in {(x["type"], x["name"].lower()) for x in new_tags}:
            new_tags.append({"type": t, "name": name})
    kind = raw.get("kind") if raw.get("kind") in KINDS else "said"
    when = raw.get("when") if raw.get("when") in when_tags else None
    if kind == "step" and when == when_tags[0]:
        when = when_tags[1]  # a step Haiku thought up is never Now
    return {
        "kind": kind,
        "body": " ".join(str(raw.get("body", "")).split()),
        "note": " ".join(str(raw.get("note", "")).split()),
        "when_tag": when,
        **{t: known(t, raw.get(t)) for t in TAG_TYPES},
        "new_tags": new_tags,
        "project": "" if kind == "project" else " ".join(str(raw.get("project", "")).split()),
    }


def to_notes(answer, when_tags, tags):
    """Haiku's answer -> the notes to file (those with some text), at most MAX_NOTES."""
    notes = [_clean(n, when_tags, tags) for n in answer.get("notes") or [] if isinstance(n, dict)]
    return [n for n in notes if n["body"]][:MAX_NOTES]


def read(text, when_tags, tags, wait=35):
    """The notes in this text (possibly none), or raises RuntimeError (no key,
    API error, refusal, no answer within wait seconds).
    tags: {"who": [...], "what": [...], "where": [...]}, the tracker's own."""
    if not available():
        raise RuntimeError("no Anthropic API key set")
    job = _pool.submit(_ask_haiku, text, when_tags, tags, wait)
    try:
        answer = job.result(timeout=wait + 5)
    except concurrent.futures.TimeoutError:
        raise RuntimeError(f"no answer from Haiku in {wait} s") from None
    return to_notes(answer, when_tags, tags)
