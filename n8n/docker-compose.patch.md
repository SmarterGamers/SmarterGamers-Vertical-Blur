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

## 3. (Optional) WEBHOOK_URL so the Telegram "Respond" button works from your phone

Telegram *Send and Wait* with a free-text reply posts a **Respond** button that opens a small
n8n-hosted form. Your phone must be able to reach n8n at that URL. With n8n bound to
`127.0.0.1:5678` that only works from the PC itself. Either answer from the PC's browser, or expose
n8n on the LAN / a tunnel and set:

```yaml
      - WEBHOOK_URL=http://<your-pc-lan-ip>:5678/     # ADD only if you want to reply from your phone
```

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
