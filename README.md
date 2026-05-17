# ocean-bitcoin-monitor

Monitors Ocean.xyz mining rigs and sends Telegram alerts when a rig goes offline. Tracks cumulative uptime for SLA verification.

## How it works

- Runs every 5 minutes via cron on a VPS
- Scrapes your ocean.xyz stats page (no public API exists)
- Alerts you on Telegram for state transitions only (no spam)
- Logs every poll to `uptime_log.jsonl` for audit trail

## Alert conditions

| Event | Trigger |
|---|---|
| Rig offline | No share submitted for >15 min |
| Rig recovered | Rig comes back online after being marked offline |
| Still offline | Follow-up at 2hr, 6hr, 24hr, 48hr marks |
| Daily digest | Auto-summary every morning at 08:00 AM ET |

## Bot commands

Send these to your Telegram bot (response arrives within 5 minutes):

| Command | What it does |
|---|---|
| `/status` or `/ping` | Current rig status and hashrate |
| `/uptime` | 30-day uptime % vs. 90% guarantee threshold |
| `/history` | Last 10 offline events with timestamps and durations |
| `/help` | List all available commands |

## Setup

### 1. Telegram bot

1. Open Telegram → search `@BotFather` → `/newbot` → follow prompts → copy token
2. Send any message to your new bot
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `id` value from `"chat"` — that's your chat ID

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your token, chat ID, and wallet address
```

### 3. Install & test

```bash
pip install -r requirements.txt
python3 monitor.py --debug   # prints parsed worker data, no alerts sent
```

### 4. Go live (cron)

```bash
# Create a virtualenv (recommended on Ubuntu/Debian)
python3 -m venv venv
venv/bin/pip install -r requirements.txt

crontab -e
```

Add (adjust path to match your setup):

```
*/5 * * * * /path/to/ocean-bitcoin-monitor/venv/bin/python3 /path/to/ocean-bitcoin-monitor/monitor.py >> /path/to/ocean-bitcoin-monitor/logs/monitor.log 2>&1
```

## Uptime reports

Data is logged to `uptime_log.jsonl` (one line per rig per poll). Run reports locally:

```bash
python3 report.py                   # last 30 days
python3 report.py --month 2026-05   # specific month
python3 report.py --days 7          # last 7 days
python3 report.py --since 2026-05-01
```

Example output:

```
=== Uptime Report: 2026-05-01 to 2026-05-31 ===
Monitoring period: 31 days (8,928 polls/rig, 5-min intervals)

Worker: worker-1
  Online:  8,640 polls  (96.8%)  — 720 hours
  Offline:   288 polls  ( 3.2%)  —  24 hours
  Status:  ✅ Above 90% guarantee

Worker: worker-2
  Online:  7,200 polls  (80.7%)  — 600 hours
  Offline: 1,728 polls  (19.3%)  — 144 hours
  Status:  ❌ BELOW 90% guarantee
           Budget: 892 polls offline allowed (10% of 8,928)
           Actual: 1,728 polls offline (836 polls / 69.7 hours over budget)
```

## Alert thresholds

Edit `.env` to tune:

| Variable | Default | Meaning |
|---|---|---|
| `OFFLINE_THRESHOLD_MINUTES` | `15` | Alert if no share for this many minutes |

## Development & testing

```bash
pip install -r requirements-dev.txt
pytest test_monitor.py -v
```

65 tests, zero network calls. Covers all parsing, scraping, state logic, alert conditions, uptime tracking, and bot commands.

## Troubleshooting

```bash
# View recent logs
tail -50 logs/monitor.log

# Check current state
cat state.json

# Test a specific command response
venv/bin/python3 monitor.py --debug

# Trigger a recovery alert for testing
python3 -c "
import json
s = json.load(open('state.json'))
for k in [k for k in s if isinstance(s[k], dict) and 'online' in s[k]]:
    s[k]['online'] = False
json.dump(s, open('state.json', 'w'), indent=2)
print('Set all workers offline — run monitor.py to trigger recovery alerts')
"
```
