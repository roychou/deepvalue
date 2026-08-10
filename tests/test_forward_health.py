"""
Watchdog + Gateway-preflight tests (the unattended clock's own failure handling).

Written after a real triage cost a round-trip: on 2026-08-03 the weekly run crashed, and the
STALE alert reported "not completed in over 8 days" (the heartbeat was 5 days old) while
withholding the exception the heartbeat had already captured. Both facts the reader needed
were on disk. These tests pin the three stale conditions apart and pin the error text INTO
the alert body.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from deepvalue.forward import ibkr_prices, notify
from deepvalue.forward import run as fwd


def _hb(status: str = "ok", hours_ago: float = 1.0, note: str = "") -> notify.Heartbeat:
    ts = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    return notify.Heartbeat(status=status, as_of="2026-08-03", ts=ts, note=note)


# --- staleness predicate -------------------------------------------------------------------

@pytest.mark.parametrize("hb,stale", [
    (_hb("ok", 1.0), False),                      # fresh success — the healthy case
    (_hb("ok", 200.0), True),                     # succeeded, but the clock has gone quiet
    (_hb("error", 0.1), True),                    # crashed minutes ago — stale despite being new
    (None, True),                                 # never ran
])
def test_heartbeat_stale_conditions(hb, stale):
    assert notify.heartbeat_stale(hb, max_age_hours=192.0) is stale


def test_heartbeat_stale_on_unreadable_timestamp():
    hb = notify.Heartbeat(status="ok", as_of="2026-08-03", ts="not-a-timestamp")
    assert notify.heartbeat_age_hours(hb) is None
    assert notify.heartbeat_stale(hb, max_age_hours=192.0) is True


def test_heartbeat_age_hours_measures_the_gap():
    assert notify.heartbeat_age_hours(_hb("ok", 48.0)) == pytest.approx(48.0, abs=0.1)
    assert notify.heartbeat_age_hours(None) is None


# --- the watchdog alert --------------------------------------------------------------------

@pytest.fixture
def alerts(monkeypatch):
    """Capture (subject, body) instead of sending, and control what the heartbeat says."""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "notify", lambda s, b="": sent.append((s, b)) or True)
    return sent


def _with_hb(monkeypatch, hb):
    monkeypatch.setattr(notify, "read_heartbeat", lambda *a, **k: hb)


def test_healthcheck_silent_when_fresh(monkeypatch, alerts):
    _with_hb(monkeypatch, _hb("ok", 1.0))
    assert fwd._healthcheck() == 0
    assert alerts == []


def test_crashed_run_reports_the_error_and_the_real_age(monkeypatch, alerts):
    """The 2026-08-03 case: a run that RAN and FAILED 5 days ago."""
    _with_hb(monkeypatch, _hb("error", 118.0, note="GatewayNotReadyError: no managed account"))
    assert fwd._healthcheck() == 1
    (subject, body), = alerts
    assert "CRASHED" in subject
    # the diagnosis the old message threw away
    assert "GatewayNotReadyError: no managed account" in body
    # it must NOT claim an 8-day gap for a 5-day-old heartbeat
    assert "4.9 days ago" in body
    assert "8 days" not in body
    # points at the file holding the traceback, and warns off the command that lacks it
    assert "/app/data/forward/logs/cron.log" in body
    assert "shows only the scheduler's exit status" in body


def test_crashed_run_survives_a_missing_note(monkeypatch, alerts):
    _with_hb(monkeypatch, _hb("error", 2.0, note=""))
    assert fwd._healthcheck() == 1
    assert "no error detail recorded" in alerts[0][1]


def test_quiet_clock_is_distinct_from_a_crash(monkeypatch, alerts):
    _with_hb(monkeypatch, _hb("ok", 300.0))
    assert fwd._healthcheck() == 1
    subject, body = alerts[0]
    assert "CLOCK QUIET" in subject
    assert "CRASHED" not in subject
    assert "last SUCCEEDED" in body


def test_never_ran_is_its_own_alert(monkeypatch, alerts):
    _with_hb(monkeypatch, None)
    assert fwd._healthcheck() == 1
    assert "NO RUNS RECORDED" in alerts[0][0]


# --- Gateway preflight retry ---------------------------------------------------------------

class _FakeIB:
    """Minimal ib_async.IB stand-in: each instance is scripted with one connect outcome."""

    def __init__(self, script: list):
        self._script = script
        self.accounts: list[str] = []
        self.disconnected = False

    async def connectAsync(self, host, port, clientId):
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.accounts = outcome

    def managedAccounts(self):
        return self.accounts

    def disconnect(self):
        self.disconnected = True


def _fake_ib_factory(monkeypatch, *outcomes):
    """Patch IB() so successive constructions replay `outcomes`; returns the instances made."""
    script, made = list(outcomes), []

    def make():
        ib = _FakeIB(script)
        made.append(ib)
        return ib

    monkeypatch.setattr(ibkr_prices, "IB", make)
    return made


def test_connect_ready_waits_out_a_relogin_window(monkeypatch):
    """Gateway up but account-less (IBC mid-re-login), then healthy — must not lose the week."""
    made = _fake_ib_factory(monkeypatch, [], [], ["DU1234567"])
    ib, acct = asyncio.run(ibkr_prices.connect_ready(attempts=5, delay=0))
    assert acct == "DU1234567"
    assert ib is made[-1]
    assert [m.disconnected for m in made] == [True, True, False]  # aborted attempts cleaned up


def test_connect_ready_retries_a_refused_connection(monkeypatch):
    _fake_ib_factory(monkeypatch, ConnectionRefusedError("no listener"), ["DU1234567"])
    _, acct = asyncio.run(ibkr_prices.connect_ready(attempts=3, delay=0))
    assert acct == "DU1234567"


def test_connect_ready_never_retries_a_live_account(monkeypatch):
    """A live account is a hard stop — one attempt, and the paper-only guard still holds."""
    made = _fake_ib_factory(monkeypatch, ["U7654321"], ["DU1234567"])
    with pytest.raises(ibkr_prices.LiveAccountError):
        asyncio.run(ibkr_prices.connect_ready(attempts=5, delay=0))
    assert len(made) == 1
    assert made[0].disconnected


def test_connect_ready_gives_up_loudly(monkeypatch):
    _fake_ib_factory(monkeypatch, [], [], [])
    with pytest.raises(ibkr_prices.GatewayNotReadyError, match="never became paper-ready"):
        asyncio.run(ibkr_prices.connect_ready(attempts=3, delay=0))
