"""
Local live dashboard for the trading desk: one page, auto-refreshing, at
http://127.0.0.1:8787 (localhost only).

READ-ONLY BY CONSTRUCTION: this server exposes no action of any kind --
no order placement, no signal writes, no config changes. It renders the
same state the agents act on, plus an "attention" panel of derived
anomaly checks (stuck pending signals, unprotected positions, wedged
stop orders, stale loop heartbeat, unreconciled orders) -- each one a
failure mode this system has actually hit.

Endpoints:
    GET /            the dashboard page (analyst/dashboard.html)
    GET /api/state   full JSON state (server-side cached ~20s so the
                     browser poll doesn't hammer Alpaca)
    GET /reports/<name>  serves files from Reports/ (PDFs, weekly R&D md)

Usage:
    venv/Scripts/python analyst/dashboard.py           # port 8787
    venv/Scripts/python analyst/dashboard.py --port N
"""
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import db
from shared.benchmark import compute_benchmark_comparison
from shared.config import KNOWN_ACCOUNTS, WATCHLIST

REPORTS_DIR = ROOT / "Reports"
HTML_PATH = Path(__file__).resolve().parent / "dashboard.html"
LOG_FILES = {
    "market_loop": ROOT / "data" / "trading_day_log.jsonl",
    "swing_cycle": ROOT / "data" / "cycle_log.jsonl",
    "sneaky_pivot_cycle": ROOT / "data" / "sneaky_pivot_log.jsonl",
    "trail_stops": ROOT / "data" / "trail_stop_log.jsonl",
    "rd_nightly": ROOT / "data" / "rd_nightly_log.jsonl",
}
CACHE_TTL_SECONDS = 20

_cache: dict = {"state": None, "at": 0.0}
_cache_lock = threading.Lock()


def _tail_jsonl(path: Path, n: int = 50) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines()[-n:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _section(fn):
    """Each dashboard section fails independently -- a dead Alpaca
    connection must not blank the sections that only need the local DB."""
    try:
        return fn()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _accounts_section():
    from dataclasses import asdict

    from execution import alpaca_client
    from shared.market_data import get_latest_trade_prices

    try:
        spy_price = get_latest_trade_prices(["SPY"])["SPY"]
    except Exception:
        spy_price = None

    accounts = {}
    with db.db_session() as conn:
        for name in KNOWN_ACCOUNTS:
            snap = alpaca_client.get_account_snapshot(account=name)
            positions = alpaca_client.get_open_positions(account=name)
            stops = alpaca_client.get_open_stop_orders(account=name)
            benchmark = None
            if spy_price is not None:
                cmp = compute_benchmark_comparison(conn, name, snap.equity, spy_price)
                if cmp is not None:
                    benchmark = asdict(cmp)
            accounts[name] = {
                "equity": snap.equity,
                "today_pnl": snap.today_pnl,
                "buying_power": snap.buying_power,
                "daytrade_count": snap.daytrade_count,
                "is_pdt_flagged": snap.is_pdt_flagged,
                "positions": positions,
                "stops": stops,
                "benchmark": benchmark,
            }
    return accounts


def _clock_section():
    from execution import alpaca_client
    clock = alpaca_client.get_market_clock()
    return {k: str(v) if not isinstance(v, bool) else v for k, v in clock.items()}


def _db_section():
    today = datetime.now().strftime("%Y-%m-%d")
    with db.db_session() as conn:
        signals_today = [dict(r) for r in conn.execute(
            "SELECT * FROM signals WHERE date(created_at) = ? ORDER BY created_at DESC", (today,)
        ).fetchall()]
        pending_signals = [dict(r) for r in conn.execute(
            "SELECT * FROM signals WHERE status = 'pending'"
        ).fetchall()]
        orders_today = [dict(r) for r in conn.execute(
            """SELECT o.*, s.strategy FROM orders o LEFT JOIN signals s ON o.signal_id = s.id
               WHERE date(o.submitted_at) = ? ORDER BY o.submitted_at DESC""", (today,)
        ).fetchall()]
        unreconciled = [dict(r) for r in conn.execute(
            """SELECT o.id, o.ticker, o.side, o.qty, o.submitted_at, o.status FROM orders o
               WHERE o.alpaca_order_id IS NOT NULL AND o.filled_at IS NULL
               AND o.status NOT IN ('canceled', 'replaced', 'expired', 'rejected')
               AND o.submitted_at < datetime('now', '-1 hour')"""
        ).fetchall()]
        rejected_recent = [dict(r) for r in conn.execute(
            """SELECT id, ticker, action, strategy, reasoning, updated_at FROM signals
               WHERE status = 'rejected' AND updated_at >= datetime('now', '-3 days')
               ORDER BY updated_at DESC LIMIT 20"""
        ).fetchall()]
        backtests = [dict(r) for r in conn.execute(
            "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT 12"
        ).fetchall()]
        proposals = [dict(r) for r in conn.execute(
            "SELECT * FROM research_notes WHERE status = 'unreviewed' ORDER BY created_at DESC LIMIT 20"
        ).fetchall()]
    return {
        "signals_today": signals_today,
        "pending_signals": pending_signals,
        "orders_today": orders_today,
        "unreconciled_orders": unreconciled,
        "rejected_signals_recent": rejected_recent,
        "backtest_runs": backtests,
        "unreviewed_proposals": proposals,
    }


def _logs_section():
    today = datetime.now(timezone.utc).date().isoformat()
    out = {}
    errors_today = []
    for name, path in LOG_FILES.items():
        entries = _tail_jsonl(path, 80)
        out[name] = {
            "last_entry_at": entries[-1].get("logged_at") if entries else None,
            "last_event": entries[-1].get("event") or entries[-1].get("step") if entries else None,
            "entries_today": sum(1 for e in entries if str(e.get("logged_at", "")).startswith(today)),
        }
        for e in entries:
            if not str(e.get("logged_at", "")).startswith(today):
                continue
            if e.get("status") == "error" or e.get("error") or e.get("execution_error") or e.get("reconciliation_error"):
                errors_today.append({
                    "source": name,
                    "logged_at": e.get("logged_at"),
                    "detail": e.get("error") or e.get("execution_error") or e.get("reconciliation_error") or str(e.get("result"))[:200],
                })
    out["errors_today"] = errors_today[-20:]
    out["rd_nightly_tail"] = _tail_jsonl(LOG_FILES["rd_nightly"], 7)
    return out


def _corrections_section():
    """Latest daily-review narrative's mistakes & corrections, if any."""
    work_root = ROOT / "data" / "daily_review_work"
    if not work_root.exists():
        return {"narrative_date": None, "mistakes": []}
    days = sorted((d for d in work_root.iterdir() if d.is_dir()), reverse=True)
    for day in days[:5]:
        narrative = day / "narrative.json"
        if narrative.exists():
            try:
                data = json.loads(narrative.read_text())
                return {"narrative_date": day.name,
                        "mistakes": data.get("mistakes_and_corrections", [])}
            except json.JSONDecodeError:
                continue
    return {"narrative_date": None, "mistakes": []}


def _reports_section():
    if not REPORTS_DIR.exists():
        return []
    files = sorted(REPORTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": f.name, "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes")}
            for f in files[:14] if f.is_file()]


def _attention(state: dict) -> list[dict]:
    """Derived anomaly checks. Severity: critical > warning > info.
    Every check here corresponds to a failure mode this system has
    actually experienced -- see the git/SKILL history for each."""
    items = []
    dbs = state.get("db", {})
    logs = state.get("logs", {})
    accounts = state.get("accounts", {})
    clock = state.get("clock", {})
    market_open = clock.get("is_open") is True

    for sig in dbs.get("pending_signals", []) if isinstance(dbs, dict) else []:
        items.append({"severity": "warning",
                      "what": f"Signal #{sig['id']} ({sig['action']} {sig['ticker']}, {sig['strategy']}) is stuck 'pending'",
                      "why": "Pending signals execute on the NEXT live pass -- a stale one can fire days later (the 2026-07-29 MSFT incident)."})

    if isinstance(accounts, dict):
        for name, acct in accounts.items():
            if "error" in acct:
                items.append({"severity": "critical", "what": f"Account '{name}' unreachable", "why": acct["error"]})
                continue
            stops = acct.get("stops", {})
            for pos in acct.get("positions", []):
                stop = stops.get(pos["symbol"])
                if stop is None:
                    items.append({"severity": "critical",
                                  "what": f"{pos['symbol']} ({name}): position with NO stop order",
                                  "why": "Unprotected until the next trail_stops cycle places one."})
                else:
                    if stop.get("time_in_force") == "day":
                        items.append({"severity": "warning",
                                      "what": f"{pos['symbol']} ({name}): stop is DAY-TIF",
                                      "why": "Expires at the close unless trail_stops converts it to GTC (the 2026-07-27 AMD expiry bug)."})
                    if stop.get("status") not in ("new", "accepted"):
                        items.append({"severity": "warning",
                                      "what": f"{pos['symbol']} ({name}): stop order in '{stop.get('status')}'",
                                      "why": "Transitional state; the last-known-good stop is still enforced by the broker, but it can't be raised until this clears."})

    if isinstance(logs, dict):
        hb = logs.get("market_loop", {}).get("last_entry_at")
        if market_open:
            stale = True
            if hb:
                try:
                    age = datetime.now(timezone.utc) - datetime.fromisoformat(hb)
                    stale = age > timedelta(minutes=20)
                except ValueError:
                    pass
            if stale:
                items.append({"severity": "critical",
                              "what": "Market is OPEN but the trading loop hasn't logged in 20+ minutes",
                              "why": "TradingDeskMarketLoop may not be running -- check Task Scheduler."})
        n_err = len(logs.get("errors_today", []))
        if n_err:
            items.append({"severity": "warning", "what": f"{n_err} error(s) in today's logs",
                          "why": "See the Corrections & Errors panel."})

    for o in (dbs.get("unreconciled_orders", []) if isinstance(dbs, dict) else []):
        items.append({"severity": "warning",
                      "what": f"Order #{o['id']} ({o['side']} {o['ticker']}) unreconciled for 1h+",
                      "why": "Local ledger may be stale vs Alpaca; reconcile_orders should have caught this."})

    n_prop = len(dbs.get("unreviewed_proposals", [])) if isinstance(dbs, dict) else 0
    if n_prop:
        items.append({"severity": "info", "what": f"{n_prop} R&D proposal(s) await your review",
                      "why": "Nothing changes until reviewed: venv/Scripts/python analyst/review_notes.py"})

    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(items, key=lambda i: order[i["severity"]])


def build_state() -> dict:
    state = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "watchlist": WATCHLIST,
        "clock": _section(_clock_section),
        "accounts": _section(_accounts_section),
        "db": _section(_db_section),
        "logs": _section(_logs_section),
        "corrections": _section(_corrections_section),
        "reports": _section(_reports_section),
    }
    state["attention"] = _attention(state)
    return state


def get_state_cached() -> dict:
    with _cache_lock:
        if _cache["state"] is not None and time.time() - _cache["at"] < CACHE_TTL_SECONDS:
            return _cache["state"]
    state = build_state()
    with _cache_lock:
        _cache["state"] = state
        _cache["at"] = time.time()
    return state


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            body = json.dumps(get_state_cached(), default=str).encode()
            self._send(200, body, "application/json")
        elif self.path.startswith("/reports/"):
            name = Path(self.path[len("/reports/"):]).name  # strips any traversal
            f = REPORTS_DIR / name
            if f.is_file():
                ctype = "application/pdf" if f.suffix == ".pdf" else "text/plain; charset=utf-8"
                self._send(200, f.read_bytes(), ctype)
            else:
                self._send(404, b"not found", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")


def main():
    port = 8787
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Trading desk dashboard: http://127.0.0.1:{port} (read-only, localhost only)")
    server.serve_forever()


if __name__ == "__main__":
    main()
