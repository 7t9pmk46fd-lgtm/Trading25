"""
Trading Desk MCP server: exposes the desk's own capabilities (market
research, strategy screeners, risk manager, portfolio tracking, historical
data analysis, and trade/backtest simulation) as MCP tools, so any
MCP-capable agent (Claude Code, Claude Desktop, etc.) can use them
directly.

HARD BOUNDARY, same as the rest of this repo: this server can NEVER place,
modify, or cancel an order, and it never writes to the `signals` table.
Screener tools here are analysis-only -- they return what a strategy sees
without queueing anything. The only path to a real trade remains
execution/run_execution_loop.py --live. The only table this server writes
is `research_notes` (via the market-research tool), which the execution
loop never reads.

Run (stdio, spawned by the MCP client per .mcp.json):
    venv/Scripts/python mcp_server.py
"""
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server import MCPServer

mcp = MCPServer(
    "trading-desk",
    instructions=(
        "Personal trading desk tools: portfolio tracking, strategy screeners, "
        "risk simulation, historical analysis, backtesting, and research "
        "ingestion. Analysis and simulation only -- this server cannot place, "
        "modify, or cancel orders, and screener tools never queue signals."
    ),
)


# ---------------------------------------------------------------- portfolio

@mcp.tool()
def portfolio_status(account: str = "default") -> dict:
    """Live portfolio tracker: equity, today's P&L, buying power, PDT
    status, open positions, standing stop orders, and performance vs the
    S&P 500 benchmark. account: 'default' (swing/mean-reversion) or
    'sneaky_pivot' (intraday). Read-only."""
    from execution import alpaca_client
    from shared import db
    from shared.benchmark import compute_benchmark_comparison
    from shared.market_data import get_latest_trade_prices

    snapshot = alpaca_client.get_account_snapshot(account=account)
    positions = alpaca_client.get_open_positions(account=account)
    stops = alpaca_client.get_open_stop_orders(account=account)

    benchmark = None
    try:
        spy_price = get_latest_trade_prices(["SPY"])["SPY"]
        with db.db_session() as conn:
            cmp = compute_benchmark_comparison(conn, account, snapshot.equity, spy_price)
        if cmp is not None:
            benchmark = asdict(cmp)
    except Exception as e:
        benchmark = {"error": f"benchmark unavailable: {e}"}

    return {
        "account": account,
        "equity": snapshot.equity,
        "today_pnl": snapshot.today_pnl,
        "buying_power": snapshot.buying_power,
        "is_pdt_flagged": snapshot.is_pdt_flagged,
        "daytrade_count": snapshot.daytrade_count,
        "open_positions": positions,
        "open_stop_orders": stops,
        "benchmark_vs_spy": benchmark,
    }


@mcp.tool()
def recent_activity(days: int = 3) -> dict:
    """Recent desk activity from the local ledger: signals generated and
    orders placed in the last N days, with statuses. Read-only."""
    from shared import db

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db.db_session() as conn:
        signals = [dict(r) for r in conn.execute(
            "SELECT * FROM signals WHERE date(created_at) >= ? ORDER BY created_at DESC", (cutoff,)
        ).fetchall()]
        orders = [dict(r) for r in conn.execute(
            """SELECT o.*, s.strategy FROM orders o LEFT JOIN signals s ON o.signal_id = s.id
               WHERE date(o.submitted_at) >= ? ORDER BY o.submitted_at DESC""", (cutoff,)
        ).fetchall()]
    return {"since": cutoff, "signals": signals, "orders": orders}


# ------------------------------------------------------------------- market

@mcp.tool()
def historical_analysis(symbol: str, days: int = 90) -> dict:
    """Historical data analysis for one symbol over the last N calendar
    days: total return, annualized volatility, ATR(14), current z-score
    (20-day), range, and average dollar volume. Read-only."""
    import numpy as np

    from shared.market_data import get_daily_bars
    from shared.risk import compute_atr
    from signals.screeners.mean_reversion import compute_zscore

    symbol = symbol.upper()
    end = datetime.now()
    bars_by_symbol = get_daily_bars([symbol], end - timedelta(days=days), end)
    if symbol not in bars_by_symbol or bars_by_symbol[symbol].empty:
        return {"symbol": symbol, "error": "no data returned"}

    bars = bars_by_symbol[symbol]
    close = bars["close"]
    daily_returns = close.pct_change().dropna()
    zscore = compute_zscore(close, 20).iloc[-1]
    atr = compute_atr(bars)

    return {
        "symbol": symbol,
        "bars": len(bars),
        "first_date": str(bars.index[0].date()),
        "last_date": str(bars.index[-1].date()),
        "last_close": float(close.iloc[-1]),
        "total_return_pct": float((close.iloc[-1] / close.iloc[0] - 1) * 100),
        "annualized_volatility_pct": float(daily_returns.std() * np.sqrt(252) * 100),
        "atr_14": None if np.isnan(atr) else float(atr),
        "zscore_20d": None if np.isnan(zscore) else float(zscore),
        "period_high": float(bars["high"].max()),
        "period_low": float(bars["low"].min()),
        "avg_dollar_volume": float((close * bars["volume"]).mean()),
    }


# ---------------------------------------------------------------- screeners

@mcp.tool()
def run_screener(strategy: str = "mean_reversion", symbols: list[str] | None = None) -> dict:
    """Run a strategy screener in ANALYSIS-ONLY mode: returns what the
    strategy currently sees per symbol (signal or not, z-score/levels)
    WITHOUT queueing anything to the signals table and without any risk of
    a trade. strategy: 'mean_reversion' (daily swing) or 'sneaky_pivot'
    (intraday levels). symbols defaults to the configured watchlist."""
    import pandas as pd

    from shared.config import TRADE_UNIVERSE
    from shared.market_data import get_daily_bars
    from shared.risk import compute_atr

    # TRADE_UNIVERSE, not WATCHLIST: SPY/QQQ are monitored but never traded,
    # so screening them would report signals the live system won't act on.
    tickers = [t.upper() for t in (symbols or TRADE_UNIVERSE)]
    end = datetime.now()

    if strategy == "mean_reversion":
        from signals.screeners.mean_reversion import MeanReversionParams, generate_signals, latest_signal

        all_bars = get_daily_bars(tickers, end - timedelta(days=90), end)
        results = []
        for t in tickers:
            if t not in all_bars or all_bars[t].empty:
                results.append({"ticker": t, "status": "no_data"})
                continue
            annotated = generate_signals(all_bars[t], MeanReversionParams())
            sig = latest_signal(annotated)
            z = annotated["zscore"].iloc[-1]
            results.append({
                "ticker": t,
                "status": "signal" if sig else "no_signal",
                "signal": sig["signal"] if sig else None,
                "zscore": None if pd.isna(z) else float(z),
                "close": float(annotated["close"].iloc[-1]),
            })
        return {"strategy": "mean_reversion", "results": results}

    if strategy == "sneaky_pivot":
        from signals.screeners.sneaky_pivot import SneakyPivotParams, compute_levels

        daily = get_daily_bars(tickers, end - timedelta(days=70), end)
        today = datetime.now().date()
        results = []
        for t in tickers:
            bars = daily.get(t)
            if bars is None or bars.empty:
                results.append({"ticker": t, "status": "no_data"})
                continue
            prior = bars[bars.index.date < today]
            levels = compute_levels(prior, SneakyPivotParams())
            atr = compute_atr(prior)
            results.append({
                "ticker": t,
                "status": "levels_computed",
                "levels": levels,
                "atr_14": None if pd.isna(atr) else float(atr),
                "note": "Entry/exit evaluation needs live intraday bars during market hours; these are today's watch levels.",
            })
        return {"strategy": "sneaky_pivot", "results": results}

    return {"error": f"unknown strategy {strategy!r}; use 'mean_reversion' or 'sneaky_pivot'"}


# --------------------------------------------------------------------- risk

@mcp.tool()
def risk_check(symbol: str, entry_price: float, stop_price: float | None = None,
               account: str = "default") -> dict:
    """Risk manager: for a hypothetical entry, returns the position size
    the desk's sizing rules would allow (1% risk per trade, 10% equity
    cap), the stop distance (2xATR default if stop_price omitted), and
    whether the PDT rule and daily circuit breaker would currently block a
    new entry on this account. Pure simulation -- nothing is queued or
    placed."""
    from execution import alpaca_client
    from shared import db, risk
    from shared.market_data import get_daily_bars

    symbol = symbol.upper()
    snapshot = alpaca_client.get_account_snapshot(account=account)
    is_pdt_account = snapshot.equity >= 25_000
    limits = risk.RiskLimits(account_equity=snapshot.equity, is_pdt_account=is_pdt_account)

    stop_source = "caller"
    if stop_price is None:
        end = datetime.now()
        bars = get_daily_bars([symbol], end - timedelta(days=90), end).get(symbol)
        if bars is None or bars.empty:
            return {"symbol": symbol, "error": "no bars available to compute an ATR-based stop; pass stop_price explicitly"}
        atr = risk.compute_atr(bars)
        import math
        if math.isnan(atr) or atr <= 0:
            return {"symbol": symbol, "error": "ATR unavailable; pass stop_price explicitly"}
        stop_price = entry_price - 2 * atr
        stop_source = "2xATR(14) default"

    sizing = risk.compute_position_size(limits, entry_price, stop_price)

    gates = {}
    with db.db_session() as conn:
        try:
            risk.check_pdt_allows_trade(conn, limits)
            gates["pdt"] = "clear"
        except risk.RiskViolation as e:
            gates["pdt"] = f"BLOCKED: {e}"
        try:
            risk.check_circuit_breaker(conn, limits, snapshot.today_pnl)
            gates["circuit_breaker"] = "clear"
        except risk.RiskViolation as e:
            gates["circuit_breaker"] = f"BLOCKED: {e}"
    if not is_pdt_account and snapshot.daytrade_count >= 3:
        gates["alpaca_daytrade_count"] = f"BLOCKED: Alpaca reports {snapshot.daytrade_count} day trades"
    else:
        gates["alpaca_daytrade_count"] = f"clear ({snapshot.daytrade_count} used)"

    return {
        "symbol": symbol,
        "account": account,
        "account_equity": snapshot.equity,
        "entry_price": entry_price,
        "stop_price": round(stop_price, 2),
        "stop_source": stop_source,
        "sizing": sizing,
        "risk_gates": gates,
        "note": "Simulation only. This server cannot place orders.",
    }


# --------------------------------------------------------------- simulation

@mcp.tool()
def backtest(strategy: str = "mean_reversion", symbols: list[str] | None = None,
             lookback_days: int = 365, entry_z: float = -1.5, exit_z: float = 0.0) -> dict:
    """Simulate trading conditions: backtest a strategy over real
    historical bars using the same production screener code the live desk
    runs. strategy: 'mean_reversion' (entry_z/exit_z apply) or
    'sneaky_pivot' (bar-by-bar intraday replay, params fixed to production
    defaults, lookback capped ~90d by data availability). Returns total
    return, Sharpe, max drawdown, trade count, win rate, and SPY
    buy-and-hold over the same window. Results are NOT logged to
    backtest_runs -- use the scripts in signals/backtest/ for runs you
    want recorded."""
    import numpy as np
    import pandas as pd

    from shared.config import TRADE_UNIVERSE
    from shared.market_data import get_daily_bars

    tickers = [t.upper() for t in (symbols or TRADE_UNIVERSE)]
    end = datetime.now()
    start = end - timedelta(days=lookback_days)

    if strategy == "mean_reversion":
        from signals.backtest.backtest_mean_reversion import compute_metrics, simulate_ticker
        from signals.screeners.mean_reversion import MeanReversionParams

        params = MeanReversionParams(entry_z=entry_z, exit_z=exit_z)
        bars = get_daily_bars(tickers + ["SPY"], start, end)
        per_ticker_daily, all_trades = {}, []
        for t in tickers:
            df = bars.get(t)
            if df is None or len(df) < params.lookback + 5:
                continue
            daily_r, trade_r, _ = simulate_ticker(df, params)
            per_ticker_daily[t] = daily_r
            all_trades.extend(trade_r)
        if not per_ticker_daily:
            return {"error": "no usable data for any requested symbol"}
        portfolio_daily = pd.concat(per_ticker_daily, axis=1).mean(axis=1)
        metrics = compute_metrics(portfolio_daily, all_trades)

    elif strategy == "sneaky_pivot":
        from signals.backtest.backtest_sneaky_pivot import compute_metrics, simulate_ticker
        from shared.market_data import get_intraday_bars

        intraday_start = max(start, end - timedelta(days=90))  # IEX intraday history limit
        daily = get_daily_bars(tickers + ["SPY"], intraday_start - timedelta(days=90), end)
        intraday = get_intraday_bars(tickers, intraday_start, end, minutes=15)
        per_ticker_daily, all_trades = {}, []
        for t in tickers:
            if daily.get(t) is None or intraday.get(t) is None or intraday[t].empty:
                continue
            r = simulate_ticker(t, daily[t], intraday[t])
            if not r["daily_returns"].empty:
                per_ticker_daily[t] = r["daily_returns"]
            all_trades.extend(r["trade_returns"])
        if not per_ticker_daily:
            return {"error": "no usable intraday data for any requested symbol"}
        portfolio_daily = pd.concat(per_ticker_daily, axis=1).fillna(0.0).mean(axis=1)
        metrics = compute_metrics(portfolio_daily, all_trades)
        bars = daily
        start = intraday_start

    else:
        return {"error": f"unknown strategy {strategy!r}"}

    spy_return = None
    spy = bars.get("SPY")
    if spy is not None and not spy.empty:
        window = spy[spy.index.date >= start.date()]
        if len(window) > 1:
            spy_return = float(window["close"].iloc[-1] / window["close"].iloc[0] - 1)

    return {
        "strategy": strategy,
        "symbols": sorted(per_ticker_daily.keys()),
        "window": {"start": str(start.date()), "end": str(end.date())},
        "metrics": metrics,
        "spy_buy_and_hold_return": spy_return,
        "beats_spy": None if spy_return is None else metrics["total_return"] > spy_return,
    }


# ------------------------------------------------------------------ research

@mcp.tool()
def market_research(url: str) -> dict:
    """Ingest a news article or YouTube video into the research pipeline:
    fetches the content, extracts a structured claim summary via Claude,
    and stores it as a research note for human review. Research notes are
    quarantined from trading -- they can never generate a trade without a
    human coding and backtesting the idea first. Requires
    ANTHROPIC_API_KEY in .env for the extraction step."""
    from analyst.ingest_source import ingest_source

    note_id = ingest_source(url)
    return {"stored_research_note_id": note_id,
            "note": "Review with: venv/Scripts/python analyst/review_notes.py"}


@mcp.tool()
def list_research_notes(status: str = "unreviewed", limit: int = 20) -> list[dict]:
    """List stored research notes. status: 'unreviewed',
    'reviewed_promising', 'reviewed_discarded', 'promoted_to_strategy',
    or 'all'."""
    from shared import db

    query = "SELECT * FROM research_notes"
    params: tuple = ()
    if status != "all":
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY created_at DESC LIMIT ?"
    with db.db_session() as conn:
        return [dict(r) for r in conn.execute(query, params + (limit,)).fetchall()]


if __name__ == "__main__":
    mcp.run()
