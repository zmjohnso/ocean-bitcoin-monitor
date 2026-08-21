#!/usr/bin/env python3
"""
Ocean.xyz Bitcoin mining rig monitor.
Polls the stats dashboard every run, sends Telegram alerts on state transitions.
Run via cron every 5 minutes.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
UPTIME_LOG = BASE_DIR / "uptime_log.jsonl"
LOG_FILE = BASE_DIR / "logs" / "monitor.log"
LOG_FILE.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def parse_hashrate(text: str) -> float:
    """Parse '661.2 Th/s' → 661.2. Returns 0.0 on failure."""
    try:
        return float(text.strip().split()[0])
    except (ValueError, IndexError):
        return 0.0


def parse_last_share_minutes(text: str) -> float | None:
    """
    Parse 'YYYY-MM-DD HH:MM' timestamp → minutes since that time (UTC assumed).
    Returns None if unparseable.
    """
    text = text.strip()
    if not text or text.lower() in ("-", "never", "n/a"):
        return None
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
        # Ocean displays times in UTC
        dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        return delta.total_seconds() / 60
    except ValueError:
        log.warning("Could not parse last-share timestamp: %r", text)
        return None


def fetch_workers(wallet: str) -> list[dict] | None:
    """
    Scrape ocean.xyz stats page and return a list of worker dicts:
      { name, last_share_minutes, hashrate_60s, hashrate_3hr, is_online }
    Returns None on fetch/parse error (caller should skip the alert cycle).

    HTML structure (as of 2026-05):
      Each worker row: <tr class="table-row">
        td.hide-overflow > a  →  worker name (href ends in .{worker_id})
        td.table-cell         →  status div (div.status-online-text or status-offline-text)
        td.date-text          →  last share timestamp "YYYY-MM-DD HH:MM"
        td.table-cell [3]     →  60s hashrate "661.2 Th/s"
        td.table-cell [4]     →  3hr hashrate "661.2 Th/s"
    """
    url = f"https://ocean.xyz/stats/{wallet}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to fetch stats page: %s", e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Data rows have class "table-row"; the Total aggregate row links to the bare wallet address
    rows = soup.find_all("tr", class_="table-row")
    if not rows:
        log.error("No table-row elements found on stats page")
        return None

    workers = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        # Name: from the link href — take the part after the last "."
        name_link = cells[0].find("a")
        if not name_link:
            continue
        href = name_link.get("href", "")
        name = name_link.get_text(strip=True)
        # Skip the Total row (href is bare wallet, no ".worker_id" suffix)
        if "." not in href.split("/stats/", 1)[-1]:
            continue
        # Use just the worker suffix
        name = href.rsplit(".", 1)[-1] if "." in href else name

        # Status: look for status div classes
        status_div = cells[1].find("div", class_=lambda c: c and "status-" in c)
        if status_div:
            is_online = "online" in " ".join(status_div.get("class", []))
        else:
            is_online = "online" in cells[1].get_text(strip=True).lower()

        # Last share timestamp
        last_share_text = cells[2].get_text(strip=True)
        last_share_minutes = parse_last_share_minutes(last_share_text)

        # Hashrate columns
        hashrate_60s = parse_hashrate(cells[3].get_text(strip=True))
        hashrate_3hr = parse_hashrate(cells[4].get_text(strip=True))

        # Fall back to last-share time for online determination if status div missing
        if not status_div and last_share_minutes is not None:
            offline_threshold = float(os.getenv("OFFLINE_THRESHOLD_MINUTES", "15"))
            is_online = last_share_minutes <= offline_threshold

        workers.append({
            "name": name,
            "last_share_minutes": last_share_minutes,
            "hashrate_60s": hashrate_60s,
            "hashrate_3hr": hashrate_3hr,
            "is_online": is_online,
        })

    log.info("Found %d worker(s): %s", len(workers), [w["name"] for w in workers])
    return workers


def parse_daily_earnings_sats(soup: BeautifulSoup) -> int | None:
    """Extract 'Estimated Earnings Per Day' from a parsed stats page → satoshis."""
    for container in soup.find_all("div", class_="blocks"):
        label = container.find("div", class_="blocks-label")
        if label and "Estimated Earnings Per Day" in label.get_text():
            span = container.find("span", recursive=False)
            if span:
                try:
                    btc = float(span.get_text(strip=True).split()[0])
                    return round(btc * 100_000_000)
                except (ValueError, IndexError):
                    return None
    return None


def fetch_daily_earnings_sats(wallet: str) -> int | None:
    """Fetch the stats page and return estimated daily earnings in satoshis."""
    url = f"https://ocean.xyz/stats/{wallet}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Failed to fetch earnings: %s", e)
        return None
    return parse_daily_earnings_sats(BeautifulSoup(resp.text, "html.parser"))


def expected_daily_btc(hashrate_ths: float, difficulty: float, block_reward: float) -> float:
    """Expected BTC/day for a given hashrate at current network difficulty."""
    hashrate_hs = hashrate_ths * 1e12
    return hashrate_hs * 86400 * block_reward / (difficulty * 2**32)


def fetch_network_stats() -> dict | None:
    """Fetch current network difficulty and block height, derive block reward."""
    try:
        difficulty = float(requests.get(
            "https://blockchain.info/q/getdifficulty", headers=HEADERS, timeout=10
        ).text)
        block_height = int(requests.get(
            "https://blockchain.info/q/getblockcount", headers=HEADERS, timeout=10
        ).text)
    except requests.RequestException as e:
        log.warning("Failed to fetch network stats: %s", e)
        return None
    except (ValueError, TypeError) as e:
        log.warning("Could not parse network stats response: %s", e)
        return None

    block_reward = 50 / 2 ** (block_height // 210_000)
    return {
        "difficulty": difficulty,
        "block_height": block_height,
        "block_reward": block_reward,
    }


def fetch_btc_price() -> float | None:
    """Fetch current BTC/USD price from CoinGecko."""
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["bitcoin"]["usd"])
    except (requests.RequestException, KeyError, TypeError, ValueError) as e:
        log.warning("Failed to fetch BTC price: %s", e)
        return None


def estimate_daily_btc(wallet: str | None, hashrate_ths: float) -> tuple[float | None, str]:
    """Estimate BTC/day, preferring Ocean's own estimate (already nets out pool fees and
    tx-fee revenue) and falling back to a theoretical hashrate/difficulty calc if that
    fetch fails or no wallet is given. Returns (daily_btc, source)."""
    if wallet:
        sats = fetch_daily_earnings_sats(wallet)
        if sats is not None:
            return sats / 100_000_000, "ocean"

    network_stats = fetch_network_stats()
    if network_stats is not None:
        daily_btc = expected_daily_btc(hashrate_ths, network_stats["difficulty"], network_stats["block_reward"])
        return daily_btc, "theoretical"

    return None, "unavailable"


def compute_breakeven(daily_btc: float, power_draw_watts: float, rate_per_kwh: float) -> dict:
    """Daily electricity cost and the BTC price needed to break even, given an
    already-estimated daily BTC yield (see estimate_daily_btc)."""
    daily_cost = power_draw_watts / 1000 * 24 * rate_per_kwh
    breakeven_price = daily_cost / daily_btc if daily_btc > 0 else None
    return {
        "expected_daily_btc": daily_btc,
        "daily_cost": daily_cost,
        "breakeven_price": breakeven_price,
    }


def format_breakeven_message(
    breakeven: dict, hashrate_ths: float, current_price: float | None, label: str
) -> str:
    lines = [f"⚖️ Break-even — {label}"]
    lines.append(f"  Hashrate: {fmt_hashrate(hashrate_ths)}")

    breakeven_price = breakeven["breakeven_price"]
    if breakeven_price is None:
        lines.append("  Break-even price: unavailable (no hashrate)")
        return "\n".join(lines)

    lines.append(f"  Break-even price: ${breakeven_price:,.2f}/BTC")
    lines.append(f"  Daily electricity cost: ${breakeven['daily_cost']:,.2f}")

    if current_price is None:
        lines.append("  Current BTC price: unavailable")
    else:
        margin = current_price - breakeven_price
        emoji = "✅" if margin >= 0 else "❌"
        lines.append(f"  Current BTC price: ${current_price:,.2f}")
        lines.append(f"  {emoji} Margin: ${margin:+,.2f}/BTC")

    return "\n".join(lines)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read state file, starting fresh")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_telegram(message: str, token: str, chat_id: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Telegram alert sent: %s", message[:80])
    except requests.RequestException as e:
        log.error("Failed to send Telegram alert: %s", e)


def fmt_hashrate(ths: float) -> str:
    return f"{ths:.1f} Th/s"


def log_uptime(workers: list[dict]) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with UPTIME_LOG.open("a") as f:
        for w in workers:
            f.write(json.dumps({
                "ts": ts,
                "worker": w["name"],
                "online": w["is_online"],
                "hashrate_3hr": w["hashrate_3hr"],
            }) + "\n")


def compute_uptime(since: datetime, until: datetime) -> dict:
    """Read uptime_log.jsonl, return { worker: { online: int, total: int } } for [since, until]."""
    stats: dict[str, dict] = {}
    if not UPTIME_LOG.exists():
        return stats
    with UPTIME_LOG.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                dt = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
                if not (since <= dt <= until):
                    continue
                w = r["worker"]
                if w not in stats:
                    stats[w] = {"online": 0, "total": 0}
                stats[w]["total"] += 1
                if r["online"]:
                    stats[w]["online"] += 1
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return stats


def compute_avg_hashrate(since: datetime, until: datetime) -> float:
    """Read uptime_log.jsonl, return the average combined hashrate (Th/s) across all
    workers over [since, until], computed as the mean of each poll's worker sum."""
    if not UPTIME_LOG.exists():
        return 0.0

    per_poll_sum: dict[str, float] = {}
    with UPTIME_LOG.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                dt = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
                if not (since <= dt <= until):
                    continue
                per_poll_sum[r["ts"]] = per_poll_sum.get(r["ts"], 0.0) + r.get("hashrate_3hr", 0.0)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

    if not per_poll_sum:
        return 0.0
    return sum(per_poll_sum.values()) / len(per_poll_sum)


def format_uptime_report(stats: dict, since: datetime, until: datetime) -> str:
    days = max(1, (until - since).days)
    lines = [f"📈 Uptime Report — Last {days} Days\n"]
    if not stats:
        lines.append("No uptime data recorded yet.")
    for worker, s in sorted(stats.items()):
        total, online = s["total"], s["online"]
        if total == 0:
            lines.append(f"  {worker}: No data")
            continue
        pct = online / total * 100
        offline_hrs = (total - online) * 5 / 60
        if pct >= 90:
            lines.append(f"  {worker}: {pct:.1f}% ✅  (offline {offline_hrs:.1f} hrs)")
        else:
            over_hrs = ((total - online) - int(total * 0.10)) * 5 / 60
            lines.append(
                f"  {worker}: {pct:.1f}% ❌  (offline {offline_hrs:.1f} hrs — {over_hrs:.1f} hrs over 90% budget)"
            )
    period = f"{since.strftime('%Y-%m-%d')} to {until.strftime('%Y-%m-%d')}"
    sample_total = next(iter(stats.values()))["total"] if stats else 0
    lines.append(f"\nGuarantee threshold: 90%")
    lines.append(f"Period: {period}")
    if sample_total:
        lines.append(f"Polls: {sample_total:,} per rig")
    return "\n".join(lines)


REMINDER_THRESHOLDS = [2, 6, 24, 48]


def check_outage_reminders(
    workers: list[dict], state: dict, token: str, chat_id: str
) -> dict:
    new_state = dict(state)
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    for w in workers:
        name = w["name"]
        entry = new_state.get(name, {})
        if entry.get("online", True):
            continue

        # Seed offline_since if missing (rig was offline before this feature was deployed)
        if "offline_since" not in entry:
            entry = {**entry, "offline_since": now_iso, "last_reminder_hrs": 0}
            new_state[name] = entry

        offline_since = datetime.fromisoformat(entry["offline_since"].replace("Z", "+00:00"))
        hours_offline = (now - offline_since).total_seconds() / 3600
        last_reminded = entry.get("last_reminder_hrs", 0)

        due = [t for t in REMINDER_THRESHOLDS if hours_offline >= t and t > last_reminded]
        if not due:
            continue

        threshold = max(due)
        since_str = offline_since.strftime("%Y-%m-%d %H:%M UTC")
        hrs = int(hours_offline)
        hrs_str = f"{hrs} hour{'s' if hrs != 1 else ''}"
        mins = f"{w['last_share_minutes']:.0f}" if w["last_share_minutes"] is not None else "unknown"
        msg = (
            f"🔴 Still offline: {name}\n"
            f"  Down for {hrs_str} (since {since_str})\n"
            f"  Last share: {mins} min ago"
        )
        send_telegram(msg, token, chat_id)
        new_state[name] = {**entry, "last_reminder_hrs": threshold}

    return new_state


def get_offline_history(n: int = 10) -> list[dict]:
    """Derive offline events from uptime_log.jsonl over the last 30 days."""
    since = datetime.now(timezone.utc) - timedelta(days=30)
    per_worker: dict[str, list] = {}

    if not UPTIME_LOG.exists():
        return []

    with UPTIME_LOG.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                dt = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
                if dt < since:
                    continue
                worker = r["worker"]
                if worker not in per_worker:
                    per_worker[worker] = []
                per_worker[worker].append({"dt": dt, "online": r["online"]})
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

    events = []
    for worker, recs in per_worker.items():
        recs.sort(key=lambda r: r["dt"])
        event_start = None
        prev_online = None

        for rec in recs:
            if prev_online is None:
                prev_online = rec["online"]
                if not rec["online"]:
                    event_start = rec["dt"]
                continue
            if prev_online and not rec["online"]:
                event_start = rec["dt"]
            elif not prev_online and rec["online"] and event_start is not None:
                events.append({
                    "worker": worker,
                    "start": event_start,
                    "end": rec["dt"],
                    "duration_mins": (rec["dt"] - event_start).total_seconds() / 60,
                })
                event_start = None
            prev_online = rec["online"]

        if event_start is not None:
            events.append({
                "worker": worker,
                "start": event_start,
                "end": None,
                "duration_mins": (datetime.now(timezone.utc) - event_start).total_seconds() / 60,
            })

    events.sort(key=lambda e: e["start"], reverse=True)
    return events[:n]


def format_history(events: list[dict]) -> str:
    if not events:
        return "📋 No offline events recorded yet.\n(Tracking since monitor was deployed)"

    lines = [f"📋 Offline History (last {len(events)} events)\n"]
    for e in events:
        start_str = e["start"].strftime("%Y-%m-%d %H:%M UTC")
        mins = e["duration_mins"]
        dur = f"{int(mins // 60)} hr {int(mins % 60)} min" if mins >= 60 else f"{int(mins)} min"
        if e["end"] is None:
            lines.append(f"{e['worker']} — ongoing since {start_str}  ({dur})")
        else:
            end_str = e["end"].strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"{e['worker']} — {start_str} → {end_str}  ({dur})")

    tracking_since = None
    try:
        with UPTIME_LOG.open() as f:
            first = json.loads(f.readline().strip())
            tracking_since = first["ts"][:10]
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    if tracking_since:
        lines.append(f"\n(Tracking since: {tracking_since})")
    return "\n".join(lines)


def maybe_send_digest(
    workers: list[dict],
    state: dict,
    token: str,
    chat_id: str,
    wallet: str | None = None,
    power_draw_watts: float | None = None,
    rate_per_kwh: float | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    now_et = now.astimezone(ZoneInfo("America/New_York"))
    today = now_et.strftime("%Y-%m-%d")

    if now_et.hour < 8 or state.get("last_digest_date") == today:
        return state

    new_state = dict(state)
    stats = compute_uptime(now - timedelta(hours=24), now)

    lines = [f"☀️ Daily Digest — {today}\n"]

    if wallet:
        sats = fetch_daily_earnings_sats(wallet)
        if sats is not None:
            lines.append(f"  Estimated daily earnings: {sats:,} sats\n")

    if power_draw_watts is not None and rate_per_kwh is not None:
        avg_hashrate = compute_avg_hashrate(now - timedelta(hours=24), now)
        daily_btc, source = estimate_daily_btc(wallet, avg_hashrate)
        if daily_btc is not None:
            breakeven = compute_breakeven(daily_btc, power_draw_watts, rate_per_kwh)
            current_price = fetch_btc_price()
            label = "24h avg" if source == "ocean" else "24h avg (fallback estimate)"
            lines.append(format_breakeven_message(breakeven, avg_hashrate, current_price, label))
            lines.append("")

    for worker in sorted(stats):
        s = stats[worker]
        total, online = s["total"], s["online"]
        if total == 0:
            continue
        pct = online / total * 100
        offline_hrs = (total - online) * 5 / 60
        if pct >= 90:
            detail = f"{pct:.1f}% uptime  ({'no outages' if offline_hrs == 0 else f'{offline_hrs:.1f} hrs offline'})"
            lines.append(f"  {worker}: 🟢 {detail}")
        else:
            lines.append(f"  {worker}: 🔴 {pct:.1f}% uptime  ({offline_hrs:.1f} hrs offline)")

    if not stats:
        lines.append("  No uptime data recorded yet.")

    online_count = sum(1 for w in workers if w["is_online"])
    lines.append(f"\nMonitor: ✅ Running  ({online_count}/{len(workers)} rigs online)")
    send_telegram("\n".join(lines), token, chat_id)

    new_state["last_digest_date"] = today
    return new_state


def get_telegram_updates(token: str, offset: int | None) -> list[dict]:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json().get("result", [])
    except requests.RequestException as e:
        log.warning("Failed to fetch Telegram updates: %s", e)
        return []


def format_status(workers: list[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["📊 Ocean Bitcoin Monitor — Status"]
    for w in workers:
        if w["is_online"]:
            mins = f"{w['last_share_minutes']:.0f}" if w["last_share_minutes"] is not None else "?"
            lines.append(
                f"  {w['name']}: 🟢 Online | 60s: {fmt_hashrate(w['hashrate_60s'])} | Last share: {mins} min ago"
            )
        else:
            mins = f"{w['last_share_minutes']:.0f}" if w["last_share_minutes"] is not None else "?"
            lines.append(f"  {w['name']}: 🔴 OFFLINE | Last share: {mins} min ago")
    lines.append(f"Checked: {now}")
    return "\n".join(lines)


def process_commands(
    token: str,
    chat_id: str,
    workers: list[dict],
    state: dict,
    power_draw_watts: float | None = None,
    rate_per_kwh: float | None = None,
    wallet: str | None = None,
) -> dict:
    new_state = dict(state)

    if "last_update_id" not in state:
        # First run: drain backlog silently so we don't replay old messages
        updates = get_telegram_updates(token, offset=None)
        if updates:
            new_state["last_update_id"] = max(u["update_id"] for u in updates)
        else:
            new_state["last_update_id"] = 0
        return new_state

    updates = get_telegram_updates(token, offset=state["last_update_id"] + 1)
    if not updates:
        return new_state

    new_state["last_update_id"] = max(u["update_id"] for u in updates)

    for update in updates:
        msg = update.get("message", {})
        if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
            continue
        text = (msg.get("text") or "").strip().lower().split()[0] if msg.get("text") else ""
        if text in ("/status", "/ping"):
            send_telegram(format_status(workers), token, chat_id)
        elif text == "/uptime":
            until = datetime.now(timezone.utc)
            since = until - timedelta(days=30)
            stats = compute_uptime(since, until)
            send_telegram(format_uptime_report(stats, since, until), token, chat_id)
        elif text == "/history":
            send_telegram(format_history(get_offline_history(10)), token, chat_id)
        elif text == "/breakeven":
            if power_draw_watts is None or rate_per_kwh is None:
                send_telegram(
                    "Break-even calc not configured — set POWER_DRAW_WATTS and "
                    "ELECTRICITY_RATE_PER_KWH in .env",
                    token,
                    chat_id,
                )
            else:
                hashrate_ths = sum(w["hashrate_3hr"] for w in workers)
                daily_btc, source = estimate_daily_btc(wallet, hashrate_ths)
                if daily_btc is None:
                    send_telegram("⚖️ Break-even — Live\n  Daily earnings estimate unavailable", token, chat_id)
                else:
                    breakeven = compute_breakeven(daily_btc, power_draw_watts, rate_per_kwh)
                    current_price = fetch_btc_price()
                    label = "Live" if source == "ocean" else "Live (fallback estimate)"
                    send_telegram(
                        format_breakeven_message(breakeven, hashrate_ths, current_price, label),
                        token, chat_id,
                    )
        elif text == "/help":
            send_telegram(
                "Available commands:\n"
                "  /status — current online/offline state of all rigs\n"
                "  /uptime — 30-day uptime report\n"
                "  /history — last 10 offline events\n"
                "  /breakeven — live break-even BTC price\n"
                "  /help — show this message",
                token,
                chat_id,
            )

    return new_state


def evaluate_and_alert(
    workers: list[dict],
    state: dict,
    token: str,
    chat_id: str,
    offline_threshold: float,
) -> dict:
    new_state = dict(state)

    for w in workers:
        name = w["name"]
        prev = state.get(name, {"online": True, "hashrate_ok": True})

        # --- Offline / recovery check ---
        if not w["is_online"] and prev["online"]:
            mins = f"{w['last_share_minutes']:.0f}" if w["last_share_minutes"] is not None else "unknown"
            msg = (
                f"🔴 Rig OFFLINE: {name}\n"
                f"  Last share: {mins} minutes ago\n"
                f"  Expected hashrate: ~{fmt_hashrate(w['hashrate_3hr'])}"
            )
            send_telegram(msg, token, chat_id)
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            new_state[name] = {**prev, "online": False, "offline_since": now_iso, "last_reminder_hrs": 0}

        elif w["is_online"] and not prev["online"]:
            mins = f"{w['last_share_minutes']:.0f}" if w["last_share_minutes"] is not None else "unknown"
            msg = (
                f"🟢 Rig RECOVERED: {name}\n"
                f"  Back online (last share {mins} min ago)\n"
                f"  Current hashrate: {fmt_hashrate(w['hashrate_60s'])}"
            )
            send_telegram(msg, token, chat_id)
            entry = {**prev, "online": True}
            entry.pop("offline_since", None)
            entry.pop("last_reminder_hrs", None)
            new_state[name] = entry

        # Ensure state entry always exists
        if name not in new_state:
            new_state[name] = {"online": w["is_online"]}

    return new_state


def main() -> None:
    load_dotenv(BASE_DIR / ".env")

    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    wallet = os.getenv("WALLET", "")
    offline_threshold = float(os.getenv("OFFLINE_THRESHOLD_MINUTES", "15"))
    drop_threshold = float(os.getenv("HASHRATE_DROP_THRESHOLD", "0.25"))
    power_draw_watts = os.getenv("POWER_DRAW_WATTS")
    power_draw_watts = float(power_draw_watts) if power_draw_watts else None
    rate_per_kwh = os.getenv("ELECTRICITY_RATE_PER_KWH")
    rate_per_kwh = float(rate_per_kwh) if rate_per_kwh else None

    if not all([token, chat_id, wallet]):
        log.error("Missing required env vars: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, WALLET")
        sys.exit(1)

    debug_mode = "--debug" in sys.argv

    workers = fetch_workers(wallet)
    if workers is None:
        log.error("Skipping alert cycle due to fetch error")
        sys.exit(0)  # exit 0 so cron doesn't spam error emails

    log_uptime(workers)

    if debug_mode:
        print("\n=== Worker Data ===")
        for w in workers:
            print(f"  {w['name']}: online={w['is_online']}, "
                  f"60s={fmt_hashrate(w['hashrate_60s'])}, "
                  f"3hr={fmt_hashrate(w['hashrate_3hr'])}, "
                  f"last_share={w['last_share_minutes']:.1f}min ago"
                  if w['last_share_minutes'] is not None
                  else f"  {w['name']}: online={w['is_online']}, "
                       f"60s={fmt_hashrate(w['hashrate_60s'])}, "
                       f"3hr={fmt_hashrate(w['hashrate_3hr'])}, last_share=unknown")
        print("===================\n")
        return

    state = load_state()
    state = process_commands(
        token, chat_id, workers, state,
        power_draw_watts=power_draw_watts, rate_per_kwh=rate_per_kwh, wallet=wallet,
    )
    new_state = evaluate_and_alert(workers, state, token, chat_id, offline_threshold)
    new_state = check_outage_reminders(workers, new_state, token, chat_id)
    new_state = maybe_send_digest(
        workers, new_state, token, chat_id, wallet=wallet,
        power_draw_watts=power_draw_watts, rate_per_kwh=rate_per_kwh,
    )
    save_state(new_state)
    log.info("Run complete. State: %s", new_state)


if __name__ == "__main__":
    main()
