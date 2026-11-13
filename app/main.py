"""Async entrypoint: ingestion -> detection -> paper trading -> dashboard.

`python -m app.main`                 run the paper-trading engine
`python -m app.main export-tax FILE` export the AEAT tax CSV (live fills only)

Orchestration (paper trading default):
  - funding snapshots are recorded to DuckDB; a carry is only approved on
    pessimistic trailing statistics (min(polled rate, p25 of history)),
    never on a spot-rate linear projection
  - approved opportunities auto-open a paper position (latency simulation
    + post-delay revalidation inside PaperTrader)
  - open positions are monitored every cycle via engines/monitor.py and
    rebuilt from SQLite on restart
  - events also go to engine.log (breaker halts, feed errors, opens/closes)
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from rich.console import Console
from rich.live import Live

from app.connectors.ccxt_manager import (
    BOOKS_TOPIC,
    FUNDING_TOPIC,
    BookFeed,
    FundingPoller,
    fetch_minimal_markets,
    fetch_unified_account_health,
)
from app.connectors.normalizer import FundingRate, L2OrderBook
from app.core.circuit_breaker import CircuitBreaker
from app.core.config import get_settings
from app.core.feed_guards import remote_book_stale, sequence_regressed
from app.core.decimals import D, ZERO, mul
from app.core.events import EventBus
from app.database.duckdb_store import TickStore
from app.database.repository import Repository
from app.engines.candidates import CarryContext, OpenCarry, handle_funding_candidate
from app.engines.perp_perp import (
    PerpPerpFill,
    PerpPerpOpportunity,
    PerpPerpTrader,
    PpContext,
    PpPosition,
    pp_handle_funding,
    pp_manage,
)
from app.engines.arbitrage import FundingOpportunity
from app.engines.fee_oracle import (
    FeeOracle,
    fetch_kraken_spot_fee,
    fetch_krakenfutures_fee,
    fetch_okx_fee,
)
from app.engines.liquidity import InsufficientDepth
from app.engines.monitor import books_fresh, MonitorAction, evaluate_carry
from app.engines.paper_trader import PaperFill, PaperTrader
from app.engines.risk import CarryPositionRisk, StalenessGate, UtaAccountState
from app.engines import screener as scr
from app.connectors import screener_io
from app.engines.validator import uta_allocation
from app.ui.cli_dashboard import render
from app.ui.notifier import TelegramNotifier

logger = logging.getLogger("main")

MAX_OPPORTUNITIES_KEPT = 200

FUNDING_LOG_CSV = "funding_log.csv"


def seed_funding_history(ticks):
    # Seed DuckDB funding history from the CSV log (survives restarts).
    import csv

    def to_utc(ts: str) -> datetime:
        """op.#16: compare INSTANTS, not strings. Stored ts is UTC-naive
        (DuckDB TIMESTAMP has no tz); CSV rows carry +00:00."""
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    watermarks = {
        k: to_utc(str(v)) for k, v in ticks.funding_max_ts_by_series().items() if v
    }
    seeded = failed = 0
    keep: list[list[str]] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    try:
        fh = open(FUNDING_LOG_CSV, newline="", encoding="utf-8")
    except FileNotFoundError:
        return 0
    with fh:  # op.#16: stream the CSV, never load it whole into RAM
        reader = csv.reader(fh)
        next(reader, None)  # header
        for row in reader:
            if len(row) < 5:
                continue
            exchange, symbol, ts_iso, rate, predicted = row[:5]
            try:
                ts = to_utc(ts_iso)
            except ValueError:
                failed += 1
                continue
            if ts >= cutoff:
                keep.append(row[:5])
            if ts <= watermarks.get((exchange, symbol), epoch):
                continue  # already in DuckDB (per-series watermark)
            if ticks.insert_funding_raw(exchange, symbol, ts_iso, rate, predicted or None):
                seeded += 1
            else:
                failed += 1
    if failed:
        logger.warning("funding seed: %d rows failed to insert", failed)
    # caos: the CSV grew unbounded (~300 KB/day). DuckDB is the durable
    # store; the CSV only needs recent rows as a restart seed. Rewrite
    # atomically keeping the last 7 days.
    # F43: compaction is housekeeping, NOT boot-critical. A read-only dir or
    # a full disk must degrade to a warning, never kill the engine at boot
    # (the seed rows are already in DuckDB by this point).
    import os
    import tempfile

    try:
        fd, tmp = tempfile.mkstemp(
            dir=str(Path(FUNDING_LOG_CSV).parent or "."), suffix=".csv.tmp")
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as out:
                w = csv.writer(out)
                w.writerow(
                    ["exchange", "symbol", "ts", "rate", "predicted_rate"])
                w.writerows(keep)
            os.replace(tmp, FUNDING_LOG_CSV)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning("funding_log compaction skipped (engine continues): %s",
                       exc)
    return seeded


def append_funding_log(rate):
    import csv
    import os

    new = not os.path.exists(FUNDING_LOG_CSV)
    with open(FUNDING_LOG_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["exchange", "symbol", "ts", "rate", "predicted_rate"])
        w.writerow([
            rate.exchange, rate.symbol,
            datetime.now(timezone.utc).isoformat(),
            str(rate.rate),
            "" if rate.predicted_rate is None else str(rate.predicted_rate),
        ])





async def run() -> None:
    from logging.handlers import RotatingFileHandler

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        # op.#18: bounded log growth (3 x 5MB), no silent disk fill
        handlers=[
            # stdout para `docker logs` en el servidor; fichero rotado para
            # historia acotada y para el HEALTHCHECK del contenedor
            logging.StreamHandler(),
            RotatingFileHandler("engine.log", maxBytes=5_000_000, backupCount=3),
        ],
    )
    settings = get_settings()
    console = Console()
    bus = EventBus()
    Path(settings.duckdb_path).parent.mkdir(parents=True, exist_ok=True)
    repo = Repository(settings.sqlite_url)
    await repo.init()
    ticks = TickStore(settings.duckdb_path)
    seeded = seed_funding_history(ticks)
    if seeded:
        logger.info("seeded %d funding samples from %s", seeded, FUNDING_LOG_CSV)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    breaker = CircuitBreaker(
        max_consecutive_failures=settings.cb_max_consecutive_failures,
        max_daily_loss_usd=settings.max_daily_loss_usd,
        max_orders_per_window=settings.cb_max_orders_per_window,
        window_seconds=settings.cb_order_window_seconds,
        state_path="breaker_state.json",
    )
    # op.#8: rebuild today's realized PnL from the ledger (RAM starts at 0,
    # the ledger does not)
    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    breaker.seed_daily_pnl(await repo.realized_pnl_since(day_start))
    # op.#17: a breaker halt must reach Telegram, not only the log
    breaker.on_halt = lambda reason: asyncio.create_task(
        notifier.send(f"CIRCUIT BREAKER HALT: {reason}")
    )
    gate = StalenessGate(settings.max_data_age_ms_funding)
    paper = PaperTrader(
        repo,
        latency_ms=settings.dry_run_latency_ms,
        spot_taker_fee_bps=settings.taker_fee_for(settings.spot_exchange_list[0]),
        # both supported perp venues are 5bps taker (krakenfutures/okx tier base)
        perp_taker_fee_bps=settings.taker_fee_for(settings.perp_exchange_list[0]),
        execution_mode=settings.execution_mode,
        spot_maker_fee_bps=settings.maker_fee_for(settings.spot_exchange_list[0]),
        perp_maker_fee_bps=settings.maker_fee_for(settings.perp_exchange_list[0]),
        maker_timeout_seconds=settings.maker_timeout_seconds,
        maker_fallback=settings.maker_fallback,
        min_net_profit_usd=settings.min_net_profit_usd,
        slippage_buffer_bps=settings.slippage_buffer_bps,
        liquidity_haircut=settings.liquidity_haircut,
        settings=settings,
    )

    stop = asyncio.Event()
    spot_symbols = settings.spot_symbol_list
    tasks = []
    # One minimal market table per venue, shared across that venue's feeds
    # (full load_markets per instance OOMs a 2GB sandbox).
    venues = list(dict.fromkeys(
        list(settings.spot_exchange_list) + list(settings.perp_exchange_list)
    ))
    venue_markets = {}
    for v in venues:
        try:
            venue_markets[v] = await fetch_minimal_markets(v)
        except Exception as exc:
            logger.warning("minimal markets %s failed: %s", v, exc)
            venue_markets[v] = None
    spot_ex = settings.spot_exchange_list[0]

    # Fee oracle: real fees detected from each venue's API at startup and
    # every fee_refresh_hours. Config values are only the last-resort fallback.
    fee_oracle = FeeOracle(settings)

    def _fee_fetchers() -> dict:
        fetchers: dict = {}
        if "krakenfutures" in settings.perp_exchange_list:
            kf_sym = settings.perp_symbols_for("krakenfutures")[0]
            kf_base = kf_sym.split("/")[0]
            raw = f"PF_{'XBT' if kf_base == 'BTC' else kf_base}USD"
            fetchers["krakenfutures"] = lambda: fetch_krakenfutures_fee({kf_sym: raw})
        if "okx" in settings.perp_exchange_list and settings.okx_api_key:
            fetchers["okx"] = lambda: fetch_okx_fee(
                settings.okx_api_key, settings.okx_api_secret,
                settings.okx_api_passphrase, settings.okx_hostname,
            )
        if settings.kraken_spot_api_key:
            fetchers["kraken"] = lambda: fetch_kraken_spot_fee(
                settings.kraken_spot_api_key, settings.kraken_spot_api_secret,
            )
        else:
            logger.warning(
                "kraken spot fees NOT auto-detectable (falta kraken_spot_api_key "
                "read-only): usando fallback config %s bps",
                settings.taker_fee_bps_map.get("kraken"),
            )
        return fetchers

    await fee_oracle.refresh(_fee_fetchers())

    async def fee_refresh_loop() -> None:
        while not stop.is_set():
            await asyncio.sleep(float(settings.fee_refresh_hours) * 3600)
            try:
                await fee_oracle.refresh(_fee_fetchers())
            except Exception as exc:
                logger.warning("fee refresh failed: %s", exc)

    # Feeds are tracked per symbol so the screener can subscribe/unsubscribe
    # without restarting the engine. Each feed gets its own stop event.
    feed_tasks: dict[str, tuple[asyncio.Task, asyncio.Event, object]] = {}
    feed_restart_count: dict[str, int] = {}

    def start_feed(name: str, factory) -> None:
        ev = asyncio.Event()
        t = asyncio.create_task(factory(ev))
        tasks.append(t)
        feed_tasks[name] = (t, ev, factory)

    def stop_feed(name: str) -> None:
        t, ev, _ = feed_tasks.pop(name)
        ev.set()
        t.cancel()
        if t in tasks:
            tasks.remove(t)

    async def feed_watchdog() -> None:
        """op.#13: supervise feed tasks (restart with backoff + alert) and
        watch data freshness per feed (a hung feed that never raises is
        detected by the absence of fresh books, not by its exception)."""
        outage_alerted: set[str] = set()
        giveup_alerted_ns: dict[str, int] = {}
        while not stop.is_set():
            await asyncio.sleep(30)
            for name, (t, ev, factory) in list(feed_tasks.items()):
                if not t.done():
                    continue
                exc = t.exception() if not t.cancelled() else None
                n = feed_restart_count.get(name, 0) + 1
                feed_restart_count[name] = n
                backoff = min(60, 2 ** n)
                logger.error(
                    "feed %s died (%s); restart #%d in %ss", name, exc, n, backoff
                )
                # caos H7: a permanently-failing feed retried forever used to
                # spam Telegram on EVERY restart (~1/min). Alert the 1st
                # death and then once every 10 restarts; recovery alerts
                # separately below.
                if n == 1 or n % 10 == 0:
                    await notifier.send(f"FEED DIED {name}: {exc} - restart #{n}")
                feed_tasks.pop(name, None)
                if n >= 20:
                    # caos: a feed failing 20 consecutive restarts has a
                    # permanent problem (bad symbol, config, venue down) -
                    # an infinite restart loop burns CPU and alerts forever.
                    # Give up loudly; the screener re-attempts on its next
                    # sweep if the symbol is still eligible.
                    last = giveup_alerted_ns.get(name, 0)
                    if time.monotonic_ns() - last >= 3600 * 10**9:
                        giveup_alerted_ns[name] = time.monotonic_ns()
                        await notifier.send(
                            f"FEED GIVEN UP {name} tras 20 reinicios: {exc}"
                        )
                    feed_restart_count[name] = 0
                    continue
                await asyncio.sleep(backoff)
                if name not in feed_tasks:  # the screener may have cut it
                    start_feed(name, factory)
            now_ns = time.monotonic_ns()
            for name in list(feed_tasks):
                if not name.startswith("book:"):
                    continue
                key = name[len("book:"):]
                book = books.get(key)
                stale = book is None or (now_ns - book.local_ts_ns) / 1e9 > 60
                if stale and key not in outage_alerted:
                    outage_alerted.add(key)
                    logger.error("feed OUTAGE %s: no fresh book in 60s", key)
                    await notifier.send(f"FEED OUTAGE {key}: sin libro fresco en 60s")
                elif not stale and key in outage_alerted:
                    outage_alerted.discard(key)
                    logger.info("feed RECOVERED %s", key)
                    await notifier.send(f"FEED RECOVERED {key}")

    spot_venues = set(settings.spot_exchange_list)

    def start_book_feed(ex: str, sym: str) -> None:
        start_feed(
            f"book:{ex}:{sym}",
            lambda ev: BookFeed(ex, [sym], markets_list=venue_markets.get(ex),
                                derivative=ex not in spot_venues).run(bus, ev),
        )

    def start_funding_feed(ex: str, sym: str) -> None:
        start_feed(
            f"fund:{ex}:{sym}",
            lambda ev: FundingPoller(ex, [sym], markets_list=venue_markets.get(ex)).run(bus, ev),
        )

    books_q = bus.subscribe(BOOKS_TOPIC)
    funding_q = bus.subscribe(FUNDING_TOPIC)

    async def books_consumer() -> None:
        """op.#1: books drain independently of management waits. Before this,
        a 60s maker wait left every position managing itself against book
        snapshots a minute old; now `books` is always the freshest snapshot
        that passed the F01 guards."""
        while not stop.is_set():
            try:
                book = await asyncio.wait_for(books_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            bk = f"{book.exchange}:{book.symbol}"
            # F01: never let a venue serve an old snapshot as new
            if remote_book_stale(book.exchange_ts):
                continue
            prev_book = books.get(bk)
            if prev_book is not None and sequence_regressed(
                prev_book.sequence, prev_book.exchange_ts,
                book.sequence, book.exchange_ts,
            ):
                continue  # F01: replay/duplicate, keep the newer snapshot
            books[bk] = book
            # sample: 1 snapshot/s per symbol is enough for VWAP/slippage
            # analysis - every tick floods the store and the sandbox RAM
            now_mono = time.monotonic()
            if now_mono - last_book_record.get(bk, 0.0) >= 1.0:
                last_book_record[bk] = now_mono
                try:
                    await asyncio.to_thread(ticks.record_book, book)
                except Exception as exc:
                    # F09: a bad record must not kill the consumer
                    logger.warning("book persist dropped %s: %s", bk, exc)
    books: dict[str, L2OrderBook] = {}
    funding: dict[str, FundingRate] = {}
    opportunities: deque[FundingOpportunity] = deque(maxlen=MAX_OPPORTUNITIES_KEPT)
    open_carries: dict[str, OpenCarry] = {}
    realized_pnl = Decimal("0")
    unified = settings.account_margin_mode.strip().upper() == "UNIFIED"
    # multi-asset UTA: survival math uses the WORST configured haircut
    spot_bases = [sym.split("/")[0] for sym in spot_symbols]
    coll_ratio = min(settings.collateral_ratio_for(b) for b in spot_bases)
    uta_state = (
        UtaAccountState(
            equity=D(settings.paper_start_balance_usd),
            utilization=D(settings.uta_capital_utilization_pct),
            collateral_ratio=coll_ratio,
            sim_mmr_rate=D(settings.sim_maintenance_margin_rate),
            leverage=D(settings.short_leverage_for("krakenfutures")),
        )
        if unified
        else None
    )
    # UNIFIED: utilization budget split evenly across spot bases;
    # SPLIT: fixed quote size
    notional = (
        uta_allocation(D(settings.paper_start_balance_usd), D(settings.uta_capital_utilization_pct))[0]
        / max(1, len(spot_bases))
        if unified
        else settings.target_quote_size_list[0]
    )
    max_safe_pump = (
        uta_state.max_safe_pump_pct(D(settings.max_account_mmr_emergency_unwind))
        if uta_state is not None
        else None
    )
    last_mmr: Decimal | None = None
    last_health_check = 0.0
    last_tick_flush = 0.0
    last_book_prune = 0.0
    last_book_record: dict[str, float] = {}

    # --- restart recovery: rebuild open carries from SQLite ---
    for pos in await repo.open_positions():
        if pos.exchange_spot.endswith("(long)"):
            continue  # perp-perp row: recovered into pp_ctx below
        # op.#2 recovery validation: a crash mid-entry (pre-atomic writes)
        # can leave a position with <2 recorded entry legs; never manage it
        if await repo.count_orders(pos.id, "open") != 2:
            logger.error("recovery SKIP pos %s: entry legs incomplete", pos.id)
            await notifier.send(
                f"RECOVERY SKIP pos {pos.id} {pos.symbol}: patas de entrada "
                "incompletas (crash a media entrada) - revision manual"
            )
            continue
        perp_key = f"{pos.exchange_perp}:{pos.symbol}"
        spot_key = f"{pos.exchange_spot}:{pos.spot_symbol or spot_symbols[0]}"
        open_carries[f"{spot_key}|{perp_key}"] = OpenCarry(
            fill=PaperFill(
                position_id=pos.id,
                qty_base=D(pos.qty_base),
                spot_vwap=D(pos.spot_entry_vwap),
                perp_vwap=D(pos.perp_entry_vwap),
            ),
            opp=FundingOpportunity(
                exchange=pos.exchange_perp,
                symbol=pos.symbol,
                notional=D(pos.notional),
                funding_rate=D(pos.entry_funding_rate),
                holding_intervals=pos.holding_intervals,
                breakeven_intervals=0,
                projected_net=D("0"),
            ),
            spot_key=spot_key,
            perp_key=perp_key,
            funding_key=perp_key,
            risk=CarryPositionRisk.build(
                        D(pos.perp_entry_vwap), D(settings.short_leverage_for(pos.exchange_perp))
                    ),
            funding_collected=D(pos.funding_collected),
            last_payment_ts=None,  # unknown: first payment after restart is flagged
            # op.#14: False = "not yet alerted". Recovery used to set True,
            # which silenced the one-time uncertainty warning forever.
            accrual_uncertain=False,
            uta=uta_state,
        )
        logger.info("recovered open position %s (%s)", pos.id, perp_key)

    screener_means: dict = {}
    carry_ctx = CarryContext(
        settings=settings,
        breaker=breaker,
        gate=gate,
        paper=paper,
        ticks=ticks,
        console=console,
        notifier=notifier,
        books=books,
        opportunities=opportunities,
        open_carries=open_carries,
        spot_ex=spot_ex,
        notional=notional,
        uta_state=uta_state,
        max_safe_pump=max_safe_pump,
        screener_means=screener_means,
        # blacklist applies from the very first second (fix: before, a
        # blacklisted key could open trades until the first sweep ran)
        eligible={
            f"{ex}:{sym}"
            for ex in settings.perp_exchange_list
            for sym in settings.perp_symbols_for(ex)
        } - settings.screener_blacklist_keys,
    )

    # --- perp-perp module (decoupled: own trader, own context) ---
    pp_realized = [ZERO]
    pp_ctx: PpContext | None = None
    if settings.perp_perp_enabled:
        def pp_entry_allowed(key: str) -> bool:
            # audit #3: PP shares the entry universe with carry (eligible
            # set + blacklist), it does not pick from raw config alone
            if key in settings.screener_blacklist_keys:
                return False
            if carry_ctx.eligible is not None and key not in carry_ctx.eligible:
                return False
            return True

        def pp_budget() -> Decimal:
            total = (
                D(uta_state.notional)
                if uta_state is not None
                else mul(D(settings.paper_start_balance_usd),
                         D(settings.uta_capital_utilization_pct))
            )
            used = sum(D(c.opp.notional) for c in open_carries.values())
            used += sum(D(pos.opp.notional) for pos in pp_ctx.open.values())
            return max(ZERO, total - used)

        pp_ctx = PpContext(
            settings=settings,
            paper=PerpPerpTrader(
                repo,
                latency_ms=int(settings.dry_run_latency_ms),
                maker_fee_bps=max(
                    D(settings.maker_fee_for(ex)) for ex in settings.perp_exchange_list
                ),
                taker_fee_bps=max(
                    D(settings.taker_fee_for(ex)) for ex in settings.perp_exchange_list
                ),
                execution_mode=settings.execution_mode,
                maker_timeout_seconds=int(settings.maker_timeout_seconds),
                maker_fallback=settings.maker_fallback,
                min_net_profit_usd=D(settings.pp_min_net_profit_usd),
                settings=settings,
            ),
            repo=repo,
            ticks=ticks,
            books=books,
            funding=funding,
            breaker=breaker,
            notifier=notifier,
            open={},
            budget_remaining=pp_budget,
            realized_pnl=pp_realized,
            entry_allowed=pp_entry_allowed,
        )
        carry_ctx.external_allocated = lambda: sum(
            D(pos.opp.notional) for pos in pp_ctx.open.values()
        )
        now_ns = time.monotonic_ns()
        for pos in await repo.open_positions():
            if not pos.exchange_spot.endswith("(long)"):
                continue
            if await repo.count_orders(pos.id, "open") != 2:
                logger.error("recovery SKIP pp pos %s: entry legs incomplete", pos.id)
                await notifier.send(
                    f"RECOVERY SKIP pp pos {pos.id} {pos.symbol}: patas de "
                    "entrada incompletas (crash a media entrada) - revision manual"
                )
                continue
            long_ex = pos.exchange_spot[: -len("(long)")]
            base = pos.symbol.split("/")[0]
            short_key = f"{pos.exchange_perp}:{pos.symbol}"
            pair_key = f"{base}:{long_ex}->{pos.exchange_perp}"
            pp_ctx.open[pair_key] = PpPosition(
                fill=PerpPerpFill(
                    position_id=pos.id, qty_base=D(pos.qty_base),
                    long_vwap=D(pos.spot_entry_vwap), short_vwap=D(pos.perp_entry_vwap),
                ),
                opp=PerpPerpOpportunity(
                    base=base, long_exchange=long_ex,
                    long_symbol=pos.spot_symbol.split(":", 1)[1],
                    short_exchange=pos.exchange_perp, short_symbol=pos.symbol,
                    notional=D(pos.notional),
                    net_rate_per_hour=D(pos.entry_funding_rate),
                    holding_hours=pos.holding_intervals,
                    breakeven_hours=0, projected_net=D("0"),
                ),
                long_key=pos.spot_symbol,
                short_key=short_key,
                funding_collected_net=D(pos.funding_collected),
                # op.#14: the holding clock survives restarts (durable
                # opened_at), it does not restart from zero
                opened_ns=now_ns - min(
                    int((datetime.now(timezone.utc) - pos.opened_at.replace(
                        tzinfo=pos.opened_at.tzinfo or timezone.utc
                    )).total_seconds() * 1e9),
                    now_ns,
                ),
                # watermarks at now: downtime accrues NOTHING (conservative),
                # and the user hears about it instead of it passing silently
                last_accrual_ns_long=now_ns,
                last_accrual_ns_short=now_ns,
            )
            await notifier.send(
                f"PP RECOVERED pos {pos.id} {pair_key}: devengo durante el "
                "downtime NO contabilizado (conservador); holding conservado"
            )
            logger.info("recovered pp position %s (%s)", pos.id, pair_key)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    def reconcile_feeds(eligible: set[str]) -> None:
        """Start/stop feeds so exactly the eligible perps (+ their spot legs
        + anything with an open carry) are subscribed."""
        keys = set(eligible)
        for c in open_carries.values():  # never unsubscribe an open carry
            keys.add(c.perp_key)
            keys.add(c.spot_key)
        if pp_ctx is not None:  # audit #3: never unsubscribe an open PP pair
            for pos in pp_ctx.open.values():
                keys.add(pos.long_key)
                keys.add(pos.short_key)
        for base in scr.eligible_bases(eligible):
            sym = settings.spot_symbol_for_base(base) or f"{base}/USD"
            keys.add(f"{spot_ex}:{sym}")
        books_wanted = set()
        funds_wanted = set()
        for key in keys:
            ex, sym = key.split(":", 1)
            books_wanted.add((ex, sym))
            if ex != spot_ex:
                funds_wanted.add((ex, sym))
        wanted = {f"book:{e}:{s_}" for e, s_ in books_wanted} | {
            f"fund:{e}:{s_}" for e, s_ in funds_wanted
        }
        for name in list(feed_tasks):
            if name not in wanted:
                logger.info("screener: stopping feed %s", name)
                stop_feed(name)
        for ex, sym in sorted(books_wanted):
            if f"book:{ex}:{sym}" not in feed_tasks:
                start_book_feed(ex, sym)
        for ex, sym in sorted(funds_wanted):
            if f"fund:{ex}:{sym}" not in feed_tasks:
                start_funding_feed(ex, sym)

    async def screener_loop() -> None:
        """Periodic re-selection of eligible perps (public data, no keys)."""
        hyst = scr.load_state(settings.screener_state_path)
        # configured pairs are protected from the cut (user decision, e.g.
        # SOL is OKX-only on purpose): snapshot them before they can mutate
        seeded_keys = set(carry_ctx.eligible or set())
        for key in seeded_keys:
            hyst.seed(key)
        await asyncio.sleep(60)  # let the feeds warm up first
        while not stop.is_set():
            try:
                spot_bases = await screener_io.fetch_kraken_spot_bases()
                min_vol = float(settings.screener_min_volume_usd)
                raw = await screener_io.fetch_okx_xperp_metrics(
                    min_volume_usd=min_vol, bases=spot_bases)
                raw += await screener_io.fetch_krakenfutures_metrics(
                    min_volume_usd=min_vol, bases=spot_bases)
                sweep_metrics = [m for m in (scr.from_raw(r) for r in raw) if m is not None]
                screener_means.clear()
                screener_means.update(
                    {m.key: m.funding_mean for m in sweep_metrics if m.funding_mean is not None})
                new_eligible = scr.run_sweep(
                    sweep_metrics,
                    hyst,
                    min_volume_usd=D(settings.screener_min_volume_usd),
                    min_positive_pct=D(settings.screener_min_positive_pct),
                    cost_bps_for=lambda ex: scr.roundtrip_cost_bps(
                        D(settings.taker_fee_for(spot_ex)),
                        D(settings.taker_fee_for(ex)),
                        D(settings.slippage_buffer_bps),
                    ),
                    spot_bases=spot_bases,
                    whitelist_keys=seeded_keys,
                    blacklist_keys=settings.screener_blacklist_keys,
                )
                # fix: an empty result IS a valid sweep outcome ("nothing is
                # eligible") and must revoke eligibility; only an exception
                # (fetch/parse failure) keeps the previous set
                if new_eligible != (carry_ctx.eligible or set()):
                    logger.info("screener eligible: %s", sorted(new_eligible))
                    if carry_ctx.eligible is not None:
                        carry_ctx.eligible.clear()
                        carry_ctx.eligible.update(new_eligible)
                    if uta_state is not None:
                        n = max(1, len(scr.eligible_bases(new_eligible)))
                        carry_ctx.notional = uta_state.notional / n
                    reconcile_feeds(new_eligible)
                scr.save_state(settings.screener_state_path, hyst)
            except Exception as exc:
                logger.warning("screener sweep failed: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.screener_interval_s)
            except asyncio.TimeoutError:
                pass

    reconcile_feeds(carry_ctx.eligible or set())
    if settings.screener_enabled:
        tasks.append(asyncio.create_task(screener_loop()))
    # audit fix: fee freshness must not depend on the screener being on
    tasks.append(asyncio.create_task(fee_refresh_loop()))
    # op.#1: the books consumer runs as its own task (see def above)
    tasks.append(asyncio.create_task(books_consumer()))
    # op.#13: feed supervision + freshness watchdog
    tasks.append(asyncio.create_task(feed_watchdog()))

    async def heartbeat_loop() -> None:
        """Daily proof-of-life: a wedged engine (no exceptions, no alerts)
        is otherwise indistinguishable from a healthy one."""
        await asyncio.sleep(24 * 3600)  # first beat 24h after boot
        while not stop.is_set():
            pp_open = len(pp_ctx.open) if pp_ctx is not None else 0
            await notifier.send(
                "ENGINE OK: "
                f"{len(open_carries)} carries, {pp_open} pares PP, "
                f"{len(feed_tasks)} feeds, "
                f"breaker={'HALT' if breaker.halted else 'ok'}"
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=24 * 3600)
            except asyncio.TimeoutError:
                pass

    tasks.append(asyncio.create_task(heartbeat_loop()))

    async def close_carry(key: str, carry: OpenCarry, reason: str) -> None:
        nonlocal realized_pnl

        def fetch() -> tuple[L2OrderBook, L2OrderBook]:
            return books[carry.spot_key], books[carry.perp_key]

        breaker.record_exit()
        breaker.record_exit()
        try:
            pnl = await paper.close_carry(carry.fill, carry.funding_collected, fetch)
        except InsufficientDepth as exc:
            # transient: keep the position open and retry next cycle
            logger.warning("unwind deferred (depth) %s: %s", key, exc)
            return
        except (KeyError, AssertionError) as exc:
            breaker.record_failure()
            logger.error("unwind failed %s: %s", key, exc)
            await notifier.send(f"UNWIND FAILED {key}: {exc}")
            return
        realized_pnl += pnl
        breaker.record_pnl_usd(pnl)
        del open_carries[key]
        logger.info("unwound %s (%s): pnl %s USD", key, reason, pnl)
        await notifier.send(f"UNWOUND {key} ({reason}): pnl {pnl} USD")

    with Live(console=console, refresh_per_second=2) as live:
        while not stop.is_set():
            now_ns = time.monotonic_ns()

            # caos H4: entries wait up to maker_timeout (60s) each and the
            # carry + PP hooks chain per base - without a budget the manage
            # phase (unwinds, MMR) starves behind entry waits. Cap the
            # drain at 30s/cycle; leftover rates are handled next cycle.
            drain_deadline = time.monotonic() + 30
            while not funding_q.empty() and time.monotonic() < drain_deadline:
                rate = funding_q.get_nowait()
                fkey = f"{rate.exchange}:{rate.symbol}"
                funding[fkey] = rate
                try:
                    await asyncio.to_thread(ticks.record_funding, rate)
                except Exception as exc:
                    # F09: a bad record must not kill the management loop
                    logger.warning("funding persist dropped %s: %s", fkey, exc)
                try:
                    append_funding_log(rate)
                except Exception as exc:
                    logger.warning("funding csv append failed: %s", exc)
                try:
                    await handle_funding_candidate(carry_ctx, rate, now_ns)
                except Exception as exc:
                    # op.#11: a candidate failure must not kill both modules
                    logger.exception("carry candidate failed: %s", exc)
                    await notifier.send(f"CANDIDATE ERROR: {type(exc).__name__}: {exc}")
                    breaker.record_failure()
                if pp_ctx is not None:
                    try:
                        await pp_handle_funding(pp_ctx, rate)
                    except Exception as exc:  # decoupled: never kills the carry loop
                        logger.exception("perp-perp funding hook failed: %s", exc)
                        await notifier.send(f"PP MODULE ERROR (funding hook): {exc}")
            if not funding_q.empty():
                logger.warning(
                    "funding queue drain deferred (%d pending) - manage phase first",
                    funding_q.qsize(),
                )

            if time.monotonic() - last_tick_flush > 30:
                last_tick_flush = time.monotonic()
                await asyncio.to_thread(ticks.flush)
            # caos H9: book snapshots retention (funding history is kept)
            if time.monotonic() - last_book_prune > 3600:
                last_book_prune = time.monotonic()
                try:
                    pruned = await asyncio.to_thread(
                        ticks.prune_books, int(settings.book_retention_days)
                    )
                    if pruned:
                        logger.info("book retention: %d snapshots pruned", pruned)
                except Exception as exc:
                    logger.warning("book prune failed: %s", exc)
                try:  # soak durability: hourly consistent funding export
                    n = await asyncio.to_thread(
                        ticks.export_funding_csv,
                        str(Path(settings.duckdb_path).with_name(
                            "funding_history_export.csv")),
                    )
                    logger.info("funding export: %d rows", n)
                except Exception as exc:
                    logger.warning("funding export failed: %s", exc)

            # real account health (read-only keys); simulated UTA is the fallback
            health_venues = []
            if settings.krakenfutures_api_key:
                health_venues.append(
                    ("krakenfutures", settings.krakenfutures_api_key,
                     settings.krakenfutures_api_secret, "", "")
                )
            if settings.okx_api_key:
                health_venues.append(
                    ("okx", settings.okx_api_key, settings.okx_api_secret,
                     settings.okx_api_passphrase, settings.okx_hostname)
                )
            if health_venues and time.monotonic() - last_health_check > 60:
                last_health_check = time.monotonic()
                for hv, hk, hs, hp, hh in health_venues:
                    try:
                        health = await fetch_unified_account_health(hv, hk, hs, hp, hh)
                    except Exception as exc:
                        health = None
                        logger.warning("account health fetch %s failed: %s", hv, exc)
                    if health is not None:
                        logger.info(
                            "real account health %s: equity %s mmr %s",
                            health.exchange, health.total_equity, health.account_mmr,
                        )
                        if health.account_mmr >= D(settings.max_account_mmr_emergency_unwind):
                            logger.warning("REAL account MMR %s >= emergency", health.account_mmr)
                            await notifier.send(f"REAL MMR EMERGENCY: {health.account_mmr}")
                            # op.#17: a real MMR emergency stops new entries,
                            # it does not only warn (paper: no real unwind)
                            breaker.halt(
                                f"real account MMR emergency: {health.account_mmr}"
                            )
            max_book_age_ns = int(settings.max_book_age_ms) * 1_000_000
            for key, carry in list(open_carries.items()):
                # op.#4: a venue down for hours must not drive decisions with
                # its last-known prices. Defer (funding accrual resumes on
                # recovery, it is watermark-based) and alert once per episode.
                if not books_fresh(
                    now_ns, max_book_age_ns,
                    books.get(carry.spot_key), books.get(carry.perp_key),
                ):
                    if not carry.books_stale_warned:
                        carry.books_stale_warned = True
                        logger.warning("carry %s: stale books, management deferred", key)
                        await notifier.send(
                            f"STALE VENUE {key}: gestion diferida (libro viejo)"
                        )
                    continue
                if carry.books_stale_warned:
                    carry.books_stale_warned = False
                    await notifier.send(f"VENUE RECOVERED {key}: gestion reanudada")
                cur_rate = funding.get(carry.funding_key)
                decision = evaluate_carry(
                    qty_base=carry.fill.qty_base,
                    perp_entry_vwap=carry.fill.perp_vwap,
                    last_payment_ts=carry.last_payment_ts,
                    rate=cur_rate,
                    prev_rate=carry.last_rate,
                    perp_book=books.get(carry.perp_key),
                    risk=carry.risk,
                    liquidation_buffer_pct=settings.liquidation_buffer_pct,
                    uta=carry.uta,
                    mmr_emergency=D(settings.max_account_mmr_emergency_unwind),
                )
                if decision.account_mmr is not None:
                    last_mmr = decision.account_mmr
                    if (
                        decision.account_mmr >= D(settings.max_account_mmr_warning)
                        and not carry.mmr_warned
                    ):
                        carry.mmr_warned = True
                        logger.warning("account MMR warning %s: %s", key, decision.account_mmr)
                        await notifier.send(f"MMR WARNING {key}: {decision.account_mmr}")
                if decision.accrual_uncertain and not carry.accrual_uncertain:
                    carry.accrual_uncertain = True
                    logger.warning("funding accrual uncertain for %s (missed payments)", key)
                    await notifier.send(f"ACCRUAL UNCERTAIN {key}: missed funding payments")
                carry.last_payment_ts = decision.new_payment_ts
                if cur_rate is not None:
                    carry.last_rate = cur_rate
                try:
                    if decision.accrual != ZERO:  # F04: costs count too
                        carry.funding_collected += decision.accrual
                        await repo.add_funding(carry.fill.position_id, decision.accrual)
                    if decision.action is MonitorAction.UNWIND:
                        await close_carry(key, carry, decision.reason)
                except Exception as exc:
                    # op.#11: one carry's failure must not kill the module
                    logger.exception("carry manage failed %s: %s", key, exc)
                    await notifier.send(f"CARRY MANAGE ERROR {key}: {type(exc).__name__}")
                    breaker.record_failure()
            # op.#8: daily loss limit sees open exposure too (MTM at
            # top-of-book exit prices, fees ignored = optimistic, so the
            # trip is if anything late, never early)
            mtm = ZERO
            for carry in open_carries.values():
                sb = books.get(carry.spot_key)
                pb = books.get(carry.perp_key)
                if sb is None or pb is None:
                    continue
                qty = D(carry.fill.qty_base)
                mtm += D(carry.funding_collected)
                mtm += mul(qty, sb.best_bid.price) - mul(qty, carry.fill.spot_vwap)
                mtm += mul(qty, carry.fill.perp_vwap) - mul(qty, pb.best_ask.price)
            if pp_ctx is not None:
                for pp in pp_ctx.open.values():
                    lb = books.get(pp.long_key)
                    sb2 = books.get(pp.short_key)
                    if lb is None or sb2 is None:
                        continue
                    qty = D(pp.fill.qty_base)
                    mtm += D(pp.funding_collected_net)
                    mtm += mul(qty, lb.best_bid.price) - mul(qty, pp.fill.long_vwap)
                    mtm += mul(qty, pp.fill.short_vwap) - mul(qty, sb2.best_ask.price)
            if open_carries or (pp_ctx is not None and pp_ctx.open):
                breaker.record_mtm_usd(mtm)

            if pp_ctx is not None:
                try:
                    await pp_manage(pp_ctx)
                except Exception as exc:  # decoupled: carry management continues
                    logger.exception("perp-perp manage failed: %s", exc)
                    await notifier.send(f"PP MODULE ERROR (manage): {exc}")

            uta_line = ""
            if uta_state is not None:
                mmr_txt = f"{last_mmr * 100:.1f}%" if last_mmr is not None else "n/a"
                pump_txt = (
                    f"+{max_safe_pump * 100:.0f}%" if max_safe_pump is not None else "inf"
                )
                uta_line = (
                    f"margin: UNIFIED {settings.uta_capital_utilization_pct * 100:.0f}% util | "
                    f"worst ratio {coll_ratio * 100:.0f}% | "
                    f"MMR {mmr_txt} (warn {settings.max_account_mmr_warning * 100:.0f}%, "
                    f"unwind {settings.max_account_mmr_emergency_unwind * 100:.0f}%) | "
                    f"max safe pump {pump_txt}"
                )
            live.update(
                render(
                    books,
                    funding,
                    list(opportunities),
                    breaker,
                    len(open_carries) + (len(pp_ctx.open) if pp_ctx else 0),
                    realized_pnl + pp_realized[0],
                    uta_line,
                )
            )
            await asyncio.sleep(0.5)

    stop.set()
    for t in tasks:
        t.cancel()
    # op.#11: await cancellation so feeds close ccxt clients and the stores
    # flush+close cleanly; close() itself flushes the duckdb buffer (op.#15)
    await asyncio.gather(*tasks, return_exceptions=True)
    ticks.close()
    await repo.close()


def entrypoint() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "export-tax":
        kind = sys.argv[3] if len(sys.argv) >= 4 else "live"

        async def _export() -> int:
            repo = Repository(get_settings().sqlite_url)
            await repo.init()
            rows = await repo.export_tax_csv(sys.argv[2], kind=kind)
            await repo.close()
            return rows

        rows = asyncio.run(_export())
        print(f"exported {rows} {kind} audit rows to {sys.argv[2]}")
        return
    asyncio.run(run())


if __name__ == "__main__":
    entrypoint()