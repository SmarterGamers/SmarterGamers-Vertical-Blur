# Personal Clipper — Runner ↔ n8n contract (v1)

n8n runs in Docker (`n8nio/n8n:2.44.6`, container `n8n`, 127.0.0.1:5678). It cannot run
Windows binaries, so every heavy step is an HTTP call to a small Windows-side **runner**
(`src/clipper/runner.py`, stdlib `http.server`, bound to `127.0.0.1:5690`). From inside the
container the runner is `http://host.docker.internal:5690` and Ollama is
`http://host.docker.internal:11434`.

All requests/responses are JSON. Every error response is `{"ok": false, "stage": "<stage>",
"error": "<human message>", "fix": "<exact command or action, or null>"}` with HTTP 500.
Every success is `{"ok": true, ...}`. Paths are Windows paths as the runner sees them.

Config lives in ONE place on the n8n side (the `Config` Set node) and ONE place on the
runner side (`src/clipper/config.py`, env-overridable). Defaults:

| key | default |
|---|---|
| INBOX | `C:\SmarterGamers\Clips\Inbox` |
| WORKING | `C:\SmarterGamers\Clips\Working` |
| READY | `C:\SmarterGamers\Clips\Ready` |
| LEDGER | `C:\SmarterGamers\Clips\processed.json` |
| RUN_LOG | `C:\SmarterGamers\Clips\runs.jsonl` |
| RUNNER_URL | `http://host.docker.internal:5690` |
| OLLAMA_URL | `http://host.docker.internal:11434` |
| OLLAMA_MODEL | `nemotron-3-nano:4b` (2.8 GB; benchmarked Nov 2026 against llama3.2, qwen3:4b, gemma4:e2b on real candidates — best titles/hooks at the lowest VRAM; llama3.2 remains a valid fallback) |
| TARGET_LEN_SEC | `30` (15 / 60 allowed) |
| REVIEW_CHANNEL | `telegram` (discord / email swappable) |
| REVIEW_TIMEOUT_HOURS | `24` |
| CAPTURES / CONTAINER_CAPTURES | `C:\Users\ultim\Videos\Captures` / `/data/captures` — second watched folder (ShadowPlay/Game Bar instant replays). Files there are never moved: ingest hard-links (or copies) them into Working |
| AUTO_POST | `true` — skip the text review: render every "clip it" moment plus heuristic picks ≥ AUTO_MIN_SCORE that pass the visual check (max AUTO_MAX_PER_DAY, non-overlapping), upload each as a **scheduled** Short at the next AUTO_SLOTS time in AUTO_TZ (private until then — pull it in Studio), and send the clip video + link to Telegram. `false` = the two-step text review. |
| AUTO_MAX_PER_DAY / AUTO_MIN_SCORE / AUTO_SLOTS / AUTO_TZ | `4` / `75` / `10:00,13:00,16:00,19:00` / `America/New_York` (75: the day-after sweep is the safety net; the runner enforces the slots + a 4/day cap across ALL runs via `PUBLISH_SLOTS`/`PUBLISH_TZ`/`MAX_PER_DAY`) |
| GAME | `""` — game name for the social copy; blank = "gameplay" (set per session if you want it in titles) |
| BF6_CLIPPER_PATH | `C:\Users\ultim\Desktop\Projects\HistoryView.org\bf6_clipper.py` |
| REPO_DIR | repo root (auto) |
| PYTHON_EXE | the venv python running the runner |

## Endpoints

### GET /health
→ `{"ok":true,"ffmpeg":"<path>","ffprobe":"<path>","faster_whisper":"<ver>|null","bf6_clipper":bool,"model":"<whisper model or null>"}`

### POST /guard   {"path": "<file in Inbox>"}
Stage 0. → `{"ok":true,"skip":false}` or `{"ok":true,"skip":true,"reason":"too_short|not_video|already_processed"}`.
Hash = sha1 of (size, mtime, first 1 MB). Idempotent: a processed hash always returns skip.

### POST /ingest   {"path": "<file>"}
Stage 1. Moves the file to `WORKING\<job_id>\source.<ext>` (job_id = `<stem>_<YYYYmmdd_HHMMSS>`),
ffprobe → `{"ok":true,"job_id","job_dir","source","duration_sec","width","height","fps","audio_tracks":[{"index","codec","channels"}]}`.
Track layout is never changed.

### POST /transcribe   {"job_id": "..."}
Stage 2. faster-whisper, word timestamps. Writes `transcript.json` (`{"segments":[{"start","end","text","words":[{"start","end","word"}]}]}`)
and `transcript.srt` in the job dir. → `{"ok":true,"transcript":"<path>","srt":"<path>","words":N,"model":"small.en","device":"cuda|cpu"}`.
If faster-whisper or the model is missing → error with `fix: "C:\\SmarterGamers\\clipper-venv\\Scripts\\pip install faster-whisper"` (or the model download hint). NEVER returns ok without words unless the audio truly has no speech (then `words:0, "note":"no speech detected"` and the workflow still shows that in review).

### POST /score   {"job_id": "...", "target_len": 30}
Stage 3. Returns top-5 windows:
`{"ok":true,"candidates":[{"rank":1,"start":123.4,"end":153.4,"duration":30.0,"score":0.81,"reason":"loud reaction + 'no way' at 0:03","signals":{"audio_energy":..,"onsets":..,"speech_density":..,"exclaim":..,"penalties":[...]}}]}`.
Signals: v1 `bf6_clipper` onsets (imported by path, optional), RMS energy, transcript exclamations/questions/payoff words, pacing, penalties for mid-sentence starts / leading silence / no audio.

### POST /predict   {"job_id": "...", "candidates": [...]}
Stage 4. Per candidate adds `"predict": {"total":0-100,"hook":0-40,"retention":0-30,"clarity":0-20,"length_fit":0-10,"lulls":[{"start","end"}],"biggest_fix":"..."}` and returns `"recommended": <rank>`.

### POST /ollama_verdict   {"job_id": "...", "candidates": [...]}
Runner-side helper (so n8n doesn't parse LLM output): calls Ollama `/api/chat` with `format: "json"`, 60 s timeout, per candidate. Adds `"llm": {"verdict","fix","hook_line","agrees": bool}` or `"llm": null, "llm_error": "<msg>"`. Never raises on LLM failure.
(n8n may instead call Ollama directly via HTTP Request; both paths produce the same shape.)

### POST /render   {"job_id": "...", "rank": 2, "start": 118.4, "end": 148.4, "captions": true, "offset": -120, "audio_mode": "mix_all", "volume": 3.0}
Stage 6. Writes `captions.ass` (safe-zone, word-highlight, one reaction emoji per chunk, profanity masked) from transcript words for [start,end], then runs
`python src\main.py "<source>" --preset shorts --start S --duration D --audio-mode mix_all --volume V --offset O --hwaccel --layout L [--facecam x,y,w,h] --captions "<captions.ass>" -o "<job_dir>"`.
Layout (`Config LAYOUT`, default `auto`): a landscape source (16:9 stream VOD / OBS recording) gets `stream` — facecam cut from `FACECAM_BOX` (fractions of the frame, default `0.03,0.50,0.31,0.43`) on top, the centre 4:3 of the gameplay filling the width below it (the overlay's own facecam rectangle blurred out), captions under the gameplay panel; a vertical source keeps the classic `blur` letterbox. The response carries `layout`.
renames the result to `READY\<original>_clip<rank>_<len>s.mp4`. → `{"ok":true,"output":"<path>","cli":"<exact command>","encoder":"h264_nvenc|..."}`.

### POST /package   {"job_id": "...", "rank": 2, "output": "<path>", "candidate": {...}}
Stage 6B. Creates `READY\<original>_clip<rank>_<len>s_capcut\` with: the rendered clip (copied), `captions.srt` (clip-relative), `transcript.json`, `editpilot-prompts.md` (Ollama, clip-specific, heuristic fallback text if Ollama fails), `capcut-steps.md`, `social.md` (titles/caption/hashtags from the existing `smartergamers-clip` webhook if reachable). → `{"ok":true,"package_dir":"<path>","files":[...]}`.

### POST /log   {"job_id","input","candidates","approved","output","package_dir","status"}
Stage 7. Appends one JSON line to RUN_LOG and marks the hash processed in LEDGER. → `{"ok":true}`.

## Review message format (built by n8n Code node)
```
🎬 Personal Clipper — <original filename> (<duration>)
#1  score 78/100  (hook 31 · retention 24 · clarity 15 · length 8)   0:02:03 → 0:02:33
    why: loud reaction + "no way" at 0:03
    fix: cut the first 3 s — the hook starts at 0:04
    llm: "Strong hook, viewer hears the stakes immediately" ✅ agrees
#2 ...
Reply: a number (e.g. 2), several ("1 and 5" renders both), "2 start 5s earlier", "2 end 3s later", "no" (or "reject all"). No reply in 24 h = nothing rendered.
```
`POST /upload {job_id, output, rank, candidate, privacy, game, publish_at}` → `{video_id, url, title, privacy, publish_at, studio_url}` — posts ONE rendered clip. Titles come from Ollama/the hook and are scrubbed (profanity dropped, "Live Stream:" prefixes removed) in `scripts/yt_post.py`; the description's spoken quote is masked. `publish_at` is moved later (90-min steps) when `runs.jsonl` already has a Short within 45 min of it, so two VODs processed the same day never share a slot to YouTube Shorts via the Shorts pipeline's own uploader/token (`scripts/yt_post.py` run in that venv). Only called after an explicit "post" reply. `POST /package` now also returns `preview` (the render, or a ≤45 MB 720p re-encode) for the Telegram video message.

Review has two phases: (1) pick a candidate → render; (2) the clip is sent as a Telegram video with its stats → reply `post` / `post 2` / `post all` / `keep`. Timeout in phase 2 keeps the clip in Ready, unposted.

`POST /sweep {dry_run, floor_fraction, min_age_hours, max_age_hours}` → `{baseline_views, checked, privatized, kept, skipped}` — day-after sweep (scripts/yt_sweep.py, manage token): Shorts the clipper posted 20–48 h ago that have < floor_fraction × (median views of recent Shorts) AND 0 likes are set to private (never deleted). Driven by `n8n/personal-clipper-sweep.json` (daily 09:00 ET, Telegram summary).

`POST /twitch_sync {channel, max, dry_run}` → `{started, log}` — downloads new Twitch past broadcasts of the channel into Inbox (scripts/twitch_vods.py, yt-dlp, ledger twitch_vods.json) in the background; the Inbox watcher then runs the normal flow. Daily 07:00 ET from the sweep workflow.

`POST /claim_reply {update_id, job_id}` → `{claimed: bool, owner}` — first-come claim of a Telegram update so concurrent reviews never act on the same message (claims in `reply_claims.json`, 48 h). A Telegram reply-to (quote) of a review is accepted only by that review and needs no claim. n8n polls getUpdates with offset 0 (never confirms) so one review cannot hide a message from another.

Voice command: saying **"clip it"** (also "clip that" / "clip this") on stream makes the 30 s before the words a candidate — always listed first, always the recommendation, marked `signals.voice_command {text, at}`. The command itself is left out of the clip (window ends 0.3 s before it). Several commands = several candidates on top of the usual five.

Reply grammar parsed by n8n: `^(\d)(?:\s*(?:start|end)\s*([+-]?\d+(?:\.\d+)?)\s*s?\s*(earlier|later)?)*$|^(?:reject all|no|none|skip|skip all|reject)$` (case-insensitive).

`POST /restart {"pull": true|false}` → `{"restarting": true, "head", "pulled"}` — optionally `git pull --ff-only`s the repo, then the runner exits with code 3; `scripts/deploy_runner.ps1` relaunches it with `Start-ScheduledTask` (works non-elevated for the same user; Task Scheduler's restart-on-failure only covers trigger-started runs). `GET /health` now returns `head` (git) and `started` (process start time) so a deploy can confirm the new process is up. `scripts/deploy_runner.ps1` does restart + wait; no elevated shell needed.
