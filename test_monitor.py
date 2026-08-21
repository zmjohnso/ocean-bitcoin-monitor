#!/usr/bin/env python3
"""Tests for monitor.py — Ocean.xyz Bitcoin mining rig monitor."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time

import monitor

# ── Shared HTML fixture ───────────────────────────────────────────────────────

SAMPLE_HTML = """
<html><body><table>
  <tr class="table-row">
    <td class="hide-overflow">
      <a href="/stats/bc1qwallet.worker1">Worker A</a>
    </td>
    <td class="table-cell">
      <div class="status-online-text">Online</div>
    </td>
    <td class="date-text">2026-05-17 02:30</td>
    <td class="table-cell">661.2 Th/s</td>
    <td class="table-cell">650.0 Th/s</td>
  </tr>
  <tr class="table-row">
    <td class="hide-overflow">
      <a href="/stats/bc1qwallet.worker2">Worker B</a>
    </td>
    <td class="table-cell">
      <div class="status-offline-text">Offline</div>
    </td>
    <td class="date-text">2026-05-16 10:00</td>
    <td class="table-cell">0.0 Th/s</td>
    <td class="table-cell">325.0 Th/s</td>
  </tr>
  <tr class="table-row">
    <td class="hide-overflow">
      <a href="/stats/bc1qwallet">Total</a>
    </td>
    <td class="table-cell">-</td>
    <td class="date-text">-</td>
    <td class="table-cell">661.2 Th/s</td>
    <td class="table-cell">975.0 Th/s</td>
  </tr>
</table></body></html>
"""

FAKE_WORKERS = [
    {
        "name": "worker1",
        "last_share_minutes": 2.0,
        "hashrate_60s": 661.2,
        "hashrate_3hr": 650.0,
        "is_online": True,
    },
    {
        "name": "worker2",
        "last_share_minutes": 960.0,
        "hashrate_60s": 0.0,
        "hashrate_3hr": 325.0,
        "is_online": False,
    },
]


def make_mock_response(html: str = "", status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.text = html
    resp.status_code = status
    if status >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status}")
    else:
        resp.raise_for_status.return_value = None
    return resp


def write_uptime_log(path: Path, worker: str, polls: list[bool], base_dt: datetime) -> None:
    with path.open("a") as f:
        for i, online in enumerate(polls):
            ts = (base_dt + timedelta(minutes=i * 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(json.dumps({"ts": ts, "worker": worker, "online": online}) + "\n")


def write_sequence(path: Path, worker: str, sequence: list[tuple]) -> None:
    """sequence: list of (datetime, online_bool)"""
    with path.open("a") as f:
        for dt, online in sequence:
            ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(json.dumps({"ts": ts, "worker": worker, "online": online}) + "\n")


def write_hashrate_records(path: Path, records: list[tuple]) -> None:
    """records: list of (datetime, worker, online_bool, hashrate_3hr)"""
    with path.open("a") as f:
        for dt, worker, online, hashrate in records:
            ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(json.dumps({"ts": ts, "worker": worker, "online": online, "hashrate_3hr": hashrate}) + "\n")


# ── 1. Parsers ────────────────────────────────────────────────────────────────

class TestParseHashrate:
    def test_normal(self):
        assert monitor.parse_hashrate("661.2 Th/s") == 661.2

    def test_zero(self):
        assert monitor.parse_hashrate("0 Th/s") == 0.0

    def test_integer(self):
        assert monitor.parse_hashrate("500 Th/s") == 500.0

    def test_empty_string(self):
        assert monitor.parse_hashrate("") == 0.0

    def test_dash(self):
        assert monitor.parse_hashrate("-") == 0.0

    def test_non_numeric(self):
        assert monitor.parse_hashrate("abc Th/s") == 0.0


class TestParseLastShareMinutes:
    def test_dash(self):
        assert monitor.parse_last_share_minutes("-") is None

    def test_never(self):
        assert monitor.parse_last_share_minutes("never") is None

    def test_empty(self):
        assert monitor.parse_last_share_minutes("") is None

    def test_na(self):
        assert monitor.parse_last_share_minutes("n/a") is None

    def test_garbage(self):
        assert monitor.parse_last_share_minutes("not a date") is None

    def test_thirty_minutes_ago(self):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
        result = monitor.parse_last_share_minutes(ts)
        assert result is not None
        assert 29 <= result <= 31

    def test_zero_minutes_ago(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        result = monitor.parse_last_share_minutes(ts)
        assert result is not None
        assert 0 <= result < 2


class TestFmtHashrate:
    def test_normal(self):
        assert monitor.fmt_hashrate(661.2) == "661.2 Th/s"

    def test_zero(self):
        assert monitor.fmt_hashrate(0.0) == "0.0 Th/s"

    def test_rounds_to_one_decimal(self):
        assert monitor.fmt_hashrate(661.25) == "661.2 Th/s"


# ── 2. HTML scraper ───────────────────────────────────────────────────────────

class TestFetchWorkers:
    def test_network_error_returns_none(self):
        import requests as req_lib
        with patch("monitor.requests.get", side_effect=req_lib.ConnectionError("timeout")):
            assert monitor.fetch_workers("bc1qwallet") is None

    def test_http_error_returns_none(self):
        import requests as req_lib
        resp = make_mock_response("", status=503)
        resp.raise_for_status.side_effect = req_lib.HTTPError("503")
        with patch("monitor.requests.get", return_value=resp):
            assert monitor.fetch_workers("bc1qwallet") is None

    def test_empty_page_returns_none(self):
        with patch("monitor.requests.get", return_value=make_mock_response("<html></html>")):
            assert monitor.fetch_workers("bc1qwallet") is None

    def test_parses_exactly_two_workers(self):
        with patch("monitor.requests.get", return_value=make_mock_response(SAMPLE_HTML)):
            workers = monitor.fetch_workers("bc1qwallet")
        assert workers is not None
        assert len(workers) == 2

    def test_total_row_excluded(self):
        with patch("monitor.requests.get", return_value=make_mock_response(SAMPLE_HTML)):
            workers = monitor.fetch_workers("bc1qwallet")
        names = [w["name"] for w in workers]
        assert "worker1" in names
        assert "worker2" in names

    def test_online_status_correct(self):
        with patch("monitor.requests.get", return_value=make_mock_response(SAMPLE_HTML)):
            workers = monitor.fetch_workers("bc1qwallet")
        online = next(w for w in workers if w["name"] == "worker1")
        assert online["is_online"] is True
        assert online["hashrate_60s"] == 661.2
        assert online["hashrate_3hr"] == 650.0

    def test_offline_status_correct(self):
        with patch("monitor.requests.get", return_value=make_mock_response(SAMPLE_HTML)):
            workers = monitor.fetch_workers("bc1qwallet")
        offline = next(w for w in workers if w["name"] == "worker2")
        assert offline["is_online"] is False
        assert offline["hashrate_60s"] == 0.0

    def test_name_comes_from_href_suffix_not_link_text(self):
        with patch("monitor.requests.get", return_value=make_mock_response(SAMPLE_HTML)):
            workers = monitor.fetch_workers("bc1qwallet")
        names = {w["name"] for w in workers}
        assert "Worker A" not in names
        assert "worker1" in names


# ── 3. State persistence ──────────────────────────────────────────────────────

class TestState:
    def test_load_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.STATE_FILE", tmp_path / "state.json")
        assert monitor.load_state() == {}

    def test_load_corrupt_json(self, tmp_path, monkeypatch):
        f = tmp_path / "state.json"
        f.write_text("not valid json {")
        monkeypatch.setattr("monitor.STATE_FILE", f)
        assert monitor.load_state() == {}

    def test_round_trip(self, tmp_path, monkeypatch):
        f = tmp_path / "state.json"
        monkeypatch.setattr("monitor.STATE_FILE", f)
        data = {"worker1": {"online": True}, "last_update_id": 123}
        monitor.save_state(data)
        assert monitor.load_state() == data


# ── 4. Uptime logging ─────────────────────────────────────────────────────────

class TestLogUptime:
    def test_creates_one_line_per_worker(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        monitor.log_uptime(FAKE_WORKERS)
        lines = (tmp_path / "uptime_log.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2

    def test_line_has_required_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        monitor.log_uptime(FAKE_WORKERS[:1])
        record = json.loads((tmp_path / "uptime_log.jsonl").read_text().strip())
        assert {"ts", "worker", "online"} <= record.keys()

    def test_appends_on_second_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        monitor.log_uptime(FAKE_WORKERS[:1])
        monitor.log_uptime(FAKE_WORKERS[:1])
        lines = (tmp_path / "uptime_log.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2

    def test_online_field_matches_worker(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        monitor.log_uptime(FAKE_WORKERS)
        records = [
            json.loads(line)
            for line in (tmp_path / "uptime_log.jsonl").read_text().strip().splitlines()
        ]
        by_name = {r["worker"]: r for r in records}
        assert by_name["worker1"]["online"] is True
        assert by_name["worker2"]["online"] is False

    def test_hashrate_3hr_logged(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        monitor.log_uptime(FAKE_WORKERS)
        records = [
            json.loads(line)
            for line in (tmp_path / "uptime_log.jsonl").read_text().strip().splitlines()
        ]
        by_name = {r["worker"]: r for r in records}
        assert by_name["worker1"]["hashrate_3hr"] == 650.0
        assert by_name["worker2"]["hashrate_3hr"] == 325.0


class TestComputeUptime:
    BASE = datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc)

    def test_no_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "missing.jsonl")
        result = monitor.compute_uptime(self.BASE, self.BASE + timedelta(hours=1))
        assert result == {}

    def test_all_online(self, tmp_path, monkeypatch):
        log = tmp_path / "uptime_log.jsonl"
        monkeypatch.setattr("monitor.UPTIME_LOG", log)
        write_uptime_log(log, "w1", [True] * 10, self.BASE)
        stats = monitor.compute_uptime(self.BASE - timedelta(hours=1), self.BASE + timedelta(hours=2))
        assert stats["w1"] == {"online": 10, "total": 10}

    def test_mixed_online_offline(self, tmp_path, monkeypatch):
        log = tmp_path / "uptime_log.jsonl"
        monkeypatch.setattr("monitor.UPTIME_LOG", log)
        write_uptime_log(log, "w1", [True] * 8 + [False] * 2, self.BASE)
        stats = monitor.compute_uptime(self.BASE - timedelta(hours=1), self.BASE + timedelta(hours=2))
        assert stats["w1"]["online"] == 8
        assert stats["w1"]["total"] == 10

    def test_excludes_records_outside_window(self, tmp_path, monkeypatch):
        log = tmp_path / "uptime_log.jsonl"
        monkeypatch.setattr("monitor.UPTIME_LOG", log)
        write_uptime_log(log, "w1", [True] * 5, self.BASE)
        # Window is entirely after the records
        since = self.BASE + timedelta(hours=3)
        until = self.BASE + timedelta(hours=4)
        assert monitor.compute_uptime(since, until) == {}


# ── 5. Offline history ────────────────────────────────────────────────────────

class TestGetOfflineHistory:
    def test_no_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "missing.jsonl")
        assert monitor.get_offline_history() == []

    def test_completed_offline_event(self, tmp_path, monkeypatch):
        log = tmp_path / "uptime_log.jsonl"
        monkeypatch.setattr("monitor.UPTIME_LOG", log)
        base = datetime.now(timezone.utc) - timedelta(hours=5)
        write_sequence(log, "w1", [
            (base, True),
            (base + timedelta(minutes=5), False),
            (base + timedelta(minutes=10), False),
            (base + timedelta(minutes=15), True),
        ])
        events = monitor.get_offline_history()
        assert len(events) == 1
        assert events[0]["end"] is not None
        assert abs(events[0]["duration_mins"] - 10) < 1

    def test_ongoing_offline_event(self, tmp_path, monkeypatch):
        log = tmp_path / "uptime_log.jsonl"
        monkeypatch.setattr("monitor.UPTIME_LOG", log)
        base = datetime.now(timezone.utc) - timedelta(hours=2)
        write_sequence(log, "w1", [
            (base, True),
            (base + timedelta(minutes=5), False),
            (base + timedelta(minutes=10), False),
        ])
        events = monitor.get_offline_history()
        assert len(events) == 1
        assert events[0]["end"] is None

    def test_respects_n_limit(self, tmp_path, monkeypatch):
        log = tmp_path / "uptime_log.jsonl"
        monkeypatch.setattr("monitor.UPTIME_LOG", log)
        base = datetime.now(timezone.utc) - timedelta(days=5)
        for i in range(5):
            t = base + timedelta(hours=i * 4)
            write_sequence(log, "w1", [
                (t, True),
                (t + timedelta(minutes=5), False),
                (t + timedelta(minutes=10), True),
            ])
        assert len(monitor.get_offline_history(n=2)) == 2

    def test_sorted_newest_first(self, tmp_path, monkeypatch):
        log = tmp_path / "uptime_log.jsonl"
        monkeypatch.setattr("monitor.UPTIME_LOG", log)
        base = datetime.now(timezone.utc) - timedelta(days=3)
        for i in range(3):
            t = base + timedelta(hours=i * 8)
            write_sequence(log, "w1", [
                (t, True),
                (t + timedelta(minutes=5), False),
                (t + timedelta(minutes=10), True),
            ])
        events = monitor.get_offline_history()
        starts = [e["start"] for e in events]
        assert starts == sorted(starts, reverse=True)


class TestFormatHistory:
    def test_empty_list_message(self):
        assert "No offline events" in monitor.format_history([])

    def test_ongoing_event_contains_ongoing_since(self):
        result = monitor.format_history([{
            "worker": "w1",
            "start": datetime(2026, 5, 17, 2, 0, tzinfo=timezone.utc),
            "end": None,
            "duration_mins": 90,
        }])
        assert "ongoing since" in result
        assert "w1" in result

    def test_completed_event_contains_arrow(self):
        result = monitor.format_history([{
            "worker": "w1",
            "start": datetime(2026, 5, 17, 2, 0, tzinfo=timezone.utc),
            "end": datetime(2026, 5, 17, 3, 0, tzinfo=timezone.utc),
            "duration_mins": 60,
        }])
        assert "→" in result
        assert "w1" in result


# ── 6. Alert state transitions ────────────────────────────────────────────────

class TestEvaluateAndAlert:
    TOKEN, CHAT_ID, THRESHOLD = "tok", "123", 15.0

    def _run(self, workers, state):
        with patch("monitor.send_telegram") as mock_send:
            new_state = monitor.evaluate_and_alert(
                workers, state, self.TOKEN, self.CHAT_ID, self.THRESHOLD
            )
        return new_state, mock_send

    def _make_worker(self, name, online, last_share=5.0, hr60=300.0, hr3=300.0):
        return {"name": name, "is_online": online, "last_share_minutes": last_share,
                "hashrate_60s": hr60, "hashrate_3hr": hr3}

    def test_online_to_offline_sends_red_alert(self):
        workers = [self._make_worker("w1", False, last_share=20)]
        state = {"w1": {"online": True}}
        new_state, mock_send = self._run(workers, state)
        mock_send.assert_called_once()
        assert "🔴" in mock_send.call_args[0][0]

    def test_online_to_offline_sets_offline_state(self):
        workers = [self._make_worker("w1", False, last_share=20)]
        state = {"w1": {"online": True}}
        new_state, _ = self._run(workers, state)
        assert new_state["w1"]["online"] is False
        assert "offline_since" in new_state["w1"]
        assert new_state["w1"]["last_reminder_hrs"] == 0

    def test_offline_to_online_sends_green_alert(self):
        workers = [self._make_worker("w1", True)]
        state = {"w1": {"online": False, "offline_since": "2026-05-17T00:00:00Z", "last_reminder_hrs": 2}}
        new_state, mock_send = self._run(workers, state)
        mock_send.assert_called_once()
        assert "🟢" in mock_send.call_args[0][0]

    def test_offline_to_online_clears_outage_fields(self):
        workers = [self._make_worker("w1", True)]
        state = {"w1": {"online": False, "offline_since": "2026-05-17T00:00:00Z", "last_reminder_hrs": 2}}
        new_state, _ = self._run(workers, state)
        assert new_state["w1"]["online"] is True
        assert "offline_since" not in new_state["w1"]
        assert "last_reminder_hrs" not in new_state["w1"]

    def test_no_change_online_no_alert(self):
        workers = [self._make_worker("w1", True)]
        _, mock_send = self._run(workers, {"w1": {"online": True}})
        mock_send.assert_not_called()

    def test_no_change_offline_no_alert(self):
        workers = [self._make_worker("w1", False, last_share=60)]
        state = {"w1": {"online": False, "offline_since": "2026-05-17T00:00:00Z", "last_reminder_hrs": 0}}
        _, mock_send = self._run(workers, state)
        mock_send.assert_not_called()

    def test_new_worker_online_no_false_alarm(self):
        workers = [self._make_worker("new", True)]
        _, mock_send = self._run(workers, {})
        mock_send.assert_not_called()


# ── 7. Outage reminders ───────────────────────────────────────────────────────

class TestCheckOutageReminders:
    TOKEN, CHAT_ID = "tok", "123"

    def _ago(self, hours: float) -> str:
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _run(self, workers, state):
        with patch("monitor.send_telegram") as mock_send:
            new_state = monitor.check_outage_reminders(workers, state, self.TOKEN, self.CHAT_ID)
        return new_state, mock_send

    def _offline_worker(self, name="w1"):
        return {"name": name, "is_online": False, "last_share_minutes": 60.0,
                "hashrate_60s": 0, "hashrate_3hr": 300}

    def test_online_worker_no_reminder(self):
        workers = [{"name": "w1", "is_online": True, "last_share_minutes": 1,
                    "hashrate_60s": 300, "hashrate_3hr": 300}]
        _, mock_send = self._run(workers, {"w1": {"online": True}})
        mock_send.assert_not_called()

    def test_offline_under_2hr_no_reminder(self):
        state = {"w1": {"online": False, "offline_since": self._ago(1), "last_reminder_hrs": 0}}
        _, mock_send = self._run([self._offline_worker()], state)
        mock_send.assert_not_called()

    def test_offline_over_2hr_sends_reminder(self):
        state = {"w1": {"online": False, "offline_since": self._ago(3), "last_reminder_hrs": 0}}
        new_state, mock_send = self._run([self._offline_worker()], state)
        mock_send.assert_called_once()
        assert new_state["w1"]["last_reminder_hrs"] == 2

    def test_offline_over_6hr_sends_at_6hr_threshold(self):
        state = {"w1": {"online": False, "offline_since": self._ago(7), "last_reminder_hrs": 2}}
        new_state, mock_send = self._run([self._offline_worker()], state)
        mock_send.assert_called_once()
        assert new_state["w1"]["last_reminder_hrs"] == 6

    def test_already_reminded_no_resend(self):
        state = {"w1": {"online": False, "offline_since": self._ago(7), "last_reminder_hrs": 6}}
        _, mock_send = self._run([self._offline_worker()], state)
        mock_send.assert_not_called()

    def test_seeds_offline_since_when_missing(self):
        state = {"w1": {"online": False}}
        new_state, mock_send = self._run([self._offline_worker()], state)
        assert "offline_since" in new_state["w1"]
        mock_send.assert_not_called()


# ── 8. Daily earnings parser ─────────────────────────────────────────────────

EARNINGS_HTML = """
<html><body>
<div class="blocks dashboard-container">
  <div class="blocks-label">Estimated Earnings Per Day
    <div class="tooltip tooltip-info">
      <span class="tooltiptext">Estimated earnings per day tooltip</span>
    </div>
  </div>
  <span>0.00050000 BTC</span>
</div>
</body></html>
"""

class TestParseDailyEarningsSats:
    def test_extracts_sats_from_btc_value(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(EARNINGS_HTML, "html.parser")
        assert monitor.parse_daily_earnings_sats(soup) == 50000

    def test_missing_element_returns_none(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<html><body></body></html>", "html.parser")
        assert monitor.parse_daily_earnings_sats(soup) is None

    def test_zero_earnings_returns_zero(self):
        from bs4 import BeautifulSoup
        html = EARNINGS_HTML.replace("0.00050000 BTC", "0.00000000 BTC")
        soup = BeautifulSoup(html, "html.parser")
        assert monitor.parse_daily_earnings_sats(soup) == 0


# ── 9. Daily digest ───────────────────────────────────────────────────────────

class TestMaybeSendDigest:
    TOKEN, CHAT_ID = "tok", "123"

    def test_already_sent_today_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        with patch("monitor.send_telegram") as mock_send:
            with freeze_time("2026-05-17 12:00:00"):
                monitor.maybe_send_digest(FAKE_WORKERS, {"last_digest_date": "2026-05-17"}, self.TOKEN, self.CHAT_ID)
        mock_send.assert_not_called()

    def test_before_8am_eastern_skips(self, tmp_path, monkeypatch):
        """11:59 UTC = 07:59 AM EDT — should NOT fire despite being past 08:00 UTC."""
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        with patch("monitor.send_telegram") as mock_send:
            with freeze_time("2026-05-17 11:59:00"):
                monitor.maybe_send_digest(FAKE_WORKERS, {"last_digest_date": "2026-05-16"}, self.TOKEN, self.CHAT_ID)
        mock_send.assert_not_called()

    def test_sends_when_conditions_met(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        with patch("monitor.send_telegram") as mock_send:
            with freeze_time("2026-05-17 12:00:00"):
                new_state = monitor.maybe_send_digest(
                    FAKE_WORKERS, {"last_digest_date": "2026-05-16"}, self.TOKEN, self.CHAT_ID
                )
        mock_send.assert_called_once()
        assert new_state["last_digest_date"] == "2026-05-17"

    def test_digest_message_contains_worker_name(self, tmp_path, monkeypatch):
        log = tmp_path / "uptime_log.jsonl"
        monkeypatch.setattr("monitor.UPTIME_LOG", log)
        base = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
        write_uptime_log(log, "worker1", [True] * 5, base)
        with patch("monitor.send_telegram") as mock_send:
            with freeze_time("2026-05-17 12:00:00"):
                monitor.maybe_send_digest(FAKE_WORKERS, {"last_digest_date": "2026-05-16"}, self.TOKEN, self.CHAT_ID)
        assert "worker1" in mock_send.call_args[0][0]

    def test_digest_includes_earnings_when_wallet_provided(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        with patch("monitor.fetch_daily_earnings_sats", return_value=50000) as mock_fetch:
            with patch("monitor.send_telegram") as mock_send:
                with freeze_time("2026-05-17 12:00:00"):
                    monitor.maybe_send_digest(
                        FAKE_WORKERS, {"last_digest_date": "2026-05-16"},
                        self.TOKEN, self.CHAT_ID, wallet="bc1qtest"
                    )
        mock_fetch.assert_called_once_with("bc1qtest")
        assert "50,000" in mock_send.call_args[0][0]
        assert "sats" in mock_send.call_args[0][0]

    def test_digest_omits_earnings_when_fetch_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        with patch("monitor.fetch_daily_earnings_sats", return_value=None):
            with patch("monitor.send_telegram") as mock_send:
                with freeze_time("2026-05-17 12:00:00"):
                    monitor.maybe_send_digest(
                        FAKE_WORKERS, {"last_digest_date": "2026-05-16"},
                        self.TOKEN, self.CHAT_ID, wallet="bc1qtest"
                    )
        assert "sats" not in mock_send.call_args[0][0]

    NETWORK_STATS = {"difficulty": 100_000_000_000_000, "block_height": 963338, "block_reward": 3.125}

    KNOWN_DAILY_BTC = 0.0004086177796125412

    def test_digest_includes_breakeven_when_configured(self, tmp_path, monkeypatch):
        log = tmp_path / "uptime_log.jsonl"
        monkeypatch.setattr("monitor.UPTIME_LOG", log)
        with freeze_time("2026-05-17 12:00:00"):
            write_hashrate_records(log, [(datetime.now(timezone.utc), "w1", True, 600.0)])
            with patch("monitor.estimate_daily_btc", return_value=(self.KNOWN_DAILY_BTC, "ocean")):
                with patch("monitor.fetch_btc_price", return_value=72500.0):
                    with patch("monitor.send_telegram") as mock_send:
                        monitor.maybe_send_digest(
                            FAKE_WORKERS, {"last_digest_date": "2026-05-16"},
                            self.TOKEN, self.CHAT_ID, wallet="bc1qtest",
                            power_draw_watts=12300, rate_per_kwh=0.068,
                        )
        result = mock_send.call_args[0][0]
        assert "⚖️" in result
        assert "24h avg" in result

    def test_digest_estimate_called_with_wallet_and_avg_hashrate(self, tmp_path, monkeypatch):
        log = tmp_path / "uptime_log.jsonl"
        monkeypatch.setattr("monitor.UPTIME_LOG", log)
        with freeze_time("2026-05-17 12:00:00"):
            write_hashrate_records(log, [(datetime.now(timezone.utc), "w1", True, 600.0)])
            with patch("monitor.estimate_daily_btc", return_value=(self.KNOWN_DAILY_BTC, "ocean")) as mock_est:
                with patch("monitor.fetch_btc_price", return_value=72500.0):
                    with patch("monitor.send_telegram"):
                        monitor.maybe_send_digest(
                            FAKE_WORKERS, {"last_digest_date": "2026-05-16"},
                            self.TOKEN, self.CHAT_ID, wallet="bc1qtest",
                            power_draw_watts=12300, rate_per_kwh=0.068,
                        )
        mock_est.assert_called_once_with("bc1qtest", pytest.approx(600.0))

    def test_digest_omits_breakeven_when_not_configured(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        with patch("monitor.send_telegram") as mock_send:
            with freeze_time("2026-05-17 12:00:00"):
                monitor.maybe_send_digest(FAKE_WORKERS, {"last_digest_date": "2026-05-16"}, self.TOKEN, self.CHAT_ID)
        assert "⚖️" not in mock_send.call_args[0][0]

    def test_digest_omits_breakeven_when_estimate_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        with patch("monitor.estimate_daily_btc", return_value=(None, "unavailable")):
            with patch("monitor.send_telegram") as mock_send:
                with freeze_time("2026-05-17 12:00:00"):
                    monitor.maybe_send_digest(
                        FAKE_WORKERS, {"last_digest_date": "2026-05-16"},
                        self.TOKEN, self.CHAT_ID,
                        power_draw_watts=12300, rate_per_kwh=0.068,
                    )
        assert "⚖️" not in mock_send.call_args[0][0]


# ── 9. Process commands ───────────────────────────────────────────────────────

class TestProcessCommandsHelp:
    TOKEN, CHAT_ID = "tok", "123"

    def test_help_lists_available_commands(self):
        update = [{"update_id": 1, "message": {"chat": {"id": self.CHAT_ID}, "text": "/help"}}]
        state = {"last_update_id": 0}
        with patch("monitor.get_telegram_updates", return_value=update):
            with patch("monitor.send_telegram") as mock_send:
                monitor.process_commands(self.TOKEN, self.CHAT_ID, FAKE_WORKERS, state)
        mock_send.assert_called_once()
        response = mock_send.call_args[0][0]
        assert "/status" in response
        assert "/uptime" in response
        assert "/history" in response
        assert "/help" in response


# ── 10. Formatters ────────────────────────────────────────────────────────────

class TestProcessCommandsBreakeven:
    TOKEN, CHAT_ID = "tok", "123"
    KNOWN_DAILY_BTC = 0.0004086177796125412

    def _send_breakeven(self, state=None):
        update = [{"update_id": 1, "message": {"chat": {"id": self.CHAT_ID}, "text": "/breakeven"}}]
        state = state or {"last_update_id": 0}
        with patch("monitor.get_telegram_updates", return_value=update):
            with patch("monitor.send_telegram") as mock_send:
                monitor.process_commands(
                    self.TOKEN, self.CHAT_ID, FAKE_WORKERS, state,
                    power_draw_watts=12300, rate_per_kwh=0.068, wallet="bc1qtest",
                )
        return mock_send

    def test_not_configured_when_no_electricity_config(self):
        update = [{"update_id": 1, "message": {"chat": {"id": self.CHAT_ID}, "text": "/breakeven"}}]
        with patch("monitor.get_telegram_updates", return_value=update):
            with patch("monitor.send_telegram") as mock_send:
                monitor.process_commands(self.TOKEN, self.CHAT_ID, FAKE_WORKERS, {"last_update_id": 0})
        assert "not configured" in mock_send.call_args[0][0].lower()

    def test_success_uses_summed_live_hashrate(self):
        with patch("monitor.estimate_daily_btc", return_value=(self.KNOWN_DAILY_BTC, "ocean")):
            with patch("monitor.fetch_btc_price", return_value=72500.0):
                mock_send = self._send_breakeven()
        result = mock_send.call_args[0][0]
        assert "975.0" in result  # 650.0 + 325.0 summed across both workers

    def test_estimate_called_with_wallet_and_summed_hashrate(self):
        with patch("monitor.estimate_daily_btc", return_value=(self.KNOWN_DAILY_BTC, "ocean")) as mock_est:
            with patch("monitor.fetch_btc_price", return_value=72500.0):
                self._send_breakeven()
        mock_est.assert_called_once_with("bc1qtest", 975.0)

    def test_daily_btc_unavailable_reports_unavailable(self):
        with patch("monitor.estimate_daily_btc", return_value=(None, "unavailable")):
            with patch("monitor.fetch_btc_price", return_value=72500.0):
                mock_send = self._send_breakeven()
        assert "unavailable" in mock_send.call_args[0][0].lower()

    def test_theoretical_fallback_noted_in_label(self):
        with patch("monitor.estimate_daily_btc", return_value=(self.KNOWN_DAILY_BTC, "theoretical")):
            with patch("monitor.fetch_btc_price", return_value=72500.0):
                mock_send = self._send_breakeven()
        assert "fallback" in mock_send.call_args[0][0].lower()

    def test_price_failure_still_shows_breakeven(self):
        with patch("monitor.estimate_daily_btc", return_value=(self.KNOWN_DAILY_BTC, "ocean")):
            with patch("monitor.fetch_btc_price", return_value=None):
                mock_send = self._send_breakeven()
        result = mock_send.call_args[0][0]
        assert "Break-even price" in result
        assert "unavailable" in result.lower()

    def test_help_lists_breakeven_command(self):
        update = [{"update_id": 1, "message": {"chat": {"id": self.CHAT_ID}, "text": "/help"}}]
        with patch("monitor.get_telegram_updates", return_value=update):
            with patch("monitor.send_telegram") as mock_send:
                monitor.process_commands(self.TOKEN, self.CHAT_ID, FAKE_WORKERS, {"last_update_id": 0})
        assert "/breakeven" in mock_send.call_args[0][0]


class TestFormatStatus:
    def test_online_worker_shows_green(self):
        workers = [{"name": "w1", "is_online": True, "last_share_minutes": 3.0,
                    "hashrate_60s": 661.2, "hashrate_3hr": 650.0}]
        result = monitor.format_status(workers)
        assert "🟢" in result
        assert "w1" in result
        assert "661.2" in result

    def test_offline_worker_shows_red(self):
        workers = [{"name": "w1", "is_online": False, "last_share_minutes": 960.0,
                    "hashrate_60s": 0.0, "hashrate_3hr": 300.0}]
        result = monitor.format_status(workers)
        assert "🔴" in result
        assert "OFFLINE" in result
        assert "w1" in result


class TestFormatUptimeReport:
    SINCE = datetime(2026, 5, 1, tzinfo=timezone.utc)
    UNTIL = datetime(2026, 5, 31, tzinfo=timezone.utc)

    def test_above_90_shows_checkmark(self):
        stats = {"w1": {"online": 9, "total": 10}}
        result = monitor.format_uptime_report(stats, self.SINCE, self.UNTIL)
        assert "✅" in result

    def test_below_90_shows_cross_and_budget(self):
        stats = {"w1": {"online": 5, "total": 10}}
        result = monitor.format_uptime_report(stats, self.SINCE, self.UNTIL)
        assert "❌" in result
        assert "budget" in result

    def test_empty_stats_shows_no_data(self):
        result = monitor.format_uptime_report({}, self.SINCE, self.UNTIL)
        assert "No uptime data" in result


# ── 11. Break-even calculations ───────────────────────────────────────────────

class TestExpectedDailyBtc:
    def test_known_values(self):
        result = monitor.expected_daily_btc(
            hashrate_ths=500, difficulty=100_000_000_000_000, block_reward=3.125
        )
        assert result == pytest.approx(0.000314321368932724, rel=1e-9)

    def test_zero_hashrate_returns_zero(self):
        result = monitor.expected_daily_btc(
            hashrate_ths=0, difficulty=100_000_000_000_000, block_reward=3.125
        )
        assert result == 0.0

    def test_scales_linearly_with_hashrate(self):
        low = monitor.expected_daily_btc(hashrate_ths=100, difficulty=1e14, block_reward=3.125)
        high = monitor.expected_daily_btc(hashrate_ths=200, difficulty=1e14, block_reward=3.125)
        assert high == pytest.approx(low * 2, rel=1e-9)


class TestFetchNetworkStats:
    def _responses(self, difficulty_text, blockcount_text):
        return [make_mock_response(difficulty_text), make_mock_response(blockcount_text)]

    def test_success_returns_difficulty_height_and_reward(self):
        with patch("monitor.requests.get", side_effect=self._responses("127479855693691.0", "963338")):
            result = monitor.fetch_network_stats()
        assert result == {
            "difficulty": pytest.approx(127479855693691.0),
            "block_height": 963338,
            "block_reward": pytest.approx(3.125),
        }

    def test_reward_at_next_halving_boundary(self):
        with patch("monitor.requests.get", side_effect=self._responses("1e14", "1050000")):
            result = monitor.fetch_network_stats()
        assert result["block_reward"] == pytest.approx(1.5625)

    def test_network_error_returns_none(self):
        import requests as req_lib
        with patch("monitor.requests.get", side_effect=req_lib.ConnectionError("timeout")):
            assert monitor.fetch_network_stats() is None

    def test_unparseable_response_returns_none(self):
        with patch("monitor.requests.get", side_effect=self._responses("not a number", "963338")):
            assert monitor.fetch_network_stats() is None


class TestFetchBtcPrice:
    def _price_response(self, usd):
        resp = make_mock_response()
        resp.json.return_value = {"bitcoin": {"usd": usd}}
        return resp

    def test_success_returns_price(self):
        with patch("monitor.requests.get", return_value=self._price_response(72500.5)):
            assert monitor.fetch_btc_price() == 72500.5

    def test_network_error_returns_none(self):
        import requests as req_lib
        with patch("monitor.requests.get", side_effect=req_lib.ConnectionError("timeout")):
            assert monitor.fetch_btc_price() is None

    def test_malformed_response_returns_none(self):
        resp = make_mock_response()
        resp.json.return_value = {"unexpected": "shape"}
        with patch("monitor.requests.get", return_value=resp):
            assert monitor.fetch_btc_price() is None


class TestEstimateDailyBtc:
    NETWORK_STATS = {"difficulty": 100_000_000_000_000, "block_height": 963338, "block_reward": 3.125}

    def test_ocean_estimate_used_when_available(self):
        with patch("monitor.fetch_daily_earnings_sats", return_value=45000):
            result = monitor.estimate_daily_btc("bc1qwallet", hashrate_ths=650)
        assert result == (pytest.approx(0.00045), "ocean")

    def test_falls_back_to_theoretical_when_ocean_fails(self):
        with patch("monitor.fetch_daily_earnings_sats", return_value=None):
            with patch("monitor.fetch_network_stats", return_value=self.NETWORK_STATS):
                result = monitor.estimate_daily_btc("bc1qwallet", hashrate_ths=650)
        assert result[1] == "theoretical"
        assert result[0] == pytest.approx(0.0004086177796125412)

    def test_returns_none_when_both_fail(self):
        with patch("monitor.fetch_daily_earnings_sats", return_value=None):
            with patch("monitor.fetch_network_stats", return_value=None):
                result = monitor.estimate_daily_btc("bc1qwallet", hashrate_ths=650)
        assert result == (None, "unavailable")

    def test_skips_ocean_fetch_when_wallet_is_none(self):
        with patch("monitor.fetch_daily_earnings_sats") as mock_fetch:
            with patch("monitor.fetch_network_stats", return_value=self.NETWORK_STATS):
                result = monitor.estimate_daily_btc(None, hashrate_ths=650)
        mock_fetch.assert_not_called()
        assert result[1] == "theoretical"


class TestComputeBreakeven:
    def test_known_values(self):
        result = monitor.compute_breakeven(
            daily_btc=0.0004086177796125412,
            power_draw_watts=12300,
            rate_per_kwh=0.068,
        )
        assert result["expected_daily_btc"] == pytest.approx(0.0004086177796125412)
        assert result["daily_cost"] == pytest.approx(20.073600000000006)
        assert result["breakeven_price"] == pytest.approx(49125.61567691489)

    def test_zero_daily_btc_breakeven_is_none(self):
        result = monitor.compute_breakeven(
            daily_btc=0.0,
            power_draw_watts=12300,
            rate_per_kwh=0.068,
        )
        assert result["expected_daily_btc"] == 0.0
        assert result["breakeven_price"] is None


class TestComputeAvgHashrate:
    BASE = datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc)

    def test_no_file_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "missing.jsonl")
        result = monitor.compute_avg_hashrate(self.BASE, self.BASE + timedelta(hours=1))
        assert result == 0.0

    def test_single_worker_constant_hashrate(self, tmp_path, monkeypatch):
        log = tmp_path / "uptime_log.jsonl"
        monkeypatch.setattr("monitor.UPTIME_LOG", log)
        write_hashrate_records(log, [
            (self.BASE, "w1", True, 600.0),
            (self.BASE + timedelta(minutes=5), "w1", True, 600.0),
            (self.BASE + timedelta(minutes=10), "w1", True, 600.0),
        ])
        result = monitor.compute_avg_hashrate(self.BASE - timedelta(hours=1), self.BASE + timedelta(hours=1))
        assert result == pytest.approx(600.0)

    def test_two_workers_summed_per_poll_then_averaged(self, tmp_path, monkeypatch):
        log = tmp_path / "uptime_log.jsonl"
        monkeypatch.setattr("monitor.UPTIME_LOG", log)
        write_hashrate_records(log, [
            (self.BASE, "w1", True, 600.0),
            (self.BASE, "w2", True, 300.0),
            (self.BASE + timedelta(minutes=5), "w1", True, 650.0),
            (self.BASE + timedelta(minutes=5), "w2", True, 350.0),
        ])
        result = monitor.compute_avg_hashrate(self.BASE - timedelta(hours=1), self.BASE + timedelta(hours=1))
        assert result == pytest.approx(950.0)

    def test_excludes_records_outside_window(self, tmp_path, monkeypatch):
        log = tmp_path / "uptime_log.jsonl"
        monkeypatch.setattr("monitor.UPTIME_LOG", log)
        write_hashrate_records(log, [(self.BASE, "w1", True, 600.0)])
        since = self.BASE + timedelta(hours=3)
        until = self.BASE + timedelta(hours=4)
        assert monitor.compute_avg_hashrate(since, until) == 0.0


class TestFormatBreakevenMessage:
    BREAKEVEN = {"expected_daily_btc": 0.00040862, "daily_cost": 20.07, "breakeven_price": 49125.62}

    def test_profitable_shows_checkmark_and_margin(self):
        result = monitor.format_breakeven_message(self.BREAKEVEN, hashrate_ths=650, current_price=72500, label="Live")
        assert "✅" in result
        assert "49,125.62" in result
        assert "72,500" in result

    def test_unprofitable_shows_cross(self):
        result = monitor.format_breakeven_message(self.BREAKEVEN, hashrate_ths=650, current_price=30000, label="Live")
        assert "❌" in result

    def test_label_appears(self):
        result = monitor.format_breakeven_message(self.BREAKEVEN, hashrate_ths=650, current_price=72500, label="24h avg")
        assert "24h avg" in result

    def test_hashrate_appears(self):
        result = monitor.format_breakeven_message(self.BREAKEVEN, hashrate_ths=650, current_price=72500, label="Live")
        assert "650.0" in result

    def test_price_unavailable_omits_margin_but_shows_breakeven(self):
        result = monitor.format_breakeven_message(self.BREAKEVEN, hashrate_ths=650, current_price=None, label="Live")
        assert "49,125.62" in result
        assert "unavailable" in result.lower()

    def test_zero_hashrate_breakeven_unavailable(self):
        breakeven = {"expected_daily_btc": 0.0, "daily_cost": 20.07, "breakeven_price": None}
        result = monitor.format_breakeven_message(breakeven, hashrate_ths=0, current_price=72500, label="Live")
        assert "unavailable" in result.lower() or "no hashrate" in result.lower()
