"""
Daily review: gathers the day's activity (trades, signals, errors, stop
changes) plus market context into a structured JSON + chart PNGs, so an
agent (Claude, in a scheduled cron prompt) can read it and write the
narrative sections -- overview, market commentary, mistakes and corrected
actions -- without needing an ANTHROPIC_API_KEY configured for this
script to call the API unattended.

Two-phase design, mirroring why: this agent's `extract.py` calls Claude
directly for research-note extraction, but that path requires
ANTHROPIC_API_KEY, which isn't set in this environment. Rather than block
on that, the mechanical data-gathering (DB queries, log parsing, market
data, charts) is fully scriptable and needs no LLM judgment; only the
narrative synthesis needs a real read of what happened, which the calling
agent already does better than a templated prompt would.

Usage:
    python scripts/daily_review.py gather
        Writes data/daily_review_work/<date>/data.json and chart PNGs.
        Prints the JSON to stdout too, so an agent can read it directly.

    python scripts/daily_review.py build --narrative <path-to-json>
        Reads data.json + chart PNGs + the narrative JSON (written by the
        agent after reading `gather`'s output), assembles the final PDF,
        and saves it to A:/trading-desk/Reports/daily_review_<date>.pdf.

Narrative JSON shape expected by `build`:
    {
      "overview": "1-2 paragraph summary of the day",
      "market_movement": "commentary on watchlist/market context",
      "mistakes_and_corrections": [
        {"summary": "...", "corrected_action": "..."},
        ...
      ]
    }
"""
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import db
from shared.config import WATCHLIST, KNOWN_ACCOUNTS
from shared.benchmark import compute_benchmark_comparison
from shared.market_data import get_daily_bars, get_daily_bars_cached, get_latest_trade_prices
from execution import alpaca_client

WORK_DIR = ROOT / "data" / "daily_review_work"
REPORTS_DIR = ROOT / "Reports"
LOG_FILES = {
    "market_loop": ROOT / "data" / "trading_day_log.jsonl",
    "rd_agent_daily_cycle": ROOT / "data" / "cycle_log.jsonl",
    "trailing_stop_cycle": ROOT / "data" / "trail_stop_log.jsonl",
}


def _today_str() -> str:
    return datetime.now().date().isoformat()


def _reconstruct_positions(conn, day: str, closes: dict) -> list[dict]:
    """Positions held at the end of `day`, rebuilt from the local order
    ledger and priced at that day's close.

    Only used for backfilled reports. Every trade this system makes is
    recorded here, so the share counts are near-exact; avg entry is a
    quantity-weighted average of buy fills, which is right unless a
    position was partly sold and re-bought.

    Known imprecision: the `orders` table stores the ORDERED quantity, not
    the filled quantity, so a partially filled order is treated as fully
    filled. A backfill can therefore under-report a residual holding by
    the unfilled remainder. Live reports are unaffected -- they read real
    positions from Alpaca.
    """
    # Only orders belonging to THIS account's history. Orders from the
    # sneaky_pivot strategy were submitted to a separate paper account that
    # the user closed on 2026-08-03; including them would resurrect
    # positions (AMD, INTC, NOK) that never existed in the default account
    # and inflate every backfilled report before that date.
    rows = conn.execute(
        """SELECT o.ticker, o.side, o.qty, o.fill_price FROM orders o
           LEFT JOIN signals s ON o.signal_id = s.id
           WHERE o.filled_at IS NOT NULL AND date(o.filled_at) <= ?
             AND COALESCE(s.strategy, '') != 'sneaky_pivot'
           ORDER BY o.filled_at""",
        (day,),
    ).fetchall()

    book: dict[str, dict] = {}
    for r in rows:
        b = book.setdefault(r["ticker"], {"qty": 0.0, "cost": 0.0})
        if r["side"] == "buy":
            b["qty"] += r["qty"]
            b["cost"] += r["qty"] * (r["fill_price"] or 0)
        else:
            # Reduce cost proportionally so avg entry survives a partial sell.
            if b["qty"] > 0:
                b["cost"] *= max(0.0, (b["qty"] - r["qty"])) / b["qty"]
            b["qty"] -= r["qty"]

    positions = []
    for ticker, b in sorted(book.items()):
        if b["qty"] <= 0.001:
            continue
        avg_entry = b["cost"] / b["qty"] if b["qty"] else 0.0
        close = closes.get(ticker)
        positions.append({
            "symbol": ticker,
            "qty": round(b["qty"], 4),
            "avg_entry_price": avg_entry,
            "current_price": close if close is not None else avg_entry,
            "unrealized_pl": (close - avg_entry) * b["qty"] if close is not None else 0.0,
        })
    return positions


def _read_todays_log_entries(path: Path, today: str) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            logged_at = entry.get("logged_at", "")
            if logged_at.startswith(today):
                entries.append(entry)
    return entries


def _flatten_results(raw) -> list[dict]:
    """Normalizes a log entry's `results` field to a flat list of per-
    ticker dicts. Older/single-account scripts log a plain list; trail_stops.py
    started logging {"<account>": [...]} per account once it began
    looping both Alpaca accounts in one invocation (2026-07-28 multi-account
    refactor) -- this was never propagated to this parser, which silently
    iterated the dict's string keys instead of its per-ticker rows.
    Each flattened row is tagged with its source account when the dict form
    is used, so it survives into errors/stop-event output."""
    if isinstance(raw, dict):
        flat = []
        for account, rows in raw.items():
            # Not every dict-shaped result is per-account rows: the market
            # loop's `clock` step logs {"is_open": true, ...}, whose values
            # are scalars. Only descend into lists.
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    flat.append({**row, "account": account})
        return flat
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def _step_results(entry: dict) -> list[dict]:
    """Per-item rows out of a log entry, whichever shape it uses.

    The standalone cycle scripts log `results`/`signals`; the market loop
    wraps each step's return value in `result` (singular). Both are read
    here -- when the loop became the primary driver, the report kept
    parsing only the old keys and silently reported zero errors and zero
    stop activity on days that had plenty of both.
    """
    for key in ("results", "result", "signals"):
        rows = _flatten_results(entry.get(key))
        if rows:
            return rows
    # The loop nests the swing cycle's own summary inside `result`.
    inner = entry.get("result")
    if isinstance(inner, dict):
        for key in ("signals", "execution", "reconciliation"):
            rows = _flatten_results(inner.get(key))
            if rows:
                return rows
    return []


def _extract_errors(source: str, entries: list[dict]) -> list[dict]:
    """Pulls anything that looks like an error out of a cycle log's
    entries, tagged with which source produced it."""
    errors = []
    for entry in entries:
        # A market-loop STEP that failed outright: the loop catches the
        # exception, marks the step status 'error', and keeps cycling.
        if entry.get("status") == "error" and entry.get("error"):
            errors.append({"source": source, "ticker": None,
                           "error": f"{entry.get('event', 'step')}: {entry['error']}",
                           "logged_at": entry.get("logged_at")})
        for key in ("execution_error", "reconciliation_error"):
            if entry.get(key):
                errors.append({"source": source, "ticker": None, "error": entry[key], "logged_at": entry.get("logged_at")})
        inner = entry.get("result")
        if isinstance(inner, dict):
            for key in ("execution_error", "reconciliation_error"):
                if inner.get(key):
                    errors.append({"source": source, "ticker": None, "error": inner[key], "logged_at": entry.get("logged_at")})
        for result in _step_results(entry):
            if result.get("status") == "error":
                errors.append({
                    "source": source,
                    "ticker": result.get("ticker"),
                    "error": result.get("error"),
                    "logged_at": entry.get("logged_at"),
                })
    return errors


def _extract_stop_events(entries: list[dict]) -> list[dict]:
    events = []
    for entry in entries:
        for result in _step_results(entry):
            status = result.get("status")
            if status in ("raised_stop", "placed_initial_stop", "converted_to_gtc", "recovered_from_stuck_chain"):
                events.append({
                    "ticker": result.get("ticker"),
                    "account": result.get("account"),
                    "status": status,
                    "old_stop": result.get("existing_stop"),
                    "new_stop": result.get("new_stop") or result.get("stop_price"),
                    "logged_at": entry.get("logged_at"),
                })
    return events


def gather(day: str | None = None) -> dict:
    """Collect one session's data. `day` defaults to today; pass an older
    YYYY-MM-DD to backfill a session that was missed.

    Backfill is not simply "today's code with a different date filter":
    the account snapshot and open positions only ever describe RIGHT NOW,
    so a naive backfill would file today's equity and today's holdings
    under an old date and quietly misreport history. Equity comes from
    Alpaca's portfolio history instead, positions are rebuilt from the
    order ledger, and prices come from that day's closing bars.
    """
    from datetime import timedelta

    today = _today_str()
    day = day or today
    backfill = day < today

    # Closing bars spanning the session and the one before it (for the
    # prior-close comparison). Not the same-day cache -- that is keyed on
    # today and would be useless for an older date.
    bar_start = datetime.fromisoformat(day) - timedelta(days=12)
    bar_end = datetime.fromisoformat(day) + timedelta(days=1)
    if backfill:
        daily_bars = get_daily_bars(WATCHLIST + ["SPY"], bar_start, bar_end)
    else:
        daily_bars = get_daily_bars_cached(WATCHLIST, datetime.now() - timedelta(days=12), datetime.now())

    def _close_on(ticker, on_day):
        bars = daily_bars.get(ticker)
        if bars is None or bars.empty:
            return None
        sel = bars[bars.index.date <= datetime.fromisoformat(on_day).date()]
        return float(sel["close"].iloc[-1]) if not sel.empty else None

    closes_on_day = {t: _close_on(t, day) for t in WATCHLIST}

    if backfill:
        history = alpaca_client.get_portfolio_history(days=45)
        hist = history.get(day)
        if hist is None:
            raise RuntimeError(
                f"Alpaca portfolio history has no entry for {day} -- it may not have "
                f"been a trading day, or it is outside the available window."
            )
        equity, day_pnl = hist["equity"], hist["pnl"]
        account = type("Snapshot", (), {"equity": equity, "today_pnl": day_pnl, "buying_power": None})()
        with db.db_session() as conn:
            open_positions = _reconstruct_positions(conn, day, closes_on_day)
        account_equity_by_name = {KNOWN_ACCOUNTS[0]: equity}
        spy_price = _close_on("SPY", day)
    else:
        account = alpaca_client.get_account_snapshot()
        open_positions = alpaca_client.get_open_positions()
        account_equity_by_name = {name: alpaca_client.get_account_snapshot(name).equity for name in KNOWN_ACCOUNTS}
        try:
            spy_price = get_latest_trade_prices(["SPY"])["SPY"]
        except Exception:
            spy_price = None

    with db.db_session() as conn:
        orders_today = [
            dict(r) for r in conn.execute(
                """SELECT o.*, s.strategy FROM orders o
                   LEFT JOIN signals s ON o.signal_id = s.id
                   WHERE date(o.submitted_at) = ?
                   ORDER BY o.submitted_at""",
                (day,),
            ).fetchall()
        ]
        signals_today = [
            dict(r) for r in conn.execute(
                "SELECT * FROM signals WHERE date(created_at) = ? ORDER BY created_at",
                (day,),
            ).fetchall()
        ]

        benchmark = {}
        if spy_price is not None:
            for name, equity in account_equity_by_name.items():
                cmp = compute_benchmark_comparison(conn, name, equity, spy_price)
                if cmp is not None:
                    benchmark[name] = asdict(cmp)

    signal_counts: dict[str, int] = {}
    for s in signals_today:
        signal_counts[s["status"]] = signal_counts.get(s["status"], 0) + 1

    all_log_entries = {name: _read_todays_log_entries(path, day) for name, path in LOG_FILES.items()}
    errors = []
    stop_events = []
    for name, entries in all_log_entries.items():
        errors.extend(_extract_errors(name, entries))
        # Stop activity now comes through the market loop's trail_stops
        # step as well as the standalone script's own log.
        if name == "trailing_stop_cycle":
            stop_events.extend(_extract_stop_events(entries))
        elif name == "market_loop":
            stop_events.extend(_extract_stop_events(
                [e for e in entries if e.get("event") == "trail_stops"]))

    # Market context. Live: prior daily close vs the latest trade price --
    # a same-day daily bar may not be finalized right after the close, but
    # the last trade always is. Backfill: that session's close vs the one
    # before it, both settled history.
    if backfill:
        last_trade_prices = {t: c for t, c in closes_on_day.items() if c is not None}
    else:
        try:
            last_trade_prices = get_latest_trade_prices(WATCHLIST)
        except Exception:
            last_trade_prices = {}

    watchlist_moves = []
    for ticker in WATCHLIST:
        bars = daily_bars.get(ticker)
        if bars is None or bars.empty:
            continue
        if backfill:
            prior = bars[bars.index.date < datetime.fromisoformat(day).date()]
            if prior.empty:
                continue
            prior_close = float(prior["close"].iloc[-1])
        else:
            prior_close = float(bars["close"].iloc[-1])
        last_price = last_trade_prices.get(ticker)
        if not last_price:
            continue
        pct_change = (last_price - prior_close) / prior_close * 100
        if abs(pct_change) > 25:
            # Sanity bound: almost certainly a bad/stale data point, not a
            # real single-day move for anything on this watchlist -- flag
            # rather than silently report a number that would undermine
            # the whole report's credibility (a real case hit this with a
            # degenerate bid/ask quote before this switched to last-trade
            # prices; kept as a backstop).
            watchlist_moves.append({"ticker": ticker, "prior_close": prior_close, "last_price": last_price, "pct_change": None, "data_quality_flag": "move looked unreliable, excluded"})
            continue
        watchlist_moves.append({"ticker": ticker, "prior_close": prior_close, "last_price": last_price, "pct_change": pct_change})
    watchlist_moves.sort(key=lambda m: m["pct_change"] if m["pct_change"] is not None else float("-inf"), reverse=True)

    data = {
        "date": day,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backfilled": backfill,
        "account": {
            "equity": account.equity,
            "today_pnl": account.today_pnl,
            "buying_power": account.buying_power,
        },
        "open_positions": open_positions,
        "orders_today": orders_today,
        "signals_today": signals_today,
        "signal_counts": signal_counts,
        "errors": errors,
        "stop_events": stop_events,
        "watchlist_moves": watchlist_moves,
        "cycle_run_counts": {name: len(entries) for name, entries in all_log_entries.items()},
        "benchmark": benchmark,
    }

    work_dir = WORK_DIR / day
    work_dir.mkdir(parents=True, exist_ok=True)
    with open(work_dir / "data.json", "w") as f:
        json.dump(data, f, indent=2, default=str)

    _build_charts(data, work_dir)

    return data


def summarize(data: dict, movers: int = 8) -> dict:
    """Compact digest of a session, for an agent to read.

    `gather` used to print its entire payload to stdout -- 85,000
    characters (~21k tokens) per run, ~60% of it the 500+ entry
    watchlist_moves array. A narrative only ever cites the extremes, so
    shipping every ticker's daily move into an agent's context burned
    usage on every scheduled review for no gain. The full payload still
    goes to data.json on disk, which is what `build` reads.
    """
    moves = [m for m in data.get("watchlist_moves", []) if m.get("pct_change") is not None]
    counts: dict[str, int] = {}
    for e in data.get("stop_events", []):
        counts[e.get("status", "?")] = counts.get(e.get("status", "?"), 0) + 1

    bench = (data.get("benchmark") or {}).get("default") or {}
    return {
        "date": data.get("date"),
        "backfilled": data.get("backfilled", False),
        "account": data.get("account", {}),
        "benchmark": {k: bench.get(k) for k in
                      ("portfolio_return_pct", "benchmark_return_pct", "alpha_pct")} if bench else None,
        "positions": [
            {"symbol": p["symbol"], "qty": p["qty"], "entry": round(p["avg_entry_price"], 2),
             "now": round(p["current_price"], 2), "pl": round(p["unrealized_pl"], 2)}
            for p in data.get("open_positions", [])
        ],
        "signals_today": [
            {"ticker": s["ticker"], "action": s["action"], "status": s["status"],
             "qty": s.get("qty"), "why": (s.get("reasoning") or "")[:70]}
            for s in data.get("signals_today", [])
        ],
        "orders_today": [
            {"ticker": o["ticker"], "side": o["side"], "qty": o["qty"],
             "status": o["status"], "fill": o.get("fill_price")}
            for o in data.get("orders_today", [])
        ],
        "errors": data.get("errors", []),
        "stop_event_counts": counts,
        "top_movers": [{"t": m["ticker"], "pct": round(m["pct_change"], 2)} for m in moves[:movers]],
        "worst_movers": [{"t": m["ticker"], "pct": round(m["pct_change"], 2)} for m in moves[-movers:]],
        "watchlist_size": len(data.get("watchlist_moves", [])),
        "cycle_run_counts": data.get("cycle_run_counts", {}),
        "note": "Compact digest. Full payload at data/daily_review_work/<date>/data.json.",
    }


def _build_charts(data: dict, work_dir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    moves = [m for m in data["watchlist_moves"] if m.get("pct_change") is not None]
    if moves:
        # The watchlist is 500+ names since 2026-08-05; a bar per ticker
        # would be an unreadable smear. Show the extremes, which is what
        # the chart is for -- the full list stays in data.json.
        if len(moves) > 30:
            moves = moves[:15] + moves[-15:]
        fig, ax = plt.subplots(figsize=(8, max(3, len(moves) * 0.35)))
        tickers = [m["ticker"] for m in moves]
        pcts = [m["pct_change"] for m in moves]
        colors = ["#2e7d32" if p >= 0 else "#c62828" for p in pcts]
        ax.barh(tickers, pcts, color=colors)
        ax.axvline(0, color="black", linewidth=0.8)
        ax.set_xlabel("% change vs prior close")
        ax.set_title("Watchlist movement — biggest gainers and losers"
                     if len(data["watchlist_moves"]) > 30 else "Watchlist daily movement")
        fig.tight_layout()
        fig.savefig(work_dir / "watchlist_moves.png", dpi=150)
        plt.close(fig)

    positions = data["open_positions"]
    if positions:
        fig, ax = plt.subplots(figsize=(8, max(3, len(positions) * 0.4)))
        tickers = [p["symbol"] for p in positions]
        pnls = [p["unrealized_pl"] for p in positions]
        colors = ["#2e7d32" if v >= 0 else "#c62828" for v in pnls]
        ax.barh(tickers, pnls, color=colors)
        ax.axvline(0, color="black", linewidth=0.8)
        ax.set_xlabel("Unrealized P&L ($)")
        ax.set_title("Open position P&L")
        fig.tight_layout()
        fig.savefig(work_dir / "position_pnl.png", dpi=150)
        plt.close(fig)


def build(narrative_path: str, day: str | None = None) -> Path:
    day = day or _today_str()
    work_dir = WORK_DIR / day
    with open(work_dir / "data.json") as f:
        data = json.load(f)
    with open(narrative_path) as f:
        narrative = json.load(f)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"daily_review_{day}.pdf"
    _render_pdf(data, narrative, work_dir, out_path)
    return out_path


def _render_pdf(data: dict, narrative: dict, work_dir: Path, out_path: Path) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleCustom", parent=styles["Title"], fontSize=20, spaceAfter=4)
    subtitle_style = ParagraphStyle("Subtitle", parent=styles["Normal"], fontSize=10, textColor=rl_colors.HexColor("#555555"))
    h2 = ParagraphStyle("H2Custom", parent=styles["Heading2"], spaceBefore=16, spaceAfter=8, textColor=rl_colors.HexColor("#1a1a2e"))
    body = ParagraphStyle("BodyCustom", parent=styles["Normal"], fontSize=10.5, leading=15)

    story = []
    story.append(Paragraph("Trading Desk — Daily Review", title_style))
    story.append(Paragraph(f"{data['date']}", subtitle_style))
    story.append(Spacer(1, 0.15 * inch))

    pnl = data["account"]["today_pnl"]
    pnl_color = "#2e7d32" if pnl >= 0 else "#c62828"
    summary_table_data = [
        ["Account equity", f"${data['account']['equity']:,.2f}"],
        ["Today's P&L", f"${pnl:,.2f}"],
        ["Open positions", str(len(data["open_positions"]))],
        ["Signals generated", str(len(data["signals_today"]))],
        ["Orders placed", str(len(data["orders_today"]))],
        ["Errors encountered", str(len(data["errors"]))],
    ]
    t = Table(summary_table_data, colWidths=[2.3 * inch, 2.3 * inch])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10.5),
        ("TEXTCOLOR", (1, 1), (1, 1), rl_colors.HexColor(pnl_color)),
        ("FONTNAME", (1, 1), (1, 1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#dddddd")),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.2 * inch))

    benchmark = data.get("benchmark") or {}
    if benchmark:
        story.append(Paragraph("Performance vs S&amp;P 500", h2))
        bench_rows = [["Account", "Since", "Portfolio Return", "SPY Return", "Alpha"]]
        for name, cmp in benchmark.items():
            alpha = cmp["alpha_pct"]
            bench_rows.append([
                name, cmp["start_date"],
                f"{cmp['portfolio_return_pct']:+.2f}%", f"{cmp['benchmark_return_pct']:+.2f}%",
                f"{alpha:+.2f}%",
            ])
        bt = Table(bench_rows, colWidths=[1.2 * inch, 1.0 * inch, 1.3 * inch, 1.1 * inch, 0.9 * inch])
        style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1a1a2e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#dddddd")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rl_colors.white, rl_colors.HexColor("#f7f7f7")]),
            ("FONTNAME", (4, 1), (4, -1), "Helvetica-Bold"),
        ]
        for i, (name, cmp) in enumerate(benchmark.items(), start=1):
            color = "#2e7d32" if cmp["alpha_pct"] >= 0 else "#c62828"
            style_cmds.append(("TEXTCOLOR", (4, i), (4, i), rl_colors.HexColor(color)))
        bt.setStyle(TableStyle(style_cmds))
        story.append(bt)
        story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Overview", h2))
    story.append(Paragraph(narrative.get("overview", "").replace("\n", "<br/>"), body))

    story.append(Paragraph("Market Movement", h2))
    story.append(Paragraph(narrative.get("market_movement", "").replace("\n", "<br/>"), body))
    moves_chart = work_dir / "watchlist_moves.png"
    if moves_chart.exists():
        story.append(Spacer(1, 0.1 * inch))
        story.append(Image(str(moves_chart), width=6.2 * inch, height=6.2 * inch * 0.55, kind="proportional"))

    if data["open_positions"]:
        story.append(Paragraph("Open Positions", h2))
        pos_rows = [["Ticker", "Qty", "Avg Entry", "Current", "Unrealized P&L"]]
        for p in data["open_positions"]:
            pos_rows.append([
                p["symbol"], f"{p['qty']:g}", f"${p['avg_entry_price']:.2f}",
                f"${p['current_price']:.2f}", f"${p['unrealized_pl']:,.2f}",
            ])
        pt = Table(pos_rows, colWidths=[0.9 * inch, 0.7 * inch, 1.1 * inch, 1.1 * inch, 1.3 * inch])
        pt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1a1a2e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#dddddd")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rl_colors.white, rl_colors.HexColor("#f7f7f7")]),
        ]))
        story.append(pt)
        pnl_chart = work_dir / "position_pnl.png"
        if pnl_chart.exists():
            story.append(Spacer(1, 0.15 * inch))
            story.append(Image(str(pnl_chart), width=6.2 * inch, height=6.2 * inch * 0.5, kind="proportional"))

    if data["orders_today"]:
        story.append(Paragraph("Orders Today", h2))
        order_rows = [["Time", "Ticker", "Side", "Qty", "Strategy", "Status"]]
        for o in data["orders_today"]:
            order_rows.append([
                str(o.get("submitted_at", ""))[11:19], o["ticker"], o["side"].upper(),
                f"{o['qty']:g}", o.get("strategy") or "-", o["status"],
            ])
        ot = Table(order_rows, colWidths=[0.8 * inch, 0.8 * inch, 0.6 * inch, 0.6 * inch, 1.4 * inch, 1.2 * inch])
        ot.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1a1a2e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#dddddd")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rl_colors.white, rl_colors.HexColor("#f7f7f7")]),
        ]))
        story.append(ot)

    story.append(Paragraph("Mistakes &amp; Corrected Actions", h2))
    mistakes = narrative.get("mistakes_and_corrections", [])
    if not mistakes:
        story.append(Paragraph("No notable mistakes identified today.", body))
    else:
        for m in mistakes:
            story.append(Paragraph(f"<b>{m['summary']}</b>", body))
            story.append(Paragraph(f"Corrected action: {m['corrected_action']}", body))
            story.append(Spacer(1, 0.08 * inch))

    if data["errors"]:
        story.append(Paragraph("Errors Encountered (raw log)", h2))
        err_cell_style = ParagraphStyle("ErrCell", parent=styles["Normal"], fontSize=8.5, leading=10.5)
        err_rows = [["Time", "Source", "Ticker", "Error"]]
        for e in data["errors"][:20]:
            err_rows.append([
                str(e.get("logged_at", ""))[11:19], e["source"], e.get("ticker") or "-",
                Paragraph((e.get("error") or "")[:200], err_cell_style),
            ])
        et = Table(err_rows, colWidths=[0.7 * inch, 1.3 * inch, 0.6 * inch, 3.6 * inch])
        et.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#c62828")),
            ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
            ("FONTSIZE", (0, 0), (-1, 0), 8.5),
            ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#dddddd")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(et)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        title=f"Trading Desk Daily Review {data['date']}",
    )
    doc.build(story)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("gather", "build"):
        print("Usage: python daily_review.py gather [--date YYYY-MM-DD]")
        print("       python daily_review.py build --narrative <path> [--date YYYY-MM-DD]")
        sys.exit(1)

    day = sys.argv[sys.argv.index("--date") + 1] if "--date" in sys.argv else None

    if sys.argv[1] == "gather":
        result = gather(day)
        # Compact by default -- see summarize(). --full restores the old
        # complete dump for debugging, but never use it in a scheduled
        # task: it is ~21k tokens a run.
        if "--full" in sys.argv:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(json.dumps(summarize(result), indent=2, default=str))
    else:
        if "--narrative" not in sys.argv:
            print("build requires --narrative <path-to-json>")
            sys.exit(1)
        narrative_path = sys.argv[sys.argv.index("--narrative") + 1]
        out = build(narrative_path, day)
        print(f"Report saved to {out}")
