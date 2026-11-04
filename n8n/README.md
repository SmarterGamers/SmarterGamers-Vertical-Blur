# Personal Clipper — n8n setup (Windows)

An n8n workflow that watches a folder for your gameplay VODs, finds the five most promising
30-second windows, asks **you** on Telegram which one to render, renders it as a 9:16 Short with
SmarterGamers Vertical Blur, and leaves a CapCut-ready folder next to it. It never uploads or posts
anything.

```
VOD dropped in Inbox ─▶ guard ─▶ ingest ─▶ transcribe ─▶ score ─▶ predict ─▶ Ollama opinion
        ─▶ Telegram review message ─▶ (you reply "2" or "2 start 5s earlier" … as a plain chat message)
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
4. Do **not** set a webhook for this bot (no n8n *Telegram Trigger*); the workflow reads your
   replies with `getUpdates`, which Telegram refuses while a webhook exists.

## 5. n8n credentials (names matter)

In n8n → *Credentials* → *Add*:

| credential type | name it exactly | used by |
|---|---|---|
| Telegram API | `Personal Clipper Bot` | every Telegram node in both workflows (the `getUpdates` poll uses `Config.TELEGRAM_BOT_TOKEN` instead, see §8) |
| Discord Bot API | `Personal Clipper Discord Bot` | only if you switch review to Discord (placeholder) |
| Gmail OAuth2 | `Personal Clipper Gmail` | only if you switch review to Email (placeholder) |

The workflow JSON references credentials by name only; after import, open each Telegram node once
and confirm the credential is selected.

## 6. Import the workflows

1. *Workflows* → *Create* → ⋯ → **Import from file** → `personal-clipper-workflow.json`.
2. Open the **Config** node and set `TELEGRAM_CHAT_ID` and `TELEGRAM_BOT_TOKEN` (the BotFather
   token, used by the `getUpdates` poll — see §8). Everything else has the CONTRACT.md defaults;
   change paths here and only here.
3. Import `personal-clipper-error-handler.json` the same way and set its **Error Config** →
   `TELEGRAM_CHAT_ID`. Save it.
4. Back in *Personal Clipper*: *Settings* (top right ⋯) → **Error workflow** → pick
   *Personal Clipper — Error Handler* → Save.
5. **Activate** *Personal Clipper* (toggle top right). The Local File Trigger and the webhook only
   run while the workflow is active. The error workflow only fires for these production runs, not
   for manual test runs.

## 7. First run, by hand

Option A — the Manual Trigger: set `Config.MANUAL_PATH` to a VOD that is already in
`C:\SmarterGamers\Clips\Inbox`, click **Execute workflow**. The execution loops at *Wait 70s* →
*Telegram: getUpdates* until you answer in the chat.

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

Just answer in the chat, as a normal message to the bot (no button, no form), with one of:

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

### How the reply reaches n8n (and why there is no "Respond" button)

n8n's Telegram *Send and Wait* node posts a **Respond** button whose link has to be a public
URL — Telegram's Bot API rejects `localhost` / `127.0.0.1` links with `Wrong HTTP URL`, and this
n8n is deliberately bound to `127.0.0.1:5678`. So the workflow does not use Send-and-Wait at all:

- **Telegram: Send Review** is a plain `sendMessage`.
- The execution then loops **Wait 70s → Telegram: getUpdates → Find Reply** until it sees a
  message from `Config.TELEGRAM_CHAT_ID` that is newer than the review message, or until
  `Config.REVIEW_TIMEOUT_HOURS` has passed. `getUpdates` is an outbound HTTPS call from the
  container to `api.telegram.org`; nothing has to reach n8n.
- You can reply from your phone anywhere; the reply is picked up on the next poll, so expect
  up to ~70 s before the render starts. If you send several messages, the newest one counts.
- While it waits, the execution shows as **Waiting** in the executions list with its node data saved
  (n8n persists a Wait of 65 s or more; a shorter wait would sleep inside the process and look like
  a stuck "Running" execution with no data). A persisted wait also survives an n8n restart.

Two things this needs:

1. **`Config.TELEGRAM_BOT_TOKEN`** must hold the BotFather token. n8n's HTTP Request node cannot
   read the token out of the *Telegram API* credential (the token sits in the URL path, and
   `$credentials` is not available in expressions), so the poll node takes it from `Config`.
   The shipped JSON has the placeholder `REPLACE_WITH_BOT_TOKEN`. It is the same token you put
   in the credential; the `Config` node is inside your local n8n database only.
2. **The bot must have no webhook set.** `getUpdates` returns HTTP 409 while a webhook is
   registered. This setup never uses an n8n *Telegram Trigger* (which would register one). If you
   ever did, or used the bot elsewhere, clear it once:
   `https://api.telegram.org/bot<TOKEN>/deleteWebhook` in a browser.

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
| Nothing after 24 h | the review timed out: the poll loop reached `REVIEW_TIMEOUT_HOURS`, sent "no reply in 24 h … nothing rendered", and ended. The working files stay in `Working\<job_id>\`. Re-run the VOD by removing its entry from `processed.json` and re-dropping it, or use the webhook. |
| `(LLM unavailable: …)` in the review | Ollama is off or the model is missing: `ollama pull llama3.2:latest`. The run continues without the LLM opinion. |
| Review message arrives but my reply is ignored | `Config.TELEGRAM_BOT_TOKEN` still says `REPLACE_WITH_BOT_TOKEN` (the poll gets 404 `Not Found`), the reply came from a different chat than `TELEGRAM_CHAT_ID`, or the bot has a webhook set (`getUpdates` → 409 — open `https://api.telegram.org/bot<TOKEN>/deleteWebhook` once). Open the execution and look at the *Telegram: getUpdates* output; *Find Reply* also puts the API error into `poll_error`. |
| Reply takes a while to act | expected: the loop polls every 70 s. |
| Execution sits at "Waiting" for hours | that is the review window: nobody has replied yet. Answer in the Telegram chat, or wait for `REVIEW_TIMEOUT_HOURS` to send the "nothing rendered" notice. |
| Error workflow never fires | it only fires for active (production) executions, and only after you set it under *Settings → Error workflow*. |
| Guard says `already_processed` | the file hash is in `processed.json`. Remove the entry to re-run. |

## Honest notes

- The 0–100 "score" and the hook/retention/clarity numbers are **heuristics** (audio energy, onsets,
  transcript exclamations, pacing) plus a local LLM's opinion. They are not predictions of views.
- CapCut has no public API; EditPilot may be unavailable or paid depending on version/account/region.
  Nothing is subscribed to or automated there.
- The workflow never posts to TikTok, YouTube or anywhere else. It writes files and sends you
  Telegram messages. Publishing is yours.
