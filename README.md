# HKUST EV Charging Collector

Collects HKUST EV charging data every 30 seconds from two APIs and writes append-only CSV files.

- Third-party charger API (`/portal/api/thirdparty/v1`): charger and connector status used for session tracking.
- Portal dashboard API (`/portal/api/api/v2/quickInfo`): live power/voltage/current/energy and fleet-level dashboard metrics.

## Current Outputs

| File | Source | Contents |
|---|---|---|
| `charging_sessions.csv` | Third-party API | Session transitions (start/end) with charger, connector, tariff, and location metadata |
| `charging_live.csv` | Portal quickInfo API | Per-connector live snapshot (`kw`, `kwh`, `voltages`, `currents`, `status`, timestamps, locId) |
| `charging_dashboard.csv` | Portal quickInfo API | Fleet summary counts and energy/duration for yesterday, today, and last 7 days |
| `collector_state.json` | Local state file | Persisted in-memory session state for restart/crash recovery |

## Architecture

`collector.py` runs two async loops:

1. `api_loop()`
- Fetches full charger list from third-party API (`/charger`).
- Derives connector-level records.
- Tracks transitions using `SessionTracker` and writes session rows.

2. `portal_loop()`
- Authenticates with portal API (`POST /authenticate`) using `PORTAL_USERNAME`/`PORTAL_PASSWORD`.
- Polls `GET /v2/quickInfo`.
- Writes one `charging_live.csv` row per connector and one `charging_dashboard.csv` row per poll.

## Restart/Crash Safety

`SessionTracker` is persisted to disk in `collector_state.json`.

- On startup, state is loaded from disk if present.
- On every transition update, state is written atomically (`.tmp` then replace).
- This prevents duplicate session-start rows after service restarts while connectors are still charging.

## Files

```
collector.py              Main async collector (dual-loop: third-party API + portal API)
requirements.txt          Python dependencies
.env.example              Template - copy to .env and fill secrets
restarter.sh              Crash-restart wrapper called by systemd
ev-collector.service      systemd user service unit
deploy.conf.example       Server connection template (copy to deploy.conf, gitignored)
scripts/
  setup_git_remote.ps1    One-time Windows setup: SSH key + first push
  server_init.sh          One-time server setup: bare repo + post-receive hook
  bootstrap_server.sh     Manual fallback bootstrap
  update_and_restart.sh   Manual update fallback
  _portal_probe.py        Endpoint discovery probe for portal API (diagnostic)
  _quickinfo_probe.py     quickInfo shape/field probe (diagnostic)
```

## Diagnostic Probes

Two probe scripts are intentionally kept in the repo as diagnostics:

- `scripts/_portal_probe.py`
  - Tries likely endpoint paths under `/portal/api/api`.
  - Prints HTTP status and sampled JSON path shapes.
  - Use this when endpoints move, are versioned, or access control changes.

- `scripts/_quickinfo_probe.py`
  - Authenticates and fetches `/v2/quickInfo`.
  - Prints connector count, key set, charging subset, and sample records.
  - Use this to detect payload shape drift before changing `collector.py` extractors.

Rationale:
- The production collector uses direct API polling (no browser automation).
- These probes provide a fast, repeatable way to rediscover endpoint and schema changes.
- Keeping them in git preserves operational knowledge and speeds incident response.

Example usage on CEZ083:

```bash
cd ~/hkust-ev-collector
python3 scripts/_portal_probe.py
python3 scripts/_quickinfo_probe.py
```

## Quick Start

### 1. Prerequisites (Windows)

- Git for Windows, Python, OpenSSH client (`ssh`, `ssh-keygen`) on `PATH`.
- Optional local test setup:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Prepare local config files

```powershell
# Connection details for CEZ083 (gitignored)
Copy-Item deploy.conf.example deploy.conf

# Runtime secrets (gitignored)
Copy-Item .env.example .env
```

Fill `.env` with:

```env
API_KEY=<thirdparty_api_key>
PORTAL_USERNAME=<portal_username>
PORTAL_PASSWORD=<portal_password>
```

### 3. First-time server setup + push

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_git_remote.ps1
```

The setup script and post-receive hook will:

- Create/configure remote bare repo.
- Check out to `~/hkust-ev-collector`.
- Install/update Python dependencies.
- Install/update user systemd service.
- Restart collector service after each push.

After first deployment, edit server `.env` once:

```bash
ssh -p <server_port> <server_user>@<server_host> "nano ~/hkust-ev-collector/.env"
```

### 4. Subsequent updates

```powershell
git add -A
git commit -m "your message"
git push
```

## Service Management (CEZ083)

```bash
# Status
systemctl --user status ev-collector.service

# Logs (live)
journalctl --user -u ev-collector.service -f

# Restart
systemctl --user restart ev-collector.service

# Stop
systemctl --user stop ev-collector.service
```

## CSV Schemas

Files are created in `~/hkust-ev-collector/`.

### charging_sessions.csv

```csv
timestamp_utc,charger_id,connector_id,status,session_start_utc,session_end_utc,charger_status,charger_type,charger_point_model,charge_point_serial_number,charge_box_serial_number,is_enabled,boot_dttm_utc,last_status_dttm_utc,bay_no,connector_name,connector_type,connector_status,connector_status_last_updated_utc,connector_max_output_kw,connector_expected_end_utc,reservation_flag,rsr_status,rsr_id,tariff_max_charging_duration_mins,tariff_max_charging_unit,tariff_max_penalty_unit,tariff_gracing_period,tariff_gracing_period_unit,location_loc_id,location_address,location_station_code,location_contact_number,source
```

### charging_live.csv

```csv
timestamp_utc,charger_id,connector_id,current_A,voltage_V,power_kW,power_kW_est,energy_kWh,soc_pct,status,tran_start_utc,tran_stop_utc,last_update_utc,loc_id,source_endpoint
```

### charging_dashboard.csv

```csv
timestamp_utc,n_charging,n_available,n_unavailable,yesterday_kwh,yesterday_duration_s,today_kwh,today_duration_s,last7d_kwh,last7d_duration_s
```

## Export CSV to Local Machine

```powershell
scp -P <server_port> <server_user>@<server_host>:~/hkust-ev-collector/charging_sessions.csv .
scp -P <server_port> <server_user>@<server_host>:~/hkust-ev-collector/charging_live.csv .
scp -P <server_port> <server_user>@<server_host>:~/hkust-ev-collector/charging_dashboard.csv .
scp -P <server_port> <server_user>@<server_host>:~/hkust-ev-collector/collector_state.json .
```

## Troubleshooting

| Symptom | Check |
|---|---|
| `Missing required environment variables` on startup | Verify `~/hkust-ev-collector/.env` contains `API_KEY`, `PORTAL_USERNAME`, `PORTAL_PASSWORD` |
| Service not starting after push | `journalctl --user -u ev-collector.service -n 100 --no-pager` |
| No live rows in `charging_live.csv` | Check logs for `Portal auth attempt` or `Portal cycle failed` |
| Duplicate session starts after restart | Ensure `collector_state.json` exists and is writable by service user |
| SSH push rejected | Confirm public key exists in remote `~/.ssh/authorized_keys` |
| API changed unexpectedly | Run `scripts/_portal_probe.py` and `scripts/_quickinfo_probe.py` to remap endpoints and fields |
