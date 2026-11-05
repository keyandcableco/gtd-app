# Tracker — a lightweight GTD tracker

Single-file Flask app + SQLite. Fast capture, When/Who/What/Where tags,
filter/edit/delete. Runs on your home network; reach it from your phone over Tailscale.

## Run it

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5001 on the same machine, or http://<that-machine's-LAN-ip>:5001
from another device on your network. The database (`gtd.db`) is created next to `app.py`
on first run. Override its location with the `GTD_DB` env var.

Runs on port **5001** by default (5000 is left free for other apps). Change it with the
`GTD_PORT` env var, e.g. `GTD_PORT=5050 python app.py`.

## Reach it from your phone (Tailscale)

1. Install Tailscale on the server machine and on your phone; log both into the same account.
2. Find the server's Tailscale IP (`tailscale ip -4`) — it looks like `100.x.y.z`.
3. On your phone, go to `http://100.x.y.z:5001`. That's it — works anywhere with signal,
   no port forwarding, no exposing anything to the public internet.
4. Optional: `tailscale serve 5001` gives you a clean HTTPS hostname instead of the raw IP.

Add the page to your phone's home screen so it opens like an app. For voice capture,
just tap the microphone key on your phone keyboard inside the text box.

## Voice capture (Tasker or any HTTP client)

`POST /api/capture` takes spoken text and files it as a note:

```bash
curl -X POST http://100.x.y.z:5001/api/capture \
  -H 'Content-Type: text/plain' \
  -d "pick up wood screws at Home Depot for the kids' shed this weekend"
```

Send either a raw `text/plain` body (easiest, no JSON escaping) or `{"text": "..."}`.
The response includes a one-line `summary` for a Tasker flash, and `reader` says which
of the two readers below filed it.

**With an Anthropic API key** (`ANTHROPIC_API_KEY` in the app's environment), Claude
Haiku reads it the way you'd say it (`tagger.py`), from one to-do to a whole brain dump,
and files as many notes as it holds. Each note gets tidied text, details in the note
field, a When from what you said ("this weekend" is Next, "no rush" is Later; none if you
didn't say), and Who/What/Where tags from the ones you already have. It never makes a new
tag: a tag you ask for that doesn't exist, or a person or place that might make a good
one, is added to the note field as `maybe tag: who Dad`.

A big idea (something that takes several sittings over weeks) becomes a project note plus
its next 2 to 4 steps, spread over Next, Soon and Later. The note field says which is
which: `project`, `part of: <project>` for a step you said, and `Haiku's step for:
<project>` for one Haiku thought up (never Now; delete the ones you don't want).
Searching for the project's name finds its steps.

Only the text and your tag names go to Anthropic; a capture costs a small fraction of a
cent and takes a few seconds. Every capture is kept as it came in, with the notes it
became, under **Recent captures** on the page.

**Without a key**, or whenever Haiku can't be reached, end the sentence with
"tag with ..." and the tag names are matched against the When values and your existing
tags (`order flex PCBs tag with Now and Workshop`). Names that don't match anything are
kept in the note field as `unmatched tags: ...`; if nothing after "tag with" matches,
the whole text is kept as the body.

With a key the page also gets a **Tag it** button: Haiku reads the text box and fills in
the form (tidied text, the note line and When where they're blank, and tags). Nothing is
saved until you tap Add, and a tag that doesn't exist yet is offered as a dashed
"+ who: Dad" chip you can tap to add it. If the text holds several notes, they're listed
instead, to drop the ones you don't want and Add all.

## Brain dumps (recordings, written out on the server)

For rambling longer than the phone's speech-to-text will wait, `POST /api/dump` takes a
recording: the raw audio as the body (any common format; m4a from the phone is fine) or a
multipart upload. It answers at once with a `summary`; Whisper writes the recording out
here (`transcribe.py`, faster-whisper on the CPU, the audio never leaves the server) and
it's filed like a capture. On a Raspberry Pi 4, the default `medium.en` takes about
2.7 minutes per minute of talk and 2 GB of memory while it works (let go again after);
`WHISPER_MODEL=small.en` is about three times faster but drops more words, and `off`
leaves recordings waiting. Recent captures on the page shows each one's progress.

The server needs `faster-whisper` (requirements.txt) and two folders that outlive the
container: `dumps/` (the recordings) and `whisper_models/` (downloaded once, about 1.5 GB
for medium.en; `WHISPER_DIR` can point at a copy another app already has).

**Tasker** (one task that starts recording, and sends it when run again; point the
phone's double-tap at it):

1. `If %BRAINDUMP ~ on`
2. ` Record Audio Stop`
3. ` Variable Clear %BRAINDUMP`
4. ` HTTP Request`: Method POST, URL `http://100.x.y.z:5001/api/dump`, Headers
   `Content-Type:audio/mp4`, File To Send `Tasker/braindump.m4a`, Timeout 60
5. ` Flash %http_data.summary`
6. `Else`
7. ` Record Audio`: Source Mic, Codec AAC, Format MPEG4, File `Tasker/braindump.m4a`
8. ` Variable Set %BRAINDUMP to on`
9. ` Flash Listening: double-tap again to send`
10. `End If`

## Done, details, and Claude's handoffs

Tick a note's box when it's done: it leaves the list, and **✓ Done** shows the finished
ones (untick to open one again). Delete is for notes you don't want at all. A note can
also carry **Details**, the long version folded shut under it.

Claude sessions file what they leave open for Greg here, and read his notes by project,
with the `gtd` command (`~/.local/bin/gtd` on the laptop, over Tailscale): `gtd list
--what Minichord`, `gtd add "Title" --details ... --what PCBs --when next --source
"Claude: <topic>"`, `gtd done ID`. The API underneath: `POST /api/notes` takes `details`,
`source` and `ref` (sending the same `ref` again updates that note), `PUT
/api/notes/<id>` changes only the fields sent (`"done": true/false` ticks it), and `GET
/api/notes` takes `done=0|1|all` and `source=` (a prefix). `tools/import_open_loops.py`
moved the Open Loops page's open items in on 2026-11-05.

## Backup / grab a copy

The **⬇ backup db** link (or `GET /api/export`) downloads the live `gtd.db`.
With Tailscale you're always hitting the one real database, so there's no merge step —
this is purely for backups. Copy `gtd.db` somewhere safe periodically, or script it:

```bash
curl -s http://100.x.y.z:5001/api/export -o "backup-$(date +%F).db"
```

## Notes on the data model

- Every note has a UUID, `created_at`, `updated_at`, and a soft-delete flag
  (`deleted=1`), so deletes are recoverable and the schema is already sync-friendly
  if you ever move off the single-server model.
- `When` is fixed (1-Now … 5-Someday). `Who/What/Where` tags are free-form; new ones
  are created as you type and reused via autocomplete afterward.
- Each entry has an optional short **note** field for measurements or quick details.
- Notes sort by When ascending, then most-recently-updated.

Existing databases are migrated automatically on startup — the `note` column is added
if it isn't already there, with no data loss.

## Run it as a background service (optional)

On Linux, a minimal systemd unit keeps it running and restarts on boot:

```ini
# /etc/systemd/system/tracker.service
[Unit]
Description=GTD Tracker
After=network.target

[Service]
WorkingDirectory=/path/to/gtd
ExecStart=/usr/bin/python3 /path/to/gtd/app.py
Restart=always
Environment=GTD_DB=/path/to/gtd/gtd.db

[Install]
WantedBy=multi-user.target
```

Then `sudo systemctl enable --now tracker`.

## License

Copyright © 2026 Greg Miller / The Key & Cable Company.

Licensed under the [GNU General Public License v3.0](LICENSE). You can use, study, share and modify it. If you distribute it or a modified version, you have to make the source available under the same licence.
