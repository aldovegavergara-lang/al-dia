# Daily local podcast

Al día can publish a short Spanish audio digest of the **previous civil day** in
`America/Santiago`. The episode is generated locally with Qwen3-TTS (MLX, Spanish
CustomVoice) and becomes the first reel on the unfiltered reader.

## What the episode contains

When `NEWSNEST_PODCAST_EDITOR_MODEL` is configured, the narration is **editorial**: a local
Spanish model rewrites the selected stories into a conversational podcast, translating English
and using brief natural transitions. It starts with the main news and has no spoken
introduction, story numbering, repeated Coverage/Publisher lines or outro, and never comments
on the input data or on details it does not have. The model is local,
offline and untrusted: incomplete, duplicated, unknown-ID, empty, overlong, tagged or
number-inventing segments reject the whole generation. The narration is **not fact-checked** and
may paraphrase or err; articles are treated as quoted, untrusted data, never instructions.

Without an editor model the fallback is **extractive**: it reads the stored headline and snippet
attributed once to the outlet, with no AI summary, opinions or invented facts. The episode metadata
carries `narration_mode` (`editorial` or `extractive`); old metadata defaults to `extractive`.

* Every story is selected by its **actual publication date** in `America/Santiago`, inside
  `[00:00, next 00:00)`. Discovery/fetch time is never used as a substitute.
* Stories already read are included. Hidden stories and items muted by the prefilter are not.
* Articles are deduplicated with the same story/event grouping used by the reader, so two
  outlets covering one event produce one story with two sources.
* The digest is capped (`max_stories`, default 25). `0` means all eligible stories. Selection
  round-robins sections and then publishers to avoid a single topic or outlet flooding the
  episode. Before narration the same selected stories are grouped into contiguous themes (frontier
  tech, providers, world/news, other tech) while keeping score order inside each group: selection
  counts and coverage do not change, but the audio stops alternating topics every story.
  The cap and the full source list are **visual metadata only** (`item_count` is what
  was narrated; `total_items` is how many eligible stories existed before the cap); the audio
  never speaks selection counts or “Cobertura”.
* `sources` in the episode metadata is the full auditable list of the included stories'
  headlines, URLs and publishers.

Missing snippets are not announced and are not filled in with invented text.

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
  "version": "7d2f2a3c-1b0e-4a5f-9c8d-2e6b0a1c4d5e",
  "narration_mode": "editorial"
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
  "editor_python": "/path/to/editor-venv/bin/python",
  "editor_model": "/path/to/local/Qwen3-editor",
  "editor_script": "scripts/podcast-editor.py",
  "editor_timeout_seconds": 900,
  "editor_provider": "local",
  "editor_api_model": "gpt-6-luna",
  "ref_audio": "",
  "ref_text_file": "",
  "pronunciations_file": "",
  "tempo": 1.0,
  "seed": 0,
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
| `NEWSNEST_PODCAST_EDITOR_PYTHON` | `editor_python` | TTS python | Python with `mlx_lm` for the editor |
| `NEWSNEST_PODCAST_EDITOR_MODEL` | `editor_model` | — | Local editorial model directory; empty = extractive fallback |
| `NEWSNEST_PODCAST_EDITOR_SCRIPT` | `editor_script` | `scripts/podcast-editor.py` | Editorial runner |
| `NEWSNEST_PODCAST_EDITOR_TIMEOUT_SECONDS` | `editor_timeout_seconds` | 900 | Hard editor subprocess timeout |
| `NEWSNEST_PODCAST_EDITOR_PROVIDER` | `editor_provider` | `local` | `local` (MLX, offline), `openai` (Responses API) or `codex` (explicit) |
| `NEWSNEST_PODCAST_EDITOR_API_MODEL` | `editor_api_model` | `gpt-6-luna` | Model used by the `openai`/`codex` providers |
| `NEWSNEST_PODCAST_EDITOR_CODEX_BINARY` | `editor_codex_binary` | `codex` | Codex CLI executable (never automatic) |
| `NEWSNEST_PODCAST_REF_AUDIO` | `ref_audio` | empty | Voice reference audio (only used with a text file) |
| `NEWSNEST_PODCAST_REF_TEXT_FILE` | `ref_text_file` | empty | Voice reference transcript (only used with audio) |
| `NEWSNEST_PODCAST_PRONUNCIATIONS_FILE` | `pronunciations_file` | empty | Flat JSON term → spoken replacement for TTS |
| `NEWSNEST_PODCAST_TEMPO` | `tempo` | 1.0 | TTS tempo multiplier (0.5–2.0) |
| `NEWSNEST_PODCAST_SEED` | `seed` | 0 | TTS sampling seed |

No personal path is hardcoded and no credential is ever read from the CLI or the config file. The
`openai` provider reads `OPENAI_API_KEY` from the inherited environment only; the app never
inspects or logs it, and availability of credits is not assumed. A configured editor that fails
(bad path, timeout, malformed or incomplete JSON, API error/refusal) fails generation and keeps the
previous episode; it never falls back to raw fragments. Missing paths fail into `status.json` and
are retried; they never break the web app or the feed scheduler. The reference audio/text pair is
only forwarded when both are set; the pronunciation map is forwarded when configured.

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

## Editorial runner

`scripts/podcast-editor.py` runs in the configured editor Python (the web environment does not
need MLX or a key). It receives UTF-8 JSON with a stable numeric id, title, snippet and publisher
per selected story, plus up to two bounded context items each; the context is retrieved only from
the local database for the previous 14 days when a story shares at least two meaningful title
components (named entities or non-generic keywords) after trailing publisher branding is removed.
A single broad entity or a shared outlet suffix such as " - Daily Market" is never enough, so zero
context is a normal, conservative result. Context URLs and dates are preserved in the episode's `sources` with
`"context": true`; `item_count` still counts only the fresh selected stories.

The **input root is a JSON array** and the **output is a JSON array** of `{"id", "text"}` covering
exactly ids `1..N` once each — this single contract is shared by the app and the runner. A model
that emits one `[{"segments": [...]}]` wrapper is tolerated on its raw completion only; anything
else fails. Local runs load `mlx_lm` with the Hub offline and call
`tokenizer.apply_chat_template(..., tokenize=False, add_generation_prompt=True,
enable_thinking=False)`, then `generate(..., sampler=make_sampler(temp=...), verbose=False)`;
the API branch calls `https://api.openai.com/v1/responses` with `store:false` and no temperature.
The cloud provider rewrites up to five stories per call; the small local model is called **one
story at a time** (the model is still loaded only once) because it drops ids when batching. Each
story gets 1–3 natural Spanish sentences as warranted, with no word quota: direct openers, brief
transitions, English summarized/translated, acronyms expanded, roles preserved, reported facts
separated from cautious interpretation, and no hashes, configuration inventories, URLs or source
boilerplate. The narration never talks about the input data or about details it lacks. Validation
guards spoken filler such as “el titular no detalla el alcance”, “no se especifica cómo ocurrió”,
“el contexto no precisa cómo funcionará” and the “la información citada/disponible no detalla …
” variants: a whole sentence matching the input-metadata pattern is conservatively removed before
validation, keeping every factual sentence in its original order and never rewriting facts or
decimals. A sentence mixing fact and filler is dropped whole, so a story can still fail; genuine
attributed uncertainty (“la fiscalía investiga si …”, “la empresa no informó …”) is kept. Tags,
URLs and hashes are rejected on the original completion before cleanup, so cleanup never acts as a
sanitizer that hides them. If nothing useful remains, the segment is rejected with a precise filler
or word-count reason. The length bound is a universal floor of 8 words (and a 60-word cap) for
every story, thin or rich: no story is padded with filler to reach a higher minimum. Numerals must
be grounded in the same story, not another story in the batch. A rejected completion gets at most
**one** concise corrective retry: each validation error names the failed id and rule (word count
with actual/min/max, input-metadata filler, tags/URL/hash, or invented numeric values) so the single
retry can repair the exact fault; no partial result is ever accepted. Parsing rejects duplicate JSON keys
(a local model emitted five repeated `id`/`text` pairs, which plain `json.loads` silently
collapsed). Local budget is 1400 tokens per story and the API budget is 4096 because reasoning
consumes output tokens. Every segment must be bounded, clean and complete; otherwise the run fails.
The result is atomically written to `--output` before TTS starts, so LLM and voice memory stay in
separate processes. The narration is not fact-checked, and cloud credits/keys are not assumed to
be configured.

The `codex` provider is an **explicit** third choice (never an automatic paid fallback): one
`codex exec` subprocess handles all selected stories in an isolated temporary cwd, with a strict
`{"segments": [...]}` output schema, subscription auth (API-key variables are removed from the
child environment), no shell/model tools, no web search and a 180-second cap. Its output goes
through the same strict validator; a validation failure gets exactly **one** corrective retry with
the same complete stories, the strict schema and the exact validation error, while an auth/quota
error fails generation and keeps the previous episode. Raw subprocess streams are never written to
status or the web UI. LaunchAgent installations can point `editor_codex_binary` at a small local
wrapper that exports an isolated `CODEX_HOME` before starting the official CLI; that runtime and
its login stay private and must already be valid. No bridge code is added to the public repository.

The prompt may cautiously explain stable general technical concepts when they help the listener,
but time-sensitive names, releases and concrete facts come only from the supplied articles and
related items. Old dated context is narrated as an antecedent or previous event, never as a
development of today and never with today's date. When comparing plan consumption units (tokens
versus credits or requests), the prompt must not invent per-token prices, per-unit costs or billing
the source does not state. A story with little beyond a headline is narrated in a short factual
sentence, never padded with invented dates or filler, and the prompt never names the input fields.
Historical context excludes hidden/muted items, generic capitalized openers, shared publisher
suffixes and single broad entities, and its URLs and publication dates are preserved in the
episode's metadata.

## TTS runner

`scripts/podcast-tts.py` runs in the configured Python (the web environment does not need
MLX). The caller forwards `--tempo` and `--seed` always, `--ref-audio`/`--ref-text-file` only
when both are configured, and `--pronunciations-file` when a flat term → spoken map is set. It:

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
* caps and topic/source diversity as visual metadata, never spoken;
* the editorial stage: clean segment validation, unique ids, a configured-editor failure that
  stops generation, per-story numeral grounding, the `make_sampler` API, the mocked OpenAI
  branch, TTS argv for the reference pair/pronunciations/tempo/seed, and `narration_mode`;
* bounded historical context (14-day cutoff, two shared components after publisher-brand
  stripping, brand-suffix and single-entity negatives, two-item cap, own URLs excluded) in
  `tests/test_podcast_context.py`;
* immutable versioned audio: a failed manifest write during same-day regeneration keeps the
  previous metadata and audio bytes, and the legacy `episode.mp3` of the first real episode
  resolves unchanged;
* idempotence, retry timing, failure atomicity and path traversal;
* authentication, strict version validation and byte-range serving of the audio endpoint;
* the `is_late`/`ready_at`/`expected_day` late status and the CLI refusing today and future
  dates;
* the scheduler start hour and catch-up behavior.
