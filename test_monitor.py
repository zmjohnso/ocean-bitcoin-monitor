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


# ── 8. Daily digest ───────────────────────────────────────────────────────────

class TestMaybeSendDigest:
    TOKEN, CHAT_ID = "tok", "123"

    def test_already_sent_today_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        with patch("monitor.send_telegram") as mock_send:
            with freeze_time("2026-05-17 10:00:00"):
                monitor.maybe_send_digest(FAKE_WORKERS, {"last_digest_date": "2026-05-17"}, self.TOKEN, self.CHAT_ID)
        mock_send.assert_not_called()

    def test_before_8am_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        with patch("monitor.send_telegram") as mock_send:
            with freeze_time("2026-05-17 07:59:00"):
                monitor.maybe_send_digest(FAKE_WORKERS, {"last_digest_date": "2026-05-16"}, self.TOKEN, self.CHAT_ID)
        mock_send.assert_not_called()

    def test_sends_when_conditions_met(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monitor.UPTIME_LOG", tmp_path / "uptime_log.jsonl")
        with patch("monitor.send_telegram") as mock_send:
            with freeze_time("2026-05-17 10:00:00"):
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
            with freeze_time("2026-05-17 10:00:00"):
                monitor.maybe_send_digest(FAKE_WORKERS, {"last_digest_date": "2026-05-16"}, self.TOKEN, self.CHAT_ID)
        assert "worker1" in mock_send.call_args[0][0]


# ── 9. Formatters ─────────────────────────────────────────────────────────────

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
