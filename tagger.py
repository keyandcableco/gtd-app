"""
tagger.py - Claude Haiku reads a note the way Greg says it into the tracker's fields.

"I need to grab more twin rose at Home Depot for the kids' shed this week" comes
back as the note itself, a short detail line, a When, and Who/What/Where tags.
Tags are picked only from the ones already in the tracker: Haiku never makes a
new one. A tag Greg asks for that isn't there, or a person or place that might
make a good one, comes back under new_tags for him to add himself.

Used by POST /api/capture (Tasker), which saves what comes back, and POST
/api/suggest (the page's Tag it button), which only fills in the form. Nothing
here is required: with no ANTHROPIC_API_KEY, no internet, an error or a
refusal, capture falls back to the "tag with ..." word matching in app.py and
the button says why. Only the note's text and the tag names go to Anthropic.
"""
import json
import os

MODEL = "claude-haiku-5-5"
TIMEOUT_SECONDS = 15  # Tasker or the page is waiting, on the app's one gunicorn worker

TAG_TYPES = ["who", "what", "where"]

# What each When means, by the word after the number ("1-Now" -> "now").
WHEN_HINTS = {
    "now": "right away: today, tonight, urgent, as soon as possible",
    "next": "the next things to get to: tomorrow, this week, this weekend",
    "soon": "in the next few weeks, this month",
    "later": "no hurry: next month or later, when there's time",
    "someday": "maybe one day: an idea, a wish, a project to think about",
}

_client = None


def _api_key():
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def available():
    """True when a key is set and the SDK is installed."""
    if not _api_key():
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=_api_key(), timeout=TIMEOUT_SECONDS, max_retries=0)
    return _client


def _when_word(w):
    """'1-Now' -> 'now'."""
    return w.split("-", 1)[-1].strip().lower()


def system_prompt(when_tags, tags):
    whens = [f"- {w}: {WHEN_HINTS.get(_when_word(w), _when_word(w))}" for w in when_tags]
    listed = [f"- {t}: {', '.join(tags.get(t) or []) or '(none yet)'}" for t in TAG_TYPES]
    return "\n".join([
        "You file a note in Greg's to-do tracker (Getting Things Done style). Greg said or typed it; spoken "
        "notes come from speech recognition, so expect missing punctuation, run-on sentences and misheard "
        "words. \"I\" and \"me\" are Greg.",
        "",
        "Give:",
        "- body: the thing to do or remember, as one short plain line in Greg's words. Leave out lead-ins "
        "(\"remind me to\", \"I need to\", \"add a note\", \"note to self\") and the words about when or about "
        "tags (\"that's for next week\", \"tag it workshop\"). Keep names, part numbers, sizes and models "
        "exactly as said; never fix a word you aren't sure was misheard.",
        "- note: details that aren't the task itself (measurements, quantities, a part number, a reason, "
        "who suggested it), short and in Greg's words, never repeating the body. Empty if there are none.",
        "- when: how soon, from what Greg said. Empty when nothing said gives a time; never guess one from "
        "how important the task sounds. A When named outright (\"tag it now\", \"someday\") always counts.",
        *whens,
        "- who, what, where: tags from the lists below only, each only where it clearly fits: who is a "
        "person the task is for, with or about; what is the area of life or work it belongs to; where is "
        "where it gets done or bought. A tag Greg names outright counts when it's on the list. Leave a "
        "list empty rather than stretch a tag to fit.",
        "- new_tags: a tag Greg asks for that isn't on the lists, and a person or place in the note that "
        "isn't on the who or where list and that he's likely to have other tasks with or at: family named "
        "by role (Dad, Mom), a friend, a store. Not a customer whose instrument or order the task is about. "
        "Never one already on the lists. Usually empty.",
        "",
        "Tags in the tracker:",
        *listed,
    ])


def output_schema(when_tags, tags):
    props = {
        "body": {"type": "string"},
        "note": {"type": "string"},
        "when": {"type": "string", "enum": [*when_tags, ""]},
        "new_tags": {"type": "array", "items": {
            "type": "object",
            "properties": {"type": {"type": "string", "enum": TAG_TYPES}, "name": {"type": "string"}},
            "required": ["type", "name"], "additionalProperties": False}},
    }
    for t in TAG_TYPES:
        if tags.get(t):  # an empty enum isn't allowed; a type with no tags yet is left out
            props[t] = {"type": "array", "items": {"type": "string", "enum": list(tags[t])}}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _ask_haiku(text, when_tags, tags):
    """Haiku's raw answer (a dict), or raises RuntimeError with a short reason."""
    import anthropic
    try:
        response = _get_client().messages.create(
            model=MODEL,
            max_tokens=2000,
            output_config={"effort": "low", "format": {"type": "json_schema",
                                                       "schema": output_schema(when_tags, tags)}},
            system=system_prompt(when_tags, tags),
            messages=[{"role": "user", "content": f"Greg's note:\n{text}"}],
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


def to_fields(answer, text, when_tags, tags):
    """Haiku's answer -> the tracker's fields, keeping only Whens and tags that exist."""
    def known(t, names):
        by_lower = {n.lower(): n for n in tags.get(t) or []}
        out = []
        for name in names or []:
            n = by_lower.get(str(name).strip().lower())
            if n and n not in out:
                out.append(n)
        return out

    fields = {t: known(t, answer.get(t)) for t in TAG_TYPES}
    existing = {n.lower() for t in TAG_TYPES for n in tags.get(t) or []}
    new_tags = []
    for nt in answer.get("new_tags") or []:
        name = " ".join(str(nt.get("name", "")).split())
        if nt.get("type") in TAG_TYPES and name and name.lower() not in existing \
                and name.lower() not in {x["name"].lower() for x in new_tags}:
            new_tags.append({"type": nt["type"], "name": name})
    when = answer.get("when")
    return {
        "body": " ".join(str(answer.get("body", "")).split()) or text,
        "note": " ".join(str(answer.get("note", "")).split()),
        "when_tag": when if when in when_tags else None,
        **fields,
        "new_tags": new_tags,
        "reader": "haiku",
    }


def read(text, when_tags, tags):
    """The fields for this note, or raises RuntimeError (no key, API error, refusal).
    tags: {"who": [...], "what": [...], "where": [...]}, the tracker's own."""
    if not available():
        raise RuntimeError("no Anthropic API key set")
    return to_fields(_ask_haiku(text, when_tags, tags), text, when_tags, tags)
