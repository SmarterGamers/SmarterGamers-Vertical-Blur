# Personal Clipper — n8n setup (Windows)

An n8n workflow that watches a folder for your gameplay VODs, finds the five most promising
30-second windows, asks **you** on Telegram which one to render, renders it as a 9:16 Short with
SmarterGamers Vertical Blur, and leaves a CapCut-ready folder next to it. It never uploads or posts
anything.

```
VOD dropped in Inbox ─▶ guard ─▶ ingest ─▶ transcribe ─▶ score ─▶ predict ─▶ Ollama opinion
        ─▶ Telegram review message ─▶ (you reply "2" or "2 start 5s earlier" …)
        ─▶ render ─▶ CapCut package ─▶ Telegram "done" ─▶ runs.jsonl
```

Files in this folder:

| file | what |
|---|---|
| `personal-clipper-workflow.json` | the main workflow (import this) |
| `personal-clipper-error-handler.json` | small Error-Trigger workflow (import, then set as error workflow) |
| `docker-compose.patch.md` | exact lines to add to your compose file |
| `CONTRACT.md` | the HTTP contract between n8n and the Windows runner |

## What already exists on this PC

- **n8n 2.44.6** in Docker (`docker.n8n.io/n8nio/n8n:2.44.6`, container `n8n`), compose file at
  `C:\projects\n8n\docker-compose.yml`, UI at <http://127.0.0.1:5678>. `NODE_FUNCTION_ALLOW_BUILTIN=*`
  is already set.
- **Ollama** on the Windows host at `:11434` with `llama3.2:latest` pulled. From inside the
  container it is `http://host.docker.internal:11434`.
- This repo (the Vertical Blur editor, `src/main.py`) and the runner (`src/clipper/runner.py`).

What n8n can *not* do: run Windows binaries. Every heavy step (ffprobe, whisper, render) is an HTTP
call to the runner on `http://host.docker.internal:5690`.

## 1. Folders

```powershell
mkdir C:\SmarterGamers\Clips\Inbox, C:\SmarterGamers\Clips\Working, C:\SmarterGamers\Clips\Ready
```

## 2. docker-compose change

Follow `docker-compose.patch.md`. In short: add the bind mount `C:/SmarterGamers/Clips:/data/clips`,
add `NODES_EXCLUDE=[]` (n8n 2.x disables the Local File Trigger by default), then
`docker compose up -d`. Docker Desktop provides `host.docker.internal` on its own; only plain Docker
Engine needs the `extra_hosts` line.

## 3. Start the runner

```powershell
cd <repo>
python -m venv C:\SmarterGamers\clipper-venv
C:\SmarterGamers\clipper-venv\Scripts\pip install -r requirements.txt
C:\SmarterGamers\clipper-venv\Scripts\pip install faster-whisper
run_runner.bat                      # foreground, listens on 127.0.0.1:5690
```

Check it: `curl http://127.0.0.1:5690/health` should return `ok: true` with ffmpeg/ffprobe paths and
a `faster_whisper` version. To have it start at logon without a console window, run
`install_runner_task.ps1` once from an elevated PowerShell (it registers a Scheduled Task that runs
`run_runner.bat`).

From inside the container the same check is
`docker exec n8n wget -qO- http://host.docker.internal:5690/health`.

## 4. Telegram bot + chat id

1. In Telegram, open **@BotFather** → `/newbot` → pick a name and a username → copy the **token**.
2. Send any message to your new bot (it cannot message you first).
3. Get your chat id: open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read `"chat":{"id":123456789}`.
   (Or message **@userinfobot**.)

## 5. n8n credentials (names matter)

In n8n → *Credentials* → *Add*:

| credential type | name it exactly | used by |
|---|---|---|
| Telegram API | `Personal Clipper Bot` | every Telegram node in both workflows |
| Discord Bot API | `Personal Clipper Discord Bot` | only if you switch review to Discord (placeholder) |
| Gmail OAuth2 | `Personal Clipper Gmail` | only if you switch review to Email (placeholder) |

The workflow JSON references credentials by name only; after import, open each Telegram node once
and confirm the credential is selected.

## 6. Import the workflows

1. *Workflows* → *Create* → ⋯ → **Import from file** → `personal-clipper-workflow.json`.
2. Open the **Config** node and set `TELEGRAM_CHAT_ID`. Everything else has the CONTRACT.md
   defaults; change paths here and only here.
3. Import `personal-clipper-error-handler.json` the same way and set its **Error Config** →
   `TELEGRAM_CHAT_ID`. Save it.
4. Back in *Personal Clipper*: *Settings* (top right ⋯) → **Error workflow** → pick
   *Personal Clipper — Error Handler* → Save.
5. **Activate** *Personal Clipper* (toggle top right). The Local File Trigger and the webhook only
   run while the workflow is active. The error workflow only fires for these production runs, not
   for manual test runs.

## 7. First run, by hand

Option A — the Manual Trigger: set `Config.MANUAL_PATH` to a VOD that is already in
`C:\SmarterGamers\Clips\Inbox`, click **Execute workflow**. The execution pauses at *Telegram Review*
until you answer.

Option B — the webhook (works from the PC):

```powershell
curl -X POST http://127.0.0.1:5678/webhook/personal-clipper -H "Content-Type: application/json" -d "{\"path\": \"C:\\\\SmarterGamers\\\\Clips\\\\Inbox\\\\stream.mp4\"}"
```

Option C — just copy a `.mp4`/`.mkv`/`.mov` into `C:\SmarterGamers\Clips\Inbox`. The trigger polls,
waits for the write to finish, and starts a run. Files that are too short, not video, or already
processed are skipped by `/guard` (nothing is moved).

## 8. Reviewing on Telegram

You receive something like:

```
🎬 Personal Clipper — stream_2026-11-02.mkv (1:42:10)
#1  score 78/100  (hook 31 · retention 24 · clarity 15 · length 8)   0:02:03 → 0:02:33 ⭐ recommended
    why: loud reaction + "no way" at 0:03
    fix: cut the first 3 s — the hook starts at 0:04
    llm: "Strong hook, viewer hears the stakes immediately" ✅ agrees
#2  ...
Reply: a number (e.g. 2), "2 start 5s earlier", "2 end 3s later", or "reject all". No reply in 24 h = nothing rendered.
```

Tap **Respond** (n8n's free-text reply opens a tiny form) and type one of:

| reply | effect |
|---|---|
| `2` | render candidate #2 as proposed |
| `2 start 5s earlier` | #2, start moved 5 s earlier |
| `2 end 3s later` | #2, end moved 3 s later |
| `1 start 2s later end 4s later` | both edges shifted |
| `3 start -4` | signed number without a word: `-` = earlier, `+` = later |
| `reject all` | nothing rendered; working files kept in `Working\<job_id>\` |

Offsets are clamped to `0 … source duration`. If the reply does not match the grammar you get a
"could not parse" message and nothing is rendered. `⚠️ disagrees` means the local LLM rated the
clip differently from the heuristics; `(LLM unavailable: …)` means Ollama was down and the run
continued without it.

> The **Respond** button opens a page on your n8n instance. With n8n bound to `127.0.0.1:5678`
> that page only loads on the PC. To reply from your phone, set `WEBHOOK_URL` to a LAN IP or
> tunnel (see `docker-compose.patch.md` §3).

## 9. After the render

Telegram gets: output path, score, chosen start/end, encoder, and the CapCut package path.

`C:\SmarterGamers\Clips\Ready\<original>_clip2_30s.mp4` is the finished 1080×1920 Short with
safe-zone captions burned in. Next to it,
`C:\SmarterGamers\Clips\Ready\<original>_clip2_30s_capcut\` contains:

- the clip (copy), `captions.srt` (clip-relative), `transcript.json`
- `editpilot-prompts.md` — clip-specific prompts for CapCut's EditPilot
- `capcut-steps.md` — the manual steps
- `social.md` — titles / caption / hashtags

### Using the CapCut package

1. Open CapCut (desktop) → *New project* → import the clip from the package folder.
2. Open `editpilot-prompts.md`, pick the job you want (e.g. "tighten the hook", "add B-roll
   callouts") and paste that prompt into EditPilot.
3. Review the result on the timeline. If your CapCut version has EditPilot's manual-confirmation
   toggle, leave it on so nothing is applied without you seeing it.
4. Polish (captions style, SFX, end card) and export at **1080 × 1920**, 30 or 60 fps.
5. Upload wherever you like — by hand. The workflow does not post.

CapCut has no public API. EditPilot availability depends on your CapCut version, account and
region, and it may be a paid feature. Nothing here subscribes to or automates CapCut; the package
is just files you open.

## 10. runs.jsonl

Every delivered run appends one line to `C:\SmarterGamers\Clips\runs.jsonl`:

```json
{"job_id":"stream_20261102_213000","input":"C:\\...\\stream.mkv","candidates":[...],"approved":{"rank":2,"start":118.4,"end":148.4,"reply":"2 start 5s earlier"},"output":"C:\\...\\Ready\\stream_clip2_30s.mp4","package_dir":"C:\\...\\stream_clip2_30s_capcut","status":"delivered","ts":"..."}
```

Quick look in PowerShell:

```powershell
Get-Content C:\SmarterGamers\Clips\runs.jsonl | ConvertFrom-Json | Select-Object job_id, status, @{n='rank';e={$_.approved.rank}}, output | Format-Table
```

`processed.json` is the ledger of file hashes already handled; delete an entry (or the file) to
re-run a VOD.

## 11. Troubleshooting

| symptom | cause / fix |
|---|---|
| HTTP node error `ECONNREFUSED host.docker.internal:5690` | runner not running, or you are on plain Docker Engine without `extra_hosts`. Start `run_runner.bat`; check `docker exec n8n wget -qO- http://host.docker.internal:5690/health`. |
| Transcribe returns `fix: ... pip install faster-whisper` | `C:\SmarterGamers\clipper-venv\Scripts\pip install faster-whisper` then restart the runner. First run downloads the `small.en` model (~500 MB). |
| Transcribe says `device: cpu` and takes ages | CUDA build of ctranslate2 not found; CPU still works, which is why the node timeout is 30 min. |
| Local File Trigger does not appear in the node list | n8n 2.x disabled it. Add `NODES_EXCLUDE=[]` to the compose environment, `docker compose up -d`. |
| Trigger fires once, then never | host→container file events are flaky; the node already uses polling + awaitWriteFinish. Confirm the mount: `docker exec n8n ls /data/clips/Inbox`. |
| Nothing after 24 h | the review timed out: the execution resumes with an empty reply, sends "no reply in 24 h … nothing rendered", and ends. The working files stay in `Working\<job_id>\`. Re-run the VOD by removing its entry from `processed.json` and re-dropping it, or use the webhook. |
| `(LLM unavailable: …)` in the review | Ollama is off or the model is missing: `ollama pull llama3.2:latest`. The run continues without the LLM opinion. |
| Telegram "Respond" link does not open on the phone | n8n is only on 127.0.0.1. Reply from the PC, or set `WEBHOOK_URL` (compose patch §3). |
| Error workflow never fires | it only fires for active (production) executions, and only after you set it under *Settings → Error workflow*. |
| Guard says `already_processed` | the file hash is in `processed.json`. Remove the entry to re-run. |

## Honest notes

- The 0–100 "score" and the hook/retention/clarity numbers are **heuristics** (audio energy, onsets,
  transcript exclamations, pacing) plus a local LLM's opinion. They are not predictions of views.
- CapCut has no public API; EditPilot may be unavailable or paid depending on version/account/region.
  Nothing is subscribed to or automated there.
- The workflow never posts to TikTok, YouTube or anywhere else. It writes files and sends you
  Telegram messages. Publishing is yours.
