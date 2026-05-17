#!/usr/bin/env python3
"""
Uptime report for Ocean.xyz Bitcoin mining rigs.

Usage:
  python3 report.py                    # last 30 days
  python3 report.py --days 7           # last 7 days
  python3 report.py --month 2026-04    # calendar month
  python3 report.py --since 2026-05-01 # from a date to now
"""

import argparse
import json
import sys
from calendar import monthrange
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
UPTIME_LOG = BASE_DIR / "uptime_log.jsonl"
GUARANTEE = 0.90


def parse_args():
    p = argparse.ArgumentParser(description="Rig uptime report")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, help="Last N days (default: 30)")
    group.add_argument("--month", help="Calendar month, e.g. 2026-04")
    group.add_argument("--since", help="Start date YYYY-MM-DD (to now)")
    return p.parse_args()


def resolve_window(args) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if args.month:
        year, month = map(int, args.month.split("-"))
        since = datetime(year, month, 1, tzinfo=timezone.utc)
        last_day = monthrange(year, month)[1]
        until = datetime(year, month, last_day, 23, 59, tzinfo=timezone.utc)
        until = min(until, now)
    elif args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        until = now
    else:
        days = args.days or 30
        until = now
        since = now.replace(hour=0, minute=0) - __import__("datetime").timedelta(days=days - 1)
        since = since.replace(tzinfo=timezone.utc)
    return since, until


def load_stats(since: datetime, until: datetime) -> dict:
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


def print_report(stats: dict, since: datetime, until: datetime) -> None:
    days = max(1, (until - since).days + 1)
    print(f"\n=== Uptime Report: {since.strftime('%Y-%m-%d')} to {until.strftime('%Y-%m-%d')} ===")

    if not stats:
        print("No uptime data found for this period.")
        print(f"(Log file: {UPTIME_LOG})")
        return

    sample_total = next(iter(stats.values()))["total"]
    print(f"Monitoring period: {days} days ({sample_total:,} polls/rig, 5-min intervals)\n")

    any_breach = False
    for worker, s in sorted(stats.items()):
        total, online = s["total"], s["online"]
        offline = total - online
        pct = online / total * 100 if total else 0
        online_hrs = online * 5 / 60
        offline_hrs = offline * 5 / 60
        budget_offline = int(total * (1 - GUARANTEE))
        over_polls = offline - budget_offline
        over_hrs = over_polls * 5 / 60

        print(f"Worker: {worker}")
        print(f"  Online:  {online:>6,} polls  ({pct:5.1f}%)  — {online_hrs:.1f} hrs")
        print(f"  Offline: {offline:>6,} polls  ({100-pct:5.1f}%)  — {offline_hrs:.1f} hrs")

        if pct >= GUARANTEE * 100:
            print(f"  Status:  ✅ Above {GUARANTEE*100:.0f}% guarantee")
        else:
            any_breach = True
            print(f"  Status:  ❌ BELOW {GUARANTEE*100:.0f}% guarantee")
            print(f"           Budget: {budget_offline:,} polls offline allowed ({(1-GUARANTEE)*100:.0f}% of {total:,})")
            print(f"           Actual: {offline:,} polls offline ({over_polls:,} polls / {over_hrs:.1f} hrs over budget)")
        print()

    print("Note: Report covers only periods when the monitor was running.")
    print("      Gaps in monitoring time are not counted as uptime or downtime.")
    if any_breach:
        print(f"\n⚠️  One or more workers are below the {GUARANTEE*100:.0f}% guarantee threshold.")
    print()


def main():
    args = parse_args()
    since, until = resolve_window(args)
    stats = load_stats(since, until)
    print_report(stats, since, until)


if __name__ == "__main__":
    main()
