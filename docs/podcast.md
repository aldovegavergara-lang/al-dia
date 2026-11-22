# Daily local podcast

Al día can publish a short Spanish audio digest of the **previous civil day** in
`America/Santiago`. The episode is generated locally with Qwen3-TTS (MLX, Spanish
CustomVoice) and becomes the first reel on the unfiltered reader.

## What the episode contains

The transcript is **extractive**. It reads the real headline and snippet stored for each
story, names the outlet, and states how many outlets covered it. It does not summarize with
an AI, invent facts, add opinions, or describe an article that is not in the database.

* Every story is selected by its **actual publication date** in `America/Santiago`, inside
  `[00:00, next 00:00)`. Discovery/fetch time is never used as a substitute.
* Stories already read are included. Hidden stories and items muted by the prefilter are not.
* Articles are deduplicated with the same story/event grouping used by the reader, so two
  outlets covering one event produce one story with two sources.
* The digest is capped (`max_stories`, default 25). `0` means all eligible stories. Selection
  round-robins sections and then publishers to avoid a single topic or outlet flooding the
  episode. If the cap (or a source cap) leaves stories out, the intro says “Se incluyen N de M
  historias elegibles” and never claims the episode contains everything.
* `sources` in the episode metadata is the full auditable list of the included stories'
  headlines, URLs and publishers. `item_count` is what was read; `total_items` is how many
  eligible stories existed before the cap.

Missing snippets are announced (“sin fragmento disponible”); they are not filled in.

## Schedule

The scheduler job starts preparing at **03:00** local time, when the previous civil day is
closed, so the episode is ready before **04:00**. If generation fails, it retries every
`retry_minutes` (default 5) until it succeeds. After a service restart at or after the start
hour, the first job run catches up immediately.

The job is independent of the feed tick and never runs inside an HTTP request. It uses a
non-blocking single-generation lock (in-process and `flock` across processes), APScheduler
`max_instances=1`/`coalesce=True`, and a bounded TTS subprocess timeout. An already
published day is never regenerated.

The civil-day window uses `zoneinfo`, so the 23-hour and 25-hour Chilean DST days are
handled as absolute UTC instants.

## Storage and atomic publication

Runtime files live **outside the repository**, under `data_dir()/podcasts` (for Al día this
is the private application-support data folder, never the repo):

```
podcasts/
  manifest.json                 latest published episode (written last, atomically)
  status.json                   last attempt: state, error, next retry, counts
  .generation.lock
  .staging/<uuid>/              private work area
  episodes/YYYY-MM-DD/episode-<version>.mp3
  episodes/YYYY-MM-DD/episode.json
```

The MP3 is written in `.staging`, validated (non-empty MP3 header, positive finite duration
from the TTS sidecar), moved into the day folder under a fresh UUID version as
`episode-<version>.mp3`, and only then is `manifest.json` atomically replaced. Audio is
immutable: each generation writes its own version and the metadata (`episode.json` and
`manifest.json`) carries the same `version`, so a failure or crash leaves the previous
manifest and the audio an already published episode serves untouched, and no old version is
overwritten. Metadata without a `version` is the legacy layout: the first real episode's
`episode.mp3` stays unchanged and keeps resolving. Status and errors are persisted in
`status.json`.

`latest_episode()` returns `None` or a dict with:

```json
{
  "day": "2026-11-21",
  "title": "Al día · sábado 21 de noviembre de 2026",
  "duration_seconds": 412.8,
  "item_count": 18,
  "total_items": 31,
  "audio_url": "/reels/podcast/2026-11-21/audio?v=7d2f2a3c-1b0e-4a5f-9c8d-2e6b0a1c4d5e",
  "transcript": "…",
  "sources": [{"title": "…", "url": "https://…", "publisher": "…"}],
  "generated_at": "2026-11-22T06:04:12+00:00",
  "version": "7d2f2a3c-1b0e-4a5f-9c8d-2e6b0a1c4d5e"
}
```

## Web integration

* `GET /reels/podcast/{day}/audio` is authenticated by the existing middleware and the
  `Depends(ui.get_conn)` dependency. The day is parsed as a strict ISO date and the resolved
  file must stay inside `podcasts/episodes`; there is no public static mount.
* The optional query parameter `v` selects one immutable version: it must be a canonical UUID
  and must resolve to the versioned file inside the day folder. Without `v`, the legacy
  `episode.mp3` of the first real episode resolves.
* `FileResponse` supports byte ranges, so the browser can seek in the audio.
* `podcast=latest_episode()` is passed to the `reels.html` render only for the unfiltered
  first page of `pending`, `latest` and `all`. Read, saved, filtered and later pages do not
  receive it. The card/transcript markup belongs to the frontend.

## Configuration

The podcast is **off by default**. Enable it with either environment variables or the
optional private file `data_dir()/podcast-config.json` (environment wins):

```json
{
  "enabled": true,
  "python": "/path/to/tts-venv/bin/python",
  "model": "/path/to/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
  "voice": "Vivian",
  "language": "Spanish",
  "max_stories": 25,
  "max_per_source": 0,
  "retry_minutes": 5,
  "timeout_seconds": 900
}
```

| Environment | File key | Default | Meaning |
|---|---|---|---|
| `NEWSNEST_PODCAST_ENABLED` | `enabled` | off (on if the file exists) | Enable generation |
| `NEWSNEST_PODCAST_CONFIG` | — | `data_dir()/podcast-config.json` | Private config file |
| `NEWSNEST_PODCAST_PYTHON` | `python` | current interpreter | Python with `mlx-audio` |
| `NEWSNEST_PODCAST_MODEL` | `model` | — | Local model path or MLX model id |
| `NEWSNEST_PODCAST_SCRIPT` | `script` | `scripts/podcast-tts.py` | TTS runner |
| `NEWSNEST_PODCAST_FFMPEG` | `ffmpeg` | `ffmpeg` | ffmpeg executable |
| `NEWSNEST_PODCAST_VOICE` | `voice` | `Vivian` | CustomVoice speaker |
| `NEWSNEST_PODCAST_LANGUAGE` | `language` | `Spanish` | Qwen language |
| `NEWSNEST_PODCAST_INSTRUCT` | `instruct` | empty | Optional style instruction |
| `NEWSNEST_PODCAST_CHUNK_CHARS` | `chunk_chars` | 350 | Max characters per TTS chunk |
| `NEWSNEST_PODCAST_TEMPERATURE` | `temperature` | 0.45 | TTS sampling temperature |
| `NEWSNEST_PODCAST_MAX_TOKENS` | `max_tokens` | 512 | Max output tokens per chunk (0 = model default) |
| `NEWSNEST_PODCAST_TIMEZONE` | `timezone` | `America/Santiago` | Civil-day time zone |
| `NEWSNEST_PODCAST_START_HOUR` | `start_hour` | 3 | Start preparing (local hour) |
| `NEWSNEST_PODCAST_READY_HOUR` | `ready_hour` | 4 | Documented target (local hour) |
| `NEWSNEST_PODCAST_MAX_STORIES` | `max_stories` | 25 | Story cap; 0 = all eligible stories |
| `NEWSNEST_PODCAST_MAX_PER_SOURCE` | `max_per_source` | 0 (no cap) | Optional per-outlet cap |
| `NEWSNEST_PODCAST_RETRY_MINUTES` | `retry_minutes` | 5 | Retry interval after failure |
| `NEWSNEST_PODCAST_TIMEOUT_SECONDS` | `timeout_seconds` | 900 | Hard TTS subprocess timeout |
| `NEWSNEST_PODCAST_TITLE` | `title` | `Al día` | Episode title prefix |

No personal path is hardcoded. The coordinator/deployment supplies the TTS interpreter and
model paths. Missing paths fail into `status.json` and are retried; they never break the web
app or the feed scheduler.

## Manual generation

```sh
cd lector
.venv/bin/python -m newsnest.podcast --date 2026-09-05 [--force]
```

`--date` must name a civil day that has closed (`America/Santiago`); today and future dates are
refused. Without `--date`, the scheduler uses the same previous civil day. The manual path
writes the same `status.json`/`manifest.json` as the scheduled path.

## Management status

`/reels/manage` passes `podcast_status`, `podcast_enabled` and `podcast_max_stories` to the
management template. The status contains lifecycle facts only; subprocess stderr and filesystem
paths are not exposed in the management UI. `public_status(now=None)` also adds `is_late`
(enabled, local hour at or past `ready_hour`, and the latest published episode is not the
previous civil day's), `ready_at` (today's ready hour as an ISO UTC string) and `expected_day`
(the previous civil day).

## TTS runner

`scripts/podcast-tts.py` runs in the configured Python (the web environment does not need
MLX). It:

1. splits the transcript into sentence-aware chunks (`--chunk-chars`);
2. loads the Qwen3-TTS CustomVoice model and calls `generate_custom_voice(text=…, speaker=…,
   language="Spanish", temperature=0.45, max_tokens=512)`;
3. validates every chunk and the joined waveform (finite samples, non-silent peak/RMS);
4. writes a temporary WAV and converts it to MP3 with `ffmpeg`;
5. writes a JSON sidecar with the exact duration for the caller to validate.

A non-zero exit, timeout, missing sidecar, invalid MP3 header or silent audio is treated as a
failure: nothing is published and the next retry starts from a clean staging folder.

## Tests

`tests/test_podcast.py` uses synthetic database rows and a fake TTS callback; it never loads
the real model. It covers:

* civil-day boundaries and the 23/25-hour DST days;
* publication-date selection (not discovery time), read stories, hidden/muted exclusion and
  deduplication;
* lead-only snippet attribution (a secondary outlet's extract is not borrowed under the lead);
* caps, topic/source diversity and honest “N of M” wording;
* immutable versioned audio: a failed manifest write during same-day regeneration keeps the
  previous metadata and audio bytes, and the legacy `episode.mp3` of the first real episode
  resolves unchanged;
* idempotence, retry timing, failure atomicity and path traversal;
* authentication, strict version validation and byte-range serving of the audio endpoint;
* the `is_late`/`ready_at`/`expected_day` late status and the CLI refusing today and future
  dates;
* the scheduler start hour and catch-up behavior.
