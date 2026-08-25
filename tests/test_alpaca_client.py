"""
Tests for execution/alpaca_client.py -- the only module that talks to the
broker. No network: every test monkeypatches _get_trading_client to return
a fake client built from plain objects, so what's under test is this
module's own response-parsing and pagination/dedup logic, not Alpaca's
SDK. Real credentials/network are never touched (see also
execution/smoke_test.py for that against a real paper account).
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from execution import alpaca_client
from shared import config as shared_config


# ------------------------------------------------------------ credentials

def test_credentials_for_unknown_account_raises():
    with pytest.raises(ValueError):
        alpaca_client._credentials_for("some_other_account")


def test_credentials_for_default_requires_alpaca_credentials(monkeypatch):
    # require_alpaca_credentials() checks shared.config's own module-level
    # names directly (not execution.alpaca_client's imported copies) -- see
    # the sibling test below. Patching only alpaca_client's copies leaves
    # the real .env-loaded credentials in place for require_alpaca_credentials
    # to see, so it never raises -- confirmed real: this test passed for the
    # wrong reason (or rather didn't fail for the wrong reason) until run
    # against a repo with real credentials configured.
    monkeypatch.setattr(shared_config, "ALPACA_API_KEY", "")
    monkeypatch.setattr(shared_config, "ALPACA_SECRET_KEY", "")
    monkeypatch.setattr(alpaca_client, "ALPACA_API_KEY", "")
    monkeypatch.setattr(alpaca_client, "ALPACA_SECRET_KEY", "")
    with pytest.raises(RuntimeError):
        alpaca_client._credentials_for("default")


def test_credentials_for_default_returns_configured_keys(monkeypatch):
    # require_alpaca_credentials() checks shared.config's own module-level
    # names directly, while _credentials_for reads the copies
    # execution.alpaca_client imported into its own namespace -- both must
    # be patched, same reasoning as shared.db's DB_PATH binding.
    monkeypatch.setattr(shared_config, "ALPACA_API_KEY", "key123")
    monkeypatch.setattr(shared_config, "ALPACA_SECRET_KEY", "secret123")
    monkeypatch.setattr(alpaca_client, "ALPACA_API_KEY", "key123")
    monkeypatch.setattr(alpaca_client, "ALPACA_SECRET_KEY", "secret123")
    key, secret = alpaca_client._credentials_for("default")
    assert (key, secret) == ("key123", "secret123")


# --------------------------------------------------------- account snapshot

def test_get_account_snapshot_computes_today_pnl(monkeypatch):
    fake_account = SimpleNamespace(
        equity="105000.50", last_equity="100000.00", buying_power="200000.00",
        cash="-4467.00", multiplier="4", pattern_day_trader=True, daytrade_count=2,
    )
    fake_client = SimpleNamespace(get_account=lambda: fake_account)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    snap = alpaca_client.get_account_snapshot()

    assert snap.equity == 105000.50
    assert snap.last_equity == 100000.00
    assert snap.today_pnl == pytest.approx(5000.50)
    assert snap.cash == -4467.00  # negative cash (margin debit) preserved, not clamped
    assert snap.multiplier == 4
    assert snap.is_pdt_flagged is True
    assert snap.daytrade_count == 2


def test_get_account_snapshot_defaults_multiplier_and_daytrade_count_when_none(monkeypatch):
    fake_account = SimpleNamespace(
        equity="1000", last_equity="1000", buying_power="1000",
        cash="1000", multiplier=None, pattern_day_trader=False, daytrade_count=None,
    )
    fake_client = SimpleNamespace(get_account=lambda: fake_account)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    snap = alpaca_client.get_account_snapshot()

    assert snap.multiplier == 1
    assert snap.daytrade_count == 0


# ------------------------------------------------------------- market clock

def test_get_market_clock_parses_fields(monkeypatch):
    fake_clock = SimpleNamespace(is_open=True, timestamp="2026-08-19T14:30:00Z",
                                  next_open="2026-08-20T13:30:00Z", next_close="2026-08-19T20:00:00Z")
    fake_client = SimpleNamespace(get_clock=lambda: fake_clock)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    clock = alpaca_client.get_market_clock()

    assert clock == {
        "is_open": True,
        "timestamp": "2026-08-19T14:30:00Z",
        "next_open": "2026-08-20T13:30:00Z",
        "next_close": "2026-08-19T20:00:00Z",
    }


# -------------------------------------------------------------- positions

def test_get_open_positions_parses_floats(monkeypatch):
    fake_position = SimpleNamespace(
        symbol="NVDA", qty="10", avg_entry_price="190.00", current_price="200.00", unrealized_pl="100.00",
    )
    fake_client = SimpleNamespace(get_all_positions=lambda: [fake_position])
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    positions = alpaca_client.get_open_positions()

    assert positions == [{
        "symbol": "NVDA", "qty": 10.0, "avg_entry_price": 190.0,
        "current_price": 200.0, "unrealized_pl": 100.0,
    }]


def test_get_open_positions_empty(monkeypatch):
    fake_client = SimpleNamespace(get_all_positions=lambda: [])
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)
    assert alpaca_client.get_open_positions() == []


# ---------------------------------------------------------------- orders

def test_submit_market_order_returns_expected_fields(monkeypatch):
    fake_order = SimpleNamespace(
        id="ord-1", symbol="AAPL", qty="10", status="accepted", submitted_at="2026-08-19T14:31:00Z",
    )
    fake_client = SimpleNamespace(submit_order=lambda request: fake_order)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.submit_market_order("AAPL", 10, "buy")

    assert result == {
        "alpaca_order_id": "ord-1", "symbol": "AAPL", "side": "buy",
        "qty": 10.0, "status": "accepted", "submitted_at": "2026-08-19T14:31:00Z",
    }


def test_place_protective_stop_returns_expected_fields(monkeypatch):
    fake_order = SimpleNamespace(id="ord-stop-1", symbol="AAPL", status="new")
    fake_client = SimpleNamespace(submit_order=lambda request: fake_order)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.place_protective_stop("AAPL", 10, 149.999)

    assert result["alpaca_order_id"] == "ord-stop-1"
    assert result["status"] == "new"
    # stop_price echoed back is the caller's rounded input, not re-derived
    # from the order object -- confirm the rounding actually happened.
    assert result["stop_price"] == 149.999


def test_get_open_buy_order_symbols(monkeypatch):
    fake_orders = [SimpleNamespace(symbol="AAPL"), SimpleNamespace(symbol="MSFT")]
    fake_client = SimpleNamespace(get_orders=lambda request: fake_orders)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    assert alpaca_client.get_open_buy_order_symbols() == {"AAPL", "MSFT"}


def test_cancel_open_orders_for_symbol_cancels_each_and_returns_ids(monkeypatch):
    fake_orders = [SimpleNamespace(id="ord-1"), SimpleNamespace(id="ord-2")]
    cancelled_ids = []
    fake_client = SimpleNamespace(
        get_orders=lambda request: fake_orders,
        cancel_order_by_id=lambda order_id: cancelled_ids.append(order_id),
    )
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.cancel_open_orders_for_symbol("AAPL")

    assert result == ["ord-1", "ord-2"]
    assert cancelled_ids == ["ord-1", "ord-2"]


def test_cancel_open_orders_for_symbol_no_open_orders(monkeypatch):
    fake_client = SimpleNamespace(get_orders=lambda request: [])
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)
    assert alpaca_client.cancel_open_orders_for_symbol("AAPL") == []


# ---------------------------------------------------------- open stop orders

def _stop_order(symbol, order_id, status, submitted_at, stop_price=100.0, qty=10.0,
                 order_type="stop", tif="gtc"):
    return SimpleNamespace(
        symbol=symbol, id=order_id, order_type=order_type, time_in_force=tif,
        status=status, stop_price=stop_price, qty=qty, submitted_at=submitted_at,
    )


def test_get_open_stop_orders_filters_non_stop_order_types(monkeypatch):
    orders = [
        _stop_order("AAPL", "ord-1", "new", "2026-08-19T10:00:00Z", order_type="stop"),
        _stop_order("MSFT", "ord-2", "new", "2026-08-19T10:00:00Z", order_type="limit"),
    ]
    fake_client = SimpleNamespace(get_orders=lambda request: orders)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.get_open_stop_orders()

    assert set(result.keys()) == {"AAPL"}


def test_get_open_stop_orders_prefers_stable_over_pending_replace(monkeypatch):
    # Regression guard: a replace can leave the OLD order stuck in
    # pending_replace while a NEW order with the updated price is already
    # live. Naive "last one wins" can pick the stale pending order.
    orders = [
        _stop_order("AAPL", "ord-old", "pending_replace", "2026-08-19T10:00:00Z", stop_price=95.0),
        _stop_order("AAPL", "ord-new", "new", "2026-08-19T09:00:00Z", stop_price=98.0),
    ]
    fake_client = SimpleNamespace(get_orders=lambda request: orders)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.get_open_stop_orders()

    assert result["AAPL"]["alpaca_order_id"] == "ord-new"
    assert result["AAPL"]["stop_price"] == 98.0


def test_get_open_stop_orders_picks_most_recent_among_equally_stable(monkeypatch):
    orders = [
        _stop_order("AAPL", "ord-earlier", "new", "2026-08-19T09:00:00Z"),
        _stop_order("AAPL", "ord-later", "new", "2026-08-19T10:00:00Z"),
    ]
    fake_client = SimpleNamespace(get_orders=lambda request: orders)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.get_open_stop_orders()

    assert result["AAPL"]["alpaca_order_id"] == "ord-later"


def test_get_open_stop_orders_keeps_first_pending_replace_over_later_pending_replace_of_other_symbol(monkeypatch):
    # Sanity: dedup logic is keyed per-symbol, unrelated symbols don't interact.
    orders = [
        _stop_order("AAPL", "ord-a", "new", "2026-08-19T09:00:00Z"),
        _stop_order("MSFT", "ord-b", "pending_replace", "2026-08-19T09:00:00Z"),
    ]
    fake_client = SimpleNamespace(get_orders=lambda request: orders)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.get_open_stop_orders()

    assert result["AAPL"]["alpaca_order_id"] == "ord-a"
    assert result["MSFT"]["alpaca_order_id"] == "ord-b"


# ----------------------------------------------------------- replace stop

def test_replace_stop_order_rounds_and_echoes_new_price(monkeypatch):
    # qty=None on the fake order matches a real Alpaca order object's shape
    # for a price-only replace (qty is a real attribute, not absent) --
    # added when replace_stop_order grew qty-echoing (2026-08-25, the
    # stop-qty-drift fix); a SimpleNamespace missing .qty entirely used to
    # raise AttributeError here instead of exercising the "no qty requested"
    # branch this test is actually meant to cover.
    fake_order = SimpleNamespace(id="ord-1", symbol="AAPL", status="new", qty=None)
    captured = {}

    def fake_replace(order_id, request):
        captured["order_id"] = order_id
        captured["stop_price"] = request.stop_price
        captured["tif"] = request.time_in_force
        captured["qty_in_request"] = getattr(request, "qty", None)
        return fake_order

    fake_client = SimpleNamespace(replace_order_by_id=fake_replace)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.replace_stop_order("ord-1", 123.456)

    assert captured["order_id"] == "ord-1"
    assert captured["stop_price"] == 123.46  # rounded to cents
    from alpaca.trading.enums import TimeInForce
    assert captured["tif"] == TimeInForce.GTC  # always forced to GTC regardless of prior TIF
    assert captured["qty_in_request"] is None  # no qty passed in -> not included in the replace request
    assert result["stop_price"] == 123.456  # echoed value is the caller's raw input
    assert result["qty"] is None  # order.qty was None and no qty was requested


def test_replace_stop_order_also_corrects_qty_when_given(monkeypatch):
    fake_order = SimpleNamespace(id="ord-2", symbol="BA", status="new", qty="26")
    captured = {}

    def fake_replace(order_id, request):
        captured["qty_in_request"] = request.qty
        return fake_order

    fake_client = SimpleNamespace(replace_order_by_id=fake_replace)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.replace_stop_order("ord-2", 199.89, qty=26.0)

    assert captured["qty_in_request"] == 26.0
    assert result["qty"] == 26.0  # echoes the broker's confirmed qty, not just the caller's request


# ------------------------------------------------------- filled orders since

def _filled_order(order_id, symbol, submitted_at, filled_at="2026-08-19T15:00:00Z",
                   status="filled", side="sell", order_type="stop", qty=10.0, filled_qty=10.0,
                   filled_avg_price=100.0):
    return SimpleNamespace(
        id=order_id, symbol=symbol, side=side, order_type=order_type,
        qty=qty, filled_qty=filled_qty, filled_avg_price=filled_avg_price,
        filled_at=filled_at, submitted_at=submitted_at, status=status,
    )


def test_get_filled_orders_since_excludes_non_filled_statuses(monkeypatch):
    orders = [
        _filled_order("ord-1", "AAPL", "2026-08-19T10:00:00Z", status="filled"),
        _filled_order("ord-2", "AAPL", "2026-08-19T10:00:00Z", status="replaced"),
        _filled_order("ord-3", "AAPL", "2026-08-19T10:00:00Z", status="canceled"),
    ]

    def fake_get_orders(request):
        return orders if request.until is None else []

    fake_client = SimpleNamespace(get_orders=fake_get_orders)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.get_filled_orders_since("2026-08-01")

    assert [o["alpaca_order_id"] for o in result] == ["ord-1"]


def test_get_filled_orders_since_paginates_and_dedups_boundary_overlap(monkeypatch):
    # Regression guard for the real 2026-08-17 bug: a single unpaginated
    # page fills up with 'replaced' noise from routine stop-trailing before
    # reaching far enough back to surface an older real fill. Page 1 here
    # is exactly `limit` (500) long, which is what makes the real code
    # continue to a second page at all (`if len(page) < 500: break`); the
    # oldest order in page 1 re-appears at the top of page 2 (the `until`
    # cursor is inclusive-ish) and must be deduped, not double-counted.
    page1 = [
        _filled_order(f"ord-{i}", "AAPL", f"2026-08-19T{i % 24:02d}:{i % 60:02d}:00Z")
        for i in range(500)
    ]
    boundary = min(page1, key=lambda o: o.submitted_at)
    page2 = [boundary, _filled_order("ord-old", "AAPL", "2026-08-05T10:00:00Z")]

    calls = {"n": 0}

    def fake_get_orders(request):
        calls["n"] += 1
        return page1 if calls["n"] == 1 else page2

    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": SimpleNamespace(get_orders=fake_get_orders))

    result = alpaca_client.get_filled_orders_since("2026-08-01")

    ids = [o["alpaca_order_id"] for o in result]
    assert calls["n"] == 2  # actually paginated past the first full page
    assert len(ids) == len(set(ids))  # boundary overlap deduped, not double-counted
    assert len(ids) == 501  # 500 from page1 + 1 genuinely new from page2
    assert "ord-old" in ids


def test_get_filled_orders_since_parses_numeric_fields(monkeypatch):
    orders = [_filled_order("ord-1", "AAPL", "2026-08-19T10:00:00Z", qty=13.0, filled_qty=11.0,
                             filled_avg_price=101.5)]
    fake_client = SimpleNamespace(get_orders=lambda request: orders)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.get_filled_orders_since("2026-08-01")

    assert result[0]["qty"] == 13.0
    assert result[0]["filled_qty"] == 11.0
    assert result[0]["filled_avg_price"] == 101.5


def test_get_filled_orders_since_handles_zero_filled_qty(monkeypatch):
    # filled_qty of 0/None must come back as 0.0, not None -- downstream
    # reconciliation math (e.g. FIFO P&L) can't safely add None.
    orders = [_filled_order("ord-1", "AAPL", "2026-08-19T10:00:00Z", filled_qty=None)]
    fake_client = SimpleNamespace(get_orders=lambda request: orders)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.get_filled_orders_since("2026-08-01")

    assert result[0]["filled_qty"] == 0.0


# --------------------------------------------------------------- order status

def test_get_order_status_parses_fields(monkeypatch):
    fake_order = SimpleNamespace(
        id="ord-1", status="filled", filled_qty="10", filled_avg_price="150.25",
        filled_at="2026-08-19T15:00:00Z",
    )
    fake_client = SimpleNamespace(get_order_by_id=lambda order_id: fake_order)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.get_order_status("ord-1")

    assert result == {
        "alpaca_order_id": "ord-1", "status": "filled", "filled_qty": 10.0,
        "filled_avg_price": 150.25, "filled_at": "2026-08-19T15:00:00Z",
    }


def test_get_order_status_handles_unfilled_order(monkeypatch):
    fake_order = SimpleNamespace(
        id="ord-1", status="new", filled_qty=None, filled_avg_price=None, filled_at=None,
    )
    fake_client = SimpleNamespace(get_order_by_id=lambda order_id: fake_order)
    monkeypatch.setattr(alpaca_client, "_get_trading_client", lambda account="default": fake_client)

    result = alpaca_client.get_order_status("ord-1")

    assert result["filled_qty"] == 0.0
    assert result["filled_avg_price"] is None
    assert result["filled_at"] is None
