"""
transcribe.py - Whisper writes out a brain-dump recording, here on the server.

The same setup as the shop app's voice intake (faster-whisper on the CPU, its
vocabulary prompt and silence filter): the audio never leaves the machine, and
the model is downloaded once into WHISPER_DIR. On the Pi 4, medium.en takes
about 2.7 minutes per minute of talk and about 2 GB of memory while loaded, so
the model is dropped again whenever the queue runs empty. WHISPER_MODEL=small.en
is about three times faster but lost words on the shop's tests; "off" turns
recordings off (they wait in the queue).

faster-whisper is optional: without it the app runs normally and recordings
just wait.
"""
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium.en")
WHISPER_DIR = Path(os.environ.get("WHISPER_DIR", APP_DIR / "whisper_models"))

_model = None


def available():
    if WHISPER_MODEL == "off":
        return False
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        WHISPER_DIR.mkdir(parents=True, exist_ok=True)
        # Leave a core free so the page and the other apps stay responsive.
        threads = int(os.environ.get("WHISPER_THREADS", max(1, (os.cpu_count() or 2) - 1)))
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                              cpu_threads=threads, download_root=str(WHISPER_DIR))
    return _model


def unload():
    """Let go of the model's memory until the next recording."""
    global _model
    _model = None


VOCABULARY = ["minichord", "Omnichord", "Key & Cable", "Teensy", "PCB", "JLC"]


def prompt_for(tags):
    """Whisper's vocabulary hint: Greg's shop words and the tracker's tag names, spelled his way."""
    names = VOCABULARY + [n for t in ("who", "what", "where") for n in tags.get(t) or []]
    return "Greg's to-do list brain dump. " + ", ".join(names) + "."


def transcribe_file(path, prompt=""):
    """Plain transcript text of one audio file."""
    segments, _info = _get_model().transcribe(
        str(path),
        beam_size=5,
        vad_filter=True,                    # skip the silence before/after talking
        initial_prompt=prompt or None,      # tag spellings
        condition_on_previous_text=False,   # with a prompt, True can loop and drop whole sentences
    )
    return " ".join(s.text.strip() for s in segments).strip()
