"""
Tests for scripts/run_trading_day.py -- the unattended market-hours loop.

_acquire_single_instance_lock uses msvcrt (Windows-only), so it's always
monkeypatched out here rather than exercised for real; every test also
passes once=True to avoid the loop's real time.sleep. What's under test is
the orchestration itself: the paper-only refusal, the single-instance
refusal, exception isolation per step (_step), and the swing-scan
once-a-day gating that survives a mid-session process restart.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_trading_day as rtd

ET = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(rtd, "LOG_PATH", tmp_path / "trading_day_log.jsonl")
    monkeypatch.setattr(rtd, "LOCK_PATH", tmp_path / "trading_day.lock")


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # Belt-and-suspenders: every test passes once=True, which should never
    # reach the sleep call, but a bug that removed the once-guard shouldn't
    # be able to hang the suite.
    monkeypatch.setattr(rtd.time_mod, "sleep", lambda seconds: None)


def _clock(is_open, next_open=None, next_close=None, timestamp=None):
    return {
        "is_open": is_open,
        "timestamp": timestamp or datetime.now(timezone.utc),
        "next_open": next_open,
        "next_close": next_close,
    }


# ------------------------------------------------------------------- _log

def test_log_writes_jsonl_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(rtd, "LOG_PATH", tmp_path / "log.jsonl")
    rtd._log("some_event", status="ok", result={"a": 1})

    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "some_event"
    assert entry["status"] == "ok"
    assert entry["result"] == {"a": 1}
    assert "logged_at" in entry


def test_log_appends_across_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(rtd, "LOG_PATH", tmp_path / "log.jsonl")
    rtd._log("first")
    rtd._log("second")
    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert len(lines) == 2


# ---------------------------------------------------------------- _step

def test_step_returns_result_and_logs_ok():
    result = rtd._step("my_step", lambda: {"x": 1})
    assert result == {"x": 1}
    entry = json.loads(rtd.LOG_PATH.read_text().splitlines()[-1])
    assert entry == {**entry, "event": "my_step", "status": "ok", "result": {"x": 1}}


def test_step_isolates_exception_and_returns_none():
    def boom():
        raise ValueError("kaboom")

    result = rtd._step("my_step", boom)

    assert result is None
    entry = json.loads(rtd.LOG_PATH.read_text().splitlines()[-1])
    assert entry["status"] == "error"
    assert "kaboom" in entry["error"]


# ------------------------------------------------ _swing_scan_already_ran_today

def test_swing_scan_already_ran_today_no_log_file():
    assert rtd._swing_scan_already_ran_today() is None


def test_swing_scan_already_ran_today_true_when_ok_entry_today():
    today_utc_iso = datetime.now(timezone.utc).isoformat()
    rtd.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rtd.LOG_PATH.write_text(json.dumps({
        "event": "swing_cycle", "status": "ok", "logged_at": today_utc_iso,
    }) + "\n")

    result = rtd._swing_scan_already_ran_today()

    assert result == datetime.now(ET).date()


def test_swing_scan_already_ran_today_ignores_error_status():
    today_utc_iso = datetime.now(timezone.utc).isoformat()
    rtd.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rtd.LOG_PATH.write_text(json.dumps({
        "event": "swing_cycle", "status": "error", "logged_at": today_utc_iso,
    }) + "\n")

    assert rtd._swing_scan_already_ran_today() is None


def test_swing_scan_already_ran_today_ignores_other_events():
    today_utc_iso = datetime.now(timezone.utc).isoformat()
    rtd.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rtd.LOG_PATH.write_text(json.dumps({
        "event": "trail_stops", "status": "ok", "logged_at": today_utc_iso,
    }) + "\n")

    assert rtd._swing_scan_already_ran_today() is None


def test_swing_scan_already_ran_today_ignores_yesterday():
    yesterday_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    rtd.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rtd.LOG_PATH.write_text(json.dumps({
        "event": "swing_cycle", "status": "ok", "logged_at": yesterday_iso,
    }) + "\n")

    assert rtd._swing_scan_already_ran_today() is None


def test_swing_scan_already_ran_today_skips_malformed_lines():
    today_utc_iso = datetime.now(timezone.utc).isoformat()
    rtd.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rtd.LOG_PATH.write_text(
        "not json at all\n"
        + json.dumps({"event": "swing_cycle", "status": "ok", "logged_at": today_utc_iso}) + "\n"
    )

    assert rtd._swing_scan_already_ran_today() == datetime.now(ET).date()


# ------------------------------------------------------------- run_trading_day

def test_run_trading_day_refuses_when_not_paper(monkeypatch):
    monkeypatch.setattr(rtd, "ALPACA_PAPER", False)
    with pytest.raises(SystemExit) as exc_info:
        rtd.run_trading_day(once=True)
    assert exc_info.value.code == 1
    entry = json.loads(rtd.LOG_PATH.read_text().splitlines()[-1])
    assert entry["event"] == "refused_to_start"


def test_run_trading_day_refuses_when_lock_held(monkeypatch):
    monkeypatch.setattr(rtd, "ALPACA_PAPER", True)
    monkeypatch.setattr(rtd, "_acquire_single_instance_lock", lambda: None)
    with pytest.raises(SystemExit) as exc_info:
        rtd.run_trading_day(once=True)
    assert exc_info.value.code == 1
    entry = json.loads(rtd.LOG_PATH.read_text().splitlines()[-1])
    assert entry["event"] == "refused_to_start"
    assert "lock" in entry["reason"]


def test_run_trading_day_once_market_closed_far_from_open_does_final_reconcile(monkeypatch):
    monkeypatch.setattr(rtd, "ALPACA_PAPER", True)
    monkeypatch.setattr(rtd, "_acquire_single_instance_lock", lambda: object())
    now = datetime.now(timezone.utc)
    next_open = now + __import__("datetime").timedelta(hours=48)  # a weekend-scale gap
    monkeypatch.setattr(
        rtd.alpaca_client, "get_market_clock",
        lambda: _clock(is_open=False, next_open=next_open, timestamp=now),
    )
    reconcile_calls = []
    monkeypatch.setattr(rtd, "reconcile", lambda: reconcile_calls.append("reconcile"))
    monkeypatch.setattr(rtd, "_reconcile_missing_fills_all_accounts", lambda: reconcile_calls.append("missing_fills"))

    rtd.run_trading_day(once=True)

    assert reconcile_calls == ["reconcile", "missing_fills"]
    events = [json.loads(l)["event"] for l in rtd.LOG_PATH.read_text().splitlines()]
    assert "session_end" in events


def test_run_trading_day_once_market_closed_near_open_waits_and_returns(monkeypatch):
    monkeypatch.setattr(rtd, "ALPACA_PAPER", True)
    monkeypatch.setattr(rtd, "_acquire_single_instance_lock", lambda: object())
    now = datetime.now(timezone.utc)
    next_open = now + timedelta(minutes=20)
    monkeypatch.setattr(
        rtd.alpaca_client, "get_market_clock",
        lambda: _clock(is_open=False, next_open=next_open, timestamp=now),
    )
    reconcile_calls = []
    monkeypatch.setattr(rtd, "reconcile", lambda: reconcile_calls.append("reconcile"))

    rtd.run_trading_day(once=True)

    # Near-open wait path must NOT run the final reconcile -- that's only
    # for the "giving up for the day" branch.
    assert reconcile_calls == []
    events = [json.loads(l)["event"] for l in rtd.LOG_PATH.read_text().splitlines()]
    assert "waiting_for_open" in events


def test_run_trading_day_once_market_open_before_scan_window_skips_swing_cycle(monkeypatch):
    monkeypatch.setattr(rtd, "ALPACA_PAPER", True)
    monkeypatch.setattr(rtd, "_acquire_single_instance_lock", lambda: object())

    # 9:00 ET, before the 9:45 scan window.
    now_et = datetime.now(ET).replace(hour=9, minute=0, second=0, microsecond=0)
    now_utc = now_et.astimezone(timezone.utc)
    next_close = now_utc + timedelta(hours=6)
    monkeypatch.setattr(rtd, "datetime", type("_dt", (datetime,), {
        "now": classmethod(lambda cls, tz=None: now_et if tz == ET else now_utc)
    }))
    monkeypatch.setattr(
        rtd.alpaca_client, "get_market_clock",
        lambda: _clock(is_open=True, next_close=next_close, timestamp=now_utc),
    )
    swing_calls = []
    monkeypatch.setattr(rtd, "_swing_cycle", lambda: swing_calls.append("swing"))
    trail_calls = []
    monkeypatch.setattr(rtd, "_trail_all_stops", lambda: trail_calls.append("trail"))

    rtd.run_trading_day(once=True)

    assert swing_calls == []
    assert trail_calls == ["trail"]


def test_run_trading_day_once_market_open_after_scan_window_runs_swing_cycle(monkeypatch):
    monkeypatch.setattr(rtd, "ALPACA_PAPER", True)
    monkeypatch.setattr(rtd, "_acquire_single_instance_lock", lambda: object())

    # 10:00 ET on a Wednesday, inside the scan window.
    now_et = datetime.now(ET).replace(hour=10, minute=0, second=0, microsecond=0)
    while now_et.weekday() not in rtd.SWING_SCAN_WEEKDAYS:
        now_et += timedelta(days=1)
    now_utc = now_et.astimezone(timezone.utc)
    next_close = now_utc + timedelta(hours=6)
    monkeypatch.setattr(rtd, "datetime", type("_dt", (datetime,), {
        "now": classmethod(lambda cls, tz=None: now_et if tz == ET else now_utc)
    }))
    monkeypatch.setattr(
        rtd.alpaca_client, "get_market_clock",
        lambda: _clock(is_open=True, next_close=next_close, timestamp=now_utc),
    )
    swing_calls = []
    monkeypatch.setattr(rtd, "_swing_cycle", lambda: swing_calls.append("swing"))
    monkeypatch.setattr(rtd, "_trail_all_stops", lambda: None)

    rtd.run_trading_day(once=True)

    assert swing_calls == ["swing"]


def test_run_trading_day_skips_swing_cycle_already_run_today(monkeypatch):
    # Regression guard: a mid-session restart must not re-run the swing
    # scan if the log shows it already succeeded today -- the log is the
    # source of truth, not in-process state.
    monkeypatch.setattr(rtd, "ALPACA_PAPER", True)
    monkeypatch.setattr(rtd, "_acquire_single_instance_lock", lambda: object())

    today_utc_iso = datetime.now(timezone.utc).isoformat()
    rtd.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rtd.LOG_PATH.write_text(json.dumps({
        "event": "swing_cycle", "status": "ok", "logged_at": today_utc_iso,
    }) + "\n")

    now_et = datetime.now(ET).replace(hour=10, minute=0, second=0, microsecond=0)
    while now_et.weekday() not in rtd.SWING_SCAN_WEEKDAYS:
        now_et += timedelta(days=1)
    now_utc = now_et.astimezone(timezone.utc)
    next_close = now_utc + timedelta(hours=6)
    monkeypatch.setattr(rtd, "datetime", type("_dt", (datetime,), {
        "now": classmethod(lambda cls, tz=None: now_et if tz == ET else now_utc)
    }))
    monkeypatch.setattr(
        rtd.alpaca_client, "get_market_clock",
        lambda: _clock(is_open=True, next_close=next_close, timestamp=now_utc),
    )
    swing_calls = []
    monkeypatch.setattr(rtd, "_swing_cycle", lambda: swing_calls.append("swing"))
    monkeypatch.setattr(rtd, "_trail_all_stops", lambda: None)

    rtd.run_trading_day(once=True)

    assert swing_calls == []


def test_run_trading_day_once_clock_unreachable_returns(monkeypatch):
    monkeypatch.setattr(rtd, "ALPACA_PAPER", True)
    monkeypatch.setattr(rtd, "_acquire_single_instance_lock", lambda: object())

    def boom():
        raise ConnectionError("no network")

    monkeypatch.setattr(rtd.alpaca_client, "get_market_clock", boom)

    rtd.run_trading_day(once=True)  # must not raise -- _step isolates the failure

    events = [json.loads(l)["event"] for l in rtd.LOG_PATH.read_text().splitlines()]
    assert "clock" in events
