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

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rd-agent"))
sys.path.insert(0, str(ROOT / "execution-agent"))

from shared import db
from shared.config import WATCHLIST, KNOWN_ACCOUNTS
from shared.benchmark import compute_benchmark_comparison
from data.alpaca_data import get_daily_bars_cached, get_latest_trade_prices
import alpaca_client

WORK_DIR = ROOT / "data" / "daily_review_work"
REPORTS_DIR = ROOT / "Reports"
LOG_FILES = {
    "rd_agent_daily_cycle": ROOT / "data" / "cycle_log.jsonl",
    "sneaky_pivot_cycle": ROOT / "data" / "sneaky_pivot_log.jsonl",
    "trailing_stop_cycle": ROOT / "data" / "trail_stop_log.jsonl",
}


def _today_str() -> str:
    return datetime.now().date().isoformat()


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
    started logging {"default": [...], "sneaky_pivot": [...]} once it began
    looping both Alpaca accounts in one invocation (2026-07-28 multi-account
    refactor) -- this was never propagated to this parser, which silently
    iterated the dict's string keys instead of its per-ticker rows.
    Each flattened row is tagged with its source account when the dict form
    is used, so it survives into errors/stop-event output."""
    if isinstance(raw, dict):
        flat = []
        for account, rows in raw.items():
            for row in rows or []:
                if isinstance(row, dict):
                    flat.append({**row, "account": account})
        return flat
    return [r for r in (raw or []) if isinstance(r, dict)]


def _extract_errors(source: str, entries: list[dict]) -> list[dict]:
    """Pulls anything that looks like a per-ticker error or a top-level
    execution/reconciliation error out of a cycle log's entries, tagged
    with which source produced it."""
    errors = []
    for entry in entries:
        for key in ("execution_error", "reconciliation_error"):
            if entry.get(key):
                errors.append({"source": source, "ticker": None, "error": entry[key], "logged_at": entry.get("logged_at")})
        results = _flatten_results(entry.get("results")) or _flatten_results(entry.get("signals"))
        for result in results:
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
        for result in _flatten_results(entry.get("results")):
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


def gather() -> dict:
    today = _today_str()

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
                (today,),
            ).fetchall()
        ]
        signals_today = [
            dict(r) for r in conn.execute(
                "SELECT * FROM signals WHERE date(created_at) = ? ORDER BY created_at",
                (today,),
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

    all_log_entries = {name: _read_todays_log_entries(path, today) for name, path in LOG_FILES.items()}
    errors = []
    stop_events = []
    for name, entries in all_log_entries.items():
        errors.extend(_extract_errors(name, entries))
        if name == "trailing_stop_cycle":
            stop_events.extend(_extract_stop_events(entries))

    # Market context: prior close from cached daily bars, current/last
    # price from a live quote -- a same-day daily bar may not be finalized
    # yet even right after close, but the latest quote will be.
    from datetime import timedelta
    daily_bars = get_daily_bars_cached(WATCHLIST, start=datetime.now() - timedelta(days=10), end=datetime.now())
    try:
        last_trade_prices = get_latest_trade_prices(WATCHLIST)
    except Exception:
        last_trade_prices = {}

    watchlist_moves = []
    for ticker in WATCHLIST:
        bars = daily_bars.get(ticker)
        if bars is None or bars.empty:
            continue
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
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
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

    work_dir = WORK_DIR / today
    work_dir.mkdir(parents=True, exist_ok=True)
    with open(work_dir / "data.json", "w") as f:
        json.dump(data, f, indent=2, default=str)

    _build_charts(data, work_dir)

    return data


def _build_charts(data: dict, work_dir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    moves = [m for m in data["watchlist_moves"] if m.get("pct_change") is not None]
    if moves:
        fig, ax = plt.subplots(figsize=(8, max(3, len(moves) * 0.35)))
        tickers = [m["ticker"] for m in moves]
        pcts = [m["pct_change"] for m in moves]
        colors = ["#2e7d32" if p >= 0 else "#c62828" for p in pcts]
        ax.barh(tickers, pcts, color=colors)
        ax.axvline(0, color="black", linewidth=0.8)
        ax.set_xlabel("% change (last price vs prior close)")
        ax.set_title("Watchlist daily movement")
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


def build(narrative_path: str) -> Path:
    today = _today_str()
    work_dir = WORK_DIR / today
    with open(work_dir / "data.json") as f:
        data = json.load(f)
    with open(narrative_path) as f:
        narrative = json.load(f)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"daily_review_{today}.pdf"
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
        print("Usage: python daily_review.py gather | build --narrative <path>")
        sys.exit(1)

    if sys.argv[1] == "gather":
        result = gather()
        print(json.dumps(result, indent=2, default=str))
    else:
        if "--narrative" not in sys.argv:
            print("build requires --narrative <path-to-json>")
            sys.exit(1)
        narrative_path = sys.argv[sys.argv.index("--narrative") + 1]
        out = build(narrative_path)
        print(f"Report saved to {out}")
