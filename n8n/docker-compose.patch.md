# docker-compose.yml changes for the Personal Clipper

File: `C:\projects\n8n\docker-compose.yml`. Do **not** rewrite the file — add the lines below
to the existing `n8n` service. Your current file already has `volumes:` with `./data:/data/shared`
and one read-only bind mount, and `environment:` with `NODE_FUNCTION_ALLOW_BUILTIN=*`; keep those.

## 1. Bind-mount the clips folder (required)

Under the `n8n` service's existing `volumes:` list, add one line:

```yaml
    volumes:
      - ./data:/data/shared                 # (already there)
      # ... your existing read-only mount   # (already there)
      - C:/SmarterGamers/Clips:/data/clips  # ADD — Inbox/Working/Ready become /data/clips/* in the container
      - C:/Users/ultim/Videos/Captures:/data/captures:ro  # ADD (optional) — instant-replay captures, watched in place (read-only; the runner hard-links them)
```

Forward slashes are correct for Docker Desktop on Windows. The runner on the host still uses
`C:\SmarterGamers\Clips\...`; only the Local File Trigger looks at `/data/clips/Inbox`.

## 2. Enable the Local File Trigger node (required on n8n 2.x)

n8n 2.x ships with `localFileTrigger` and `executeCommand` disabled by default. Under the
existing `environment:` list add:

```yaml
    environment:
      - NODE_FUNCTION_ALLOW_BUILTIN=*       # (already there)
      - NODES_EXCLUDE=[]                    # ADD — re-enables Local File Trigger (and Execute Command)
```

If you would rather keep Execute Command off, use `- NODES_EXCLUDE=["n8n-nodes-base.executeCommand"]`
instead.

## 3. WEBHOOK_URL — not needed

The review reply is read by polling Telegram's `getUpdates` from inside the container, so n8n
needs no public URL and can stay on `127.0.0.1:5678`. Leave `WEBHOOK_URL` unset.

## 4. host.docker.internal (only if you are NOT on Docker Desktop)

Docker Desktop for Windows provides `host.docker.internal` automatically — skip this. On plain
Docker Engine (Linux / WSL-only installs) add to the `n8n` service:

```yaml
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

## Apply

```powershell
cd C:\projects\n8n
docker compose up -d
docker compose logs -f n8n   # Ctrl+C when it says "Editor is now accessible"
```

Verify the mount from inside the container:

```powershell
docker exec n8n ls /data/clips
# expected: Inbox  Ready  Working  (create them on the host first if missing)
```
