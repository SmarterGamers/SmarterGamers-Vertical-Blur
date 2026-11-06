# Personal Clipper — status & handoff for Antigravity

_Last updated 2026-11-04 by Claude (branch `feat/n8n-personal-clipper`). Read this before touching anything
clipper-related on `desktop-gt79d30`. It is the single source of truth for what is built, what is running,
and what is still open. Keep it current when you change something._

## What this is

An n8n workflow that turns one of Brian's own stream recordings into a 30 s vertical Short, with Brian
approving the moment in Telegram before anything is rendered. n8n orchestrates; the existing repo code does
the work; local Ollama gives a second opinion; the Vertical Blur engine renders. **Nothing is ever posted
or uploaded automatically.**

```
Inbox file / webhook / manual
  → Guard (ledger, idempotent)  → Ingest (ffprobe, copy to Working)
  → Transcribe (faster-whisper, CUDA)  → Score (v1 bf6_clipper onsets + transcript)
  → Predict (hook / retention / clarity / length, 0-100)  → Ollama verdict (llama3.2, local)
  → Telegram review message  → poll getUpdates every 70 s, up to 24 h
  → reply "5" → Render (src\main.py, burned captions, NVENC)  → CapCut/EditPilot package
  → Telegram "Delivered"  → runs.jsonl + processed.json
```

## Hard rules Brian set (do not relax)

- Local Ollama only. No OpenAI / Anthropic / Google / any cloud LLM, no API keys, at any stage.
- Reuse existing code (`src/main.py`, `core/ffmpeg_engine.py`, v1 `bf6_clipper.py`). Never duplicate the renderer.
- Human approval in Telegram before every render. No auto-approve, no silent timeout approval.
- Never auto-post, auto-upload, or message anyone except Brian on the review channel.
- Own content only. Never add anything for clipping other creators.
- Secrets live only in the n8n Config node / credentials (Telegram token + chat id). Never in the repo,
  JSON exports, or scripts. `C:\SmarterGamers\deploy_workflow2.ps1` re-imports the workflow while
  preserving them — use it, do not import the JSON by hand.
- Don't break the existing CLI flags / encoders / modes. Every new dependency goes in `requirements.txt`.
- No official CapCut API exists; the package is files + instructions, not fake automation.

## What is running on desktop-gt79d30

| Piece | Where | Notes |
|---|---|---|
| Runner service | `src\clipper\runner.py` on `127.0.0.1:5690` | Windows scheduled task **"SmarterGamers Clipper Runner"** (S4U). Log: `C:\SmarterGamers\Clips\runner.log`. After pulling runner/scorer/predictor code changes it must be restarted from an **elevated** PowerShell: `Stop-ScheduledTask -TaskName "SmarterGamers Clipper Runner"; Start-ScheduledTask -TaskName "SmarterGamers Clipper Runner"`. A Claude session cannot do that (access denied). |
| Python venv | `C:\SmarterGamers\clipper-venv314` | Python 3.14. `faster-whisper` needs `av<19`. The miniconda 3.11 venv crashes on ctranslate2 — don't use it. |
| n8n | Docker `n8n` 2.44.6, `C:\projects\n8n`, UI `http://127.0.0.1:5678` | Workflow **Personal Clipper** id `0M20vSIKvhym4lI6` (active), error handler `nAHKdK91LQYHeGr3`. Reaches the runner as `host.docker.internal:5690`, Ollama as `host.docker.internal:11434`. `NODES_EXCLUDE` must not list the Local File Trigger. |
| Folders | `C:\SmarterGamers\Clips\{Inbox,Working,Ready}` | Ledger `processed.json`, log `runs.jsonl`. Ingest **moves** the source out of Inbox into `Working\<job_id>\source.mp4`. |
| Ollama | `llama3.2:latest` on the RTX 4060 | Only used for the verdict line and EditPilot prompt extras; heuristic fallback if down. |
| Telegram | bot "Personal Clipper Bot" | Plain `sendMessage` + `getUpdates` polling (sendAndWait rejects localhost URLs). `parse_mode: None` everywhere — underscores in paths break Markdown. |
| Helper scripts | `C:\SmarterGamers\*.ps1`, `C:\projects\n8n\data\*.js` | `deploy_workflow2.ps1` (redeploy), `render_pick.py` (render an already-reviewed rank directly, used when Brian approves in chat instead of Telegram), `exec_rows2.js` / `last_err.js` (inspect executions), `make_test_vod.ps1`. |

## Proven so far (Nov 4 2026)

- End-to-end through n8n on a real BF6 fights VOD: drop → review 19 s → reply → render 6 s → Delivered, ~2 min (exec #5928).
- Real 20-min OBS session slice (`OBS_2026-07-12_slice.mp4`): 5 real moments found, Brian approved #1 in
  Telegram, and #5 in chat. Outputs in `Ready\OBS_2026-07-12_slice_clip{1,5}_30s.mp4` + `_capcut\`.
- Chat-only VOD (`SAMPLE_highlights_montage.mp4`) correctly reports "nothing worth clipping" — after an
  earlier version mis-scored it and a boring menu clip got posted (Brian deleted it). Lesson: **watch the
  clip before calling it good.** Pull frames (`ffmpeg -vf "fps=1/5,scale=270:-1,tile=6x1"`) if you can't play it.

## Known limits / open items

1. Brian's OBS sessions are already 1080×1920 (TikTok layout), so the blur-pad render is a straight cut
   with captions. Clarity now gives vertical sources full marks (`source_already_vertical`).
2. The predictor can't see *visual* dead time: `clip5` scored 87 but its back half is a static trophy
   screen. A cheap frame-difference "static screen" penalty over the window would catch this. Not built.
3. Scoring tags any loud impact as "firefight" (v1 heuristic). Fine for ranking, but the review text
   says "gunfire/impact", not "firefight", on purpose.
4. Game name: `Config.GAME` (blank = "gameplay") feeds the social copy. Streams rotate games; nothing detects it.
5. No 16:9 session recordings exist yet to exercise the actual blur-pad path through the workflow (the
   engine's own tests cover it).
6. `/webhook/personal-clipper` on a file the Local File Trigger already grabbed fails with "file not found"
   (ingest moved it). Expected; don't double-trigger.

## How to test without Telegram

```powershell
# reset the ledger so the same file can run again, then drop/copy a file into Inbox (trigger) or:
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5678/webhook/personal-clipper -ContentType application/json `
  -Body '{"path":"C:\\SmarterGamers\\Clips\\Inbox\\TEST_bf6_fights.mp4"}'
# watch: docker exec n8n node /data/shared/exec_rows2.js | Select-String 0M20 | Select-Object -Last 1
# render an already-reviewed rank directly (only after Brian approved it):
C:\SmarterGamers\clipper-venv314\Scripts\python.exe C:\SmarterGamers\render_pick.py <job_id> <rank>
```

Unit tests (no ffmpeg needed): `python tests\test_clipper.py` and `python tests\test_predictor.py`.

## Nov 6 2026 additions
- **Stream layout**: 16:9 sources (Twitch VODs, OBS stream recordings) render as facecam-top / gameplay-centre / captions-under (`--layout stream`, auto-picked by the runner). Classic blur is unchanged for vertical sources. Constants in `src/core/ffmpeg_engine.py` (`STREAM_*`, `DEFAULT_FACECAM_BOX`); facecam box per channel in `Config FACECAM_BOX`.
- **Captions**: one reaction emoji per chunk (`captions.EMOJI_RULES`, rendered with Segoe UI Emoji — monochrome under libass; colour would need PNG overlays), profanity masked in captions and in the upload description, dropped from titles.
- **Auto mode**: `AUTO_MIN_SCORE` 75 (the day-after sweep is the safety net). Publish slots are de-collided by the runner (`_free_slot`).
- **Twitch**: `scripts/twitch_vods.py` downloads to `<Clips>/.twitch_tmp` (outside Inbox — the Inbox watcher is depth 0 and ignores dot-folders; earlier it fired on every download fragment). Set `SG_CLIPPER_INBOX=C:\SmarterGamers\Clips\Hold` to download without triggering a run. Twitch clips from VODs are UI-only (Helix create-clip is live-only): done through Chrome at the clipper's timestamps. Kick has no VOD clipping at all (clips only while live) and no VOD descriptions.
- **Deploy**: `C:\SmarterGamers\deploy_workflow2.ps1` / `deploy_sweep.ps1` now restart the n8n container — the running instance caches active workflows, a CLI import alone is NOT picked up.
- **Runner restart** is still Brian-only (elevated `Stop/Start-ScheduledTask "SmarterGamers Clipper Runner"`); any change under `src/clipper/` needs it, `scripts/*.py` and `src/main.py`/`src/core/*` do not (subprocess per render/upload).

## If you change something

Code → commit on `feat/n8n-personal-clipper` → `git pull` on the PC → restart the runner task (elevated) →
`deploy_workflow2.ps1` if the workflow JSON changed → run the two test files → update this document.
