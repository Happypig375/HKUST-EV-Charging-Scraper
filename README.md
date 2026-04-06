# HKUST EV Charging Collector

Polls the HKUST EV charging REST API and the portal web interface every 30 seconds,
writing two append-only CSV files:

| File | Contents |
|---|---|
| `charging_sessions.csv` | Session start/end times per charger |
| `charging_live.csv` | Live current (A) and voltage (V) captured from portal XHR traffic |

---

## Files

```
collector.py              Main async collector (dual-loop: API + Playwright)
requirements.txt          Python dependencies
.env.example              Template — copy to .env and fill in secrets
restarter.sh              Crash-restart wrapper called by systemd
ev-collector.service      systemd user-service unit
deploy.conf.example       Server connection template (copy to deploy.conf, gitignored)
scripts/
  setup_git_remote.ps1    One-time Windows setup: SSH key + push
  server_init.sh          One-time server setup: bare repo + post-receive hook
  bootstrap_server.sh     Manual fallback bootstrap (if not using git push)
  update_and_restart.sh   Manual update fallback (if not using git push)
```

---

## Quick start

### 1 — Prerequisites (Windows machine)

- Git for Windows, Python 3.12, OpenSSH client (`ssh`, `ssh-keygen`) all on `PATH`.
- Install Python deps locally if you want to test before pushing:
  ```powershell
  python -m venv .venv
  .venv\Scripts\Activate.ps1
  pip install -r requirements.txt
  ```

### 2 — Create your local credential files

```powershell
# Connection details for CEZ083 (gitignored)
Copy-Item deploy.conf.example deploy.conf
# Then open deploy.conf and confirm the values match CEZ083:
#   SERVER_HOST=143.89.22.207  SERVER_PORT=830  SERVER_USER=user

# Runtime secrets — only these three lines matter
Copy-Item .env.example .env
# Open .env and set:
#   API_KEY=<thirdparty_api_key>
#   PORTAL_USERNAME=<portal_username>
#   PORTAL_PASSWORD=<portal_password>
```

> `.env` and `deploy.conf` are both gitignored and will never be committed.

### 3 — One-time server setup + first push

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_git_remote.ps1
```

This script:
1. Generates an ed25519 SSH key pair on your machine (skips if one exists).
2. Copies the public key to CEZ083 (one authentication prompt).
3. Pipes `server_init.sh` to CEZ083 over SSH, which creates a bare git repo,
   installs the `post-receive` hook, and enables systemd user linger.
4. Adds `origin` to your local git config and pushes.

The **post-receive hook** runs automatically on every push and:
- Checks out code to `~/hkust-ev-collector`
- Installs / updates Python dependencies
- Installs Playwright Chromium
- Copies `ev-collector.service` into `~/.config/systemd/user/`
- Restarts the systemd user service

After the first push, **edit `.env` on the server once**:
```bash
ssh -p <server_port> <server_user>@<server_host> "nano ~/hkust-ev-collector/.env"
```

### 4 — Subsequent updates

```powershell
git add -A
git commit -m "your message"
git push
```

That's it. The hook handles deployment and service restart automatically.

---

## Service management (on CEZ083)

```bash
# SSH in
ssh -p <server_port> <server_user>@<server_host>

# Status
systemctl --user status ev-collector

# Logs (live)
journalctl --user -u ev-collector -f

# Restart manually
systemctl --user restart ev-collector

# Stop
systemctl --user stop ev-collector
```

---

## CSV output

Both files are created in `~/hkust-ev-collector/` on the server.

**`charging_sessions.csv`**
```
timestamp_utc, charger_id, connector_id, status, session_start_utc, session_end_utc, source
```

**`charging_live.csv`**
```
timestamp_utc, charger_id, connector_id, current_A, voltage_V, source_endpoint
```

Copy files back to your Windows machine:
```powershell
scp -P <server_port> <server_user>@<server_host>:~/hkust-ev-collector/charging_sessions.csv .
scp -P <server_port> <server_user>@<server_host>:~/hkust-ev-collector/charging_live.csv .
```

---

## Telemetry discovery

The collector intercepts portal XHR/fetch responses and scans them for current/voltage fields.
Until the internal API schema is confirmed, every candidate JSON endpoint and its top-level keys
are logged at `INFO` level:

```
Telemetry candidate url=https://... keys=['chargerId', 'outputCurrent', 'outputVoltage', ...]
```

Check logs to identify the correct field names, then they are extracted automatically if they
match any of the built-in key aliases in `collector.py → Config.current_keys / voltage_keys`.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `Missing required environment variables` on start | `~/hkust-ev-collector/.env` is missing or incomplete |
| Service not starting after push | `journalctl --user -u ev-collector -n 50` |
| Playwright fails headless on Linux | Ensure `python -m playwright install-deps chromium` was run (needs sudo once) |
| SSH push rejected | Confirm `~/.ssh/authorized_keys` on server contains your public key |
| `charging_live.csv` has no rows | Check `Telemetry candidate` log lines to find the correct JSON field names |
