# From the Field Clipper — Coder deployment

This fork of Chopify powers an internal, Coder-hosted workflow for the
**From the Field Agronomy Podcast**. It uploads full episodes from a browser,
transcribes them with Parakeet on CPU, finds complete-thought clips, ranks them
with agronomy-aware editorial scoring, and exports captioned **1080×1920**
masters after human approval.

The original Chopify CLI and Studio remain in the repository. The Coder app is
`app.py`; it does not use the local-only Studio server.

## URL and authentication

The app listens on `0.0.0.0:8765` so the Coder workspace proxy can reach it:

```
https://8765--main--test--crashcoleman22.dollarsperacrecropsolutions.app/
```

Access is protected by the **Coder workspace login** — the proxy does not
forward unauthenticated visitors. The app also:

- accepts only `Host` values in `PODCAST_CLIPPER_ALLOWED_HOSTS` (defaults:
  localhost, `*.dollarsperacrecropsolutions.app`, `*.coder.app`);
- checks same-origin on POST requests and requires a per-session CSRF token;
- sets `HttpOnly`, `SameSite=Strict` session cookies, a restrictive CSP, and
  `frame-ancestors 'none'`.

### Optional application access code

Add a second lock (recommended if the workspace proxy is shared):

```bash
export PODCAST_CLIPPER_ACCESS_CODE='your-long-code'
# or, persisted outside Git:
printf '%s\n' 'your-long-code' > /home/coder/podcast-clipper-data/access-code.txt
```

Restart the app. Visitors then see an access-code page after Coder login.
Failed attempts are rate-limited. Remove the environment variable and file to
disable the extra lock.

Never put real codes in Git.

## Start, stop, and autostart

```bash
cd /home/coder/workspace/from-the-field-clipper

scripts/start.sh      # background, idempotent, health-checked
scripts/status.sh
scripts/stop.sh
```

Logs: `/home/coder/podcast-clipper-data/logs/app.log`.

The app auto-resumes interrupted work on startup:

- jobs marked `running` in `queue.json` return to `pending` and execute;
- episodes stuck in `queued`, `processing`, `rendering`, or
  `rendering_previews` are re-enqueued;
- a pipeline that finished transcribing but crashed before editorial analysis is
  re-run from cache.

`python chopify.py` checkpoints transcription chunks and completed renders, so a
restart does not start over.

### Template auto-start

If the Coder template supports a workspace startup command, set it to:

```bash
bash /home/coder/workspace/from-the-field-clipper/scripts/start.sh
```

Where no template hook or user service manager exists, run
`scripts/install-profile-autostart.sh` to add a guarded, idempotent start to
`~/.profile`. This is a fallback, not a substitute for a template startup hook.

## Storage layout

All large files live outside Git in `/home/coder/podcast-clipper-data`:

```
sources/<sha256>/<original>.mp4   immutable originals, source.json
uploads/<upload-id>/              resumable upload parts
projects/<episode-id>/            project.json, transcript, candidates,
                                  segments, editorial.json, review.json,
                                  clips/, previews/, job.log
exports/<episode-id>/<clip-id>/   master.mp4, captions.srt, poster.png,
                                  metadata.json
brand/                            brand.json, chopify-brand.json,
                                  vocabulary.json, logo/intro/outro files
models/                           Parakeet and YuNet caches
queue.json                        persistent serial queue
logs/app.log
```

- Browser uploads are hashed (SHA-256), content-addressed, deduplicated, and
  stored read-only. Re-uploading the same bytes opens the existing episode.
- Originals are never modified and are deleted only by an explicit
  **Storage Management → Delete** action. A **Verify hash** action re-hashes the
  original to prove it is unchanged.
- Uploads and processing are refused when accepting the file would leave less
  than the **30 GB** safety reserve (`PODCAST_CLIPPER_MIN_FREE_GB`).
- Runtime data, generated media, models, sessions, and codes are ignored by Git.

## Workflow

1. **Upload Episodes** — choose one or many MP4/MOV/MKV/WebM/M4V files. Uploads
   use resumable 8 MB chunks; the serial queue processes one episode at a time.
2. **Processing Queue** — Parakeet transcription, sentence-aware discovery,
   format detection, and selection. The queue survives restarts.
3. **Clips Awaiting Review** — for every clip: exact transcript and timestamps,
   overall score, engagement, agronomic relevance, educational value,
   timeliness, hook strength, selection explanation, seasonal relevance and
   expiration, suggested title/description, and platform recommendations.
   Edit text, play the source range, override composition, or render previews.
4. **Approve** — choose clips and export. Rejected clips never render.
5. **Approved Clips** — download the shared 1080×1920 H.264/AAC master, SRT,
   poster, and platform-specific metadata. A single approval covers export;
   publishing/scheduling are intentionally not connected.

Clip targets: **30–90 seconds preferred**, up to **180 seconds only to preserve
a complete thought**. Sentence boundaries come from the transcript, and the
deterministic fallback can lower the minimum for unusually short episodes.

### Layout hints

After processing, the app samples frames with YuNet (downloaded once to
`models/yunet.onnx`) and suggests:

- **1 speaker** → single full-frame crop;
- **2 speakers** → split view with two stacked crops centered on face clusters;
- **3+ speakers** → single frame with `contain` so nobody is cropped out.

Every episode can be manually overridden to `single`, `split`, or `stacked`
with `cover`/`contain`.

### Brand settings

Podcast name, logo, colors, caption font/size/color/position, speaker names,
custom agricultural vocabulary, and platform-safe margins. The brand is
validated and converted to Chopify's caption preset for rendering.

Custom vocabulary uses one pair per line:

```
side dress => sidedress
vomit oxen => vomitoxin
```

Corrections are applied as whole-token replacements after transcription, before
discovery and rendering. They never invent new sentences.

Intro and outro files can be stored now. They are **not stitched into exports
yet**; the UI says so explicitly.

## AI editorial scoring

The app uses the workspace LiteLLM gateway through an OpenAI-compatible API:

```bash
AI_DEFAULT_BASE_URL=http://dollarsperacrecropsolutions-app-shared-litellm:4000
AI_DEFAULT_API_KEY=...                 # injected by the workspace, never committed
PODCAST_CLIPPER_AI_MODEL=sync/opencode-go/deepseek-v4.1-flash
```

The default model is `sync/opencode-go/deepseek-v4.1-flash`. The gateway bills
`opencode-go` allowance tokens rather than per-request dollars in this workspace;
the app records token usage when the gateway returns it.

If the gateway is absent, disabled (`PODCAST_CLIPPER_AI_DISABLED=1`), slow, or
returns malformed JSON, deterministic local scoring is used automatically. The
UI labels each clip **AI assisted** or **Deterministic**.

Prompts require JSON-only output and prohibit invented quotes, trials, yields,
numbers, products, and recommendations. The app additionally flags any number
in an AI title/description that does not appear in the clip transcript
(`needs_review`). Human review is still mandatory: AI text is a draft, not a
fact-check.

## First episode

1. Open the Coder URL and sign in.
2. Go to **Brand Settings** once: name, logo, captions, vocabulary.
3. Go to **Upload Episodes**, choose one MP4, and wait for the chunk progress to
   finish. The upload is validated (duration and audio track) before queueing.
4. Watch **Processing Queue**. First use of Parakeet downloads ~631 MB into
   `models/` (already prefetched in this workspace).
5. Open **Clips Awaiting Review**, play each range, edit title/description,
   choose platforms, then approve the clips worth posting.
6. Download **Approved Clips** (master MP4, SRT, poster, metadata JSON).

## Verification and tests

```bash
cd /home/coder/workspace/from-the-field-clipper
.venv/bin/python -m pytest -q
scripts/prefetch-models.sh
```

The Coder app tests cover storage/deduplication, resumable upload offsets,
vocabulary/brand validation, deterministic editorial scoring, the AI
numeric-guard, CSRF/host rejection, and queue recovery. The upstream Chopify
suite also passes.

End-to-end validation performed in this workspace:

- browser-style chunked upload of an MP4;
- Parakeet transcription;
- clip discovery and selection;
- AI-assisted scoring with deterministic fallback;
- human title/platform edit;
- approval and export;
- 1080×1920 H.264/AAC master plus SRT/poster/metadata;
- source SHA-256 unchanged after processing.

## Known limitations

- **No publishing or scheduling.** Facebook, Instagram, and YouTube are not
  connected; Content Calendar, Published Content, and Analytics are marked
  coming soon and have no working controls.
- **No URL ingestion in the Coder app.** Only browser-uploaded files are
  accepted. The upstream CLI can still download URLs, but that path is not
  exposed here.
- **One worker, no GPU.** Episodes process serially on CPU with 2-part ASR and 2
  render workers. A 60–90 minute episode will take tens of minutes to transcribe.
- **Speaker diarization is not word-level.** Layout hints count faces; they do
  not label who said what. Speaker names are brand metadata only.
- **3+ speaker grid** is represented by a full-frame `contain` composition, not
  a custom multi-camera grid.
- **Intro/outro stitching is not implemented.**
- **AI is a drafting aid.** Titles/descriptions can still require editing; the
  numeric guard only catches invented numbers, not every unsupported claim.
- **No user accounts or roles.** Anyone who passes Coder auth (and the optional
  access code) has the same full access.
- **No external object storage.** Backups and retention are the workspace
  volume's responsibility.
- **Very short sources** may select one longer clip because there are not
  enough sentence boundaries to meet the 30-second minimum; real episodes use
  the preferred 30–90 second window.
- **`ffmpeg` loudness normalization** changes level, not speech content; the
  original source audio is untouched in `sources/`.

## Configuration reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `PODCAST_CLIPPER_DATA` | `/home/coder/podcast-clipper-data` | Persistent data root |
| `PODCAST_CLIPPER_HOST` | `0.0.0.0` | Bind address (keep `0.0.0.0` for Coder) |
| `PODCAST_CLIPPER_PORT` | `8765` | Coder app port |
| `PODCAST_CLIPPER_MIN_FREE_GB` | `30` | Disk safety reserve |
| `PODCAST_CLIPPER_ACCESS_CODE` | unset | Optional second-factor code |
| `PODCAST_CLIPPER_ALLOWED_HOSTS` | Coder/local defaults | Comma-separated Host allow-list |
| `PODCAST_CLIPPER_AI_MODEL` | `sync/opencode-go/deepseek-v4.1-flash` | Editorial model |
| `PODCAST_CLIPPER_AI_DISABLED` | unset | `1` forces deterministic scoring |
| `CHOPIFY_ASR_PARTS` | `2` | Parallel ASR chunks |
| `CHOPIFY_RENDER_WORKERS` | `2` | Parallel clip renders |
| `CHOPIFY_SCAN_WORKERS` | `2` | Parallel visual scan workers |
