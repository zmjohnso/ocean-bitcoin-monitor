# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Run all tests (65 tests, no network calls)
pytest test_monitor.py -v

# Run a single test class or test
pytest test_monitor.py::TestEvaluateAndAlert -v
pytest test_monitor.py::TestEvaluateAndAlert::test_online_to_offline_sends_red_alert -v

# Debug mode — scrapes live data and prints worker info, no alerts sent
python3 monitor.py --debug

# Uptime reports (reads from uptime_log.jsonl)
python3 report.py                   # last 30 days
python3 report.py --month 2026-05   # specific month
python3 report.py --days 7
python3 report.py --since 2026-05-01
```

## Architecture

Single-file application: all monitor logic lives in `monitor.py`, deployed as a cron job that runs every 5 minutes.

**Data flow per cron run (`main()`):**
1. `fetch_workers()` — scrapes `ocean.xyz/stats/{wallet}` with BeautifulSoup (no public API exists); returns `None` on error (exits 0 to suppress cron email spam)
2. `log_uptime()` — appends one JSONL line per worker to `uptime_log.jsonl`
3. `process_commands()` — polls Telegram `getUpdates` and replies to `/status`, `/uptime`, `/history`; drains the backlog silently on first run to avoid replaying old messages
4. `evaluate_and_alert()` — fires Telegram alerts only on state transitions (online→offline, offline→online)
5. `check_outage_reminders()` — sends follow-up alerts at 2h/6h/24h/48h marks for sustained outages
6. `maybe_send_digest()` — sends a daily summary after 08:00 UTC (once per day, guarded by `last_digest_date` in state)
7. `save_state()` — writes `state.json` (per-worker online status + `offline_since`, `last_reminder_hrs`, `last_update_id`, `last_digest_date`)

**`report.py`** is a standalone CLI tool that reads `uptime_log.jsonl` directly to compute SLA compliance (90% guarantee threshold).

**Break-even pricing:** `estimate_daily_btc(wallet, hashrate_ths)` is the daily-BTC source for the calc — it prefers Ocean's own "Estimated Earnings Per Day" (`fetch_daily_earnings_sats()`, already nets out Ocean's pool fee and tx-fee revenue), and falls back to a theoretical hashrate/difficulty calc (`expected_daily_btc()` × `fetch_network_stats()`, which pulls difficulty/block height from blockchain.info — block reward is derived from height, so it self-corrects at the next halving) if the Ocean fetch fails or no wallet is given. `compute_breakeven()` combines that daily-BTC figure with `POWER_DRAW_WATTS`/`ELECTRICITY_RATE_PER_KWH` into a break-even BTC price; `fetch_btc_price()` (CoinGecko, no key) supplies the current price for comparison. The `/breakeven` Telegram command uses the live hashrate from that poll's `fetch_workers()` call; the daily digest uses a trailing 24h average via `compute_avg_hashrate()`, which reads the hashrate now logged in `uptime_log.jsonl`. When the fallback path is used, the message label notes "(fallback estimate)" so it's visible when accuracy has degraded. Both are skipped (silently in the digest, with an explicit message for `/breakeven`) if `POWER_DRAW_WATTS`/`ELECTRICITY_RATE_PER_KWH` aren't set or both estimate sources fail.

**Persistent files:**
- `state.json` — current online/offline state per worker; includes outage timestamps and Telegram update cursor
- `uptime_log.jsonl` — append-only audit log, one JSON record per worker per poll: `{"ts": "...", "worker": "...", "online": bool, "hashrate_3hr": float}`

**HTML scraping details** (see `fetch_workers()` docstring): worker rows have class `table-row`; the Total aggregate row is excluded by checking whether the href contains a `.worker_id` suffix after the wallet address. Worker names are derived from the href suffix, not the link text.

**Testing:** `test_monitor.py` uses `monkeypatch` to redirect `STATE_FILE` and `UPTIME_LOG` to `tmp_path`, patches `monitor.requests.get` for network isolation, patches `monitor.send_telegram` to assert alert behavior, and uses `freezegun` for time-sensitive tests (daily digest).

## Configuration

Copy `.env.example` to `.env` and set:
- `TELEGRAM_TOKEN` — from @BotFather
- `TELEGRAM_CHAT_ID` — numeric ID from `getUpdates`
- `WALLET` — Ocean.xyz wallet address
- `OFFLINE_THRESHOLD_MINUTES` (default: 15) — minutes since last share before marking offline
- `HASHRATE_DROP_THRESHOLD` (default: 0.25) — currently read but not acted upon in alert logic
- `POWER_DRAW_WATTS`, `ELECTRICITY_RATE_PER_KWH` — optional; enable the `/breakeven` command and the digest break-even line
