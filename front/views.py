"""Streamlit sections. Each takes plain dataframes/dicts from front.data."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

def _fmt_usd(value: float) -> str:
    return f"${value:,.2f}"


def status_pill(health: list[dict]) -> bool:
    """Engine status header. Returns True when the engine looks alive."""
    now = datetime.now(timezone.utc)
    freshest: datetime | None = None
    for h in health:
        try:
            ts = datetime.strptime(h["ts"], "%Y-%m-%d %H:%M:%S,%f").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        freshest = ts if freshest is None else max(freshest, ts)
    alive = freshest is not None and (now - freshest).total_seconds() < 180
    seen = freshest.strftime("%H:%M:%S UTC") if freshest else "nunca"
    if alive:
        st.success(f"Motor activo - ultima senal {seen}", icon="🟢")
    else:
        st.error(f"Motor sin senal reciente - ultima senal {seen}", icon="🔴")
    return alive


def kpi_row(funding: pd.DataFrame, positions: pd.DataFrame, health: list[dict]) -> None:
    """Top-level numbers: the four things that answer 'va ganando algo?'."""
    cols = st.columns(4)

    ann = None
    if not funding.empty:
        latest = funding.sort_values("ts").groupby("exchange").tail(1)
        ann = float(latest["rate"].mean()) * 3 * 365 * 100
    cols[0].metric(
        "Funding medio anualizado",
        f"{ann:.2f}%" if ann is not None else "-",
        help="Media del ultimo snapshot por venue, x3 intervalos/dia x365",
    )

    open_pos = 0 if positions.empty else int((positions["status"] == "open").sum())
    cols[1].metric("Posiciones paper abiertas", open_pos)

    collected = (
        0.0
        if positions.empty or "funding_collected" not in positions
        else float(positions["funding_collected"].sum())
    )
    cols[2].metric("Funding cobrado (paper)", _fmt_usd(collected))

    venues = len(health)
    cols[3].metric("Venues con salud OK", f"{venues}/2")


def health_cards(health: list[dict]) -> None:
    if not health:
        st.info("Sin datos de salud todavia (el motor escribe una linea cada 60s).")
        return
    cols = st.columns(len(health))
    for col, h in zip(cols, health):
        col.metric(
            h["venue"],
            f"equity {h['equity']}",
            f"mmr {h['mmr']} - {h['ts'].split()[-1]}",
        )


def funding_section(funding: pd.DataFrame) -> None:
    if funding.empty:
        st.info("Sin snapshots de funding todavia.")
        return
    latest = (
        funding.sort_values("ts")
        .groupby("exchange")
        .tail(1)
        .set_index("exchange")
    )
    cols = st.columns(len(latest))
    for col, (exchange, row) in zip(cols, latest.iterrows()):
        annual = float(row["rate"]) * 3 * 365 * 100
        col.metric(
            exchange,
            f"{float(row['rate']) * 100:+.4f}%",
            f"{annual:+.2f}% anualizado",
        )
    st.caption("Rate por intervalo de 8h - cada punto es un snapshot")
    chart = funding.pivot_table(index="ts", columns="exchange", values="rate") * 100
    st.line_chart(chart, y_label="rate %")


def positions_section(positions: pd.DataFrame) -> None:
    if positions.empty:
        st.info(
            "Sin posiciones paper (normal: el gate exige beneficio neto "
            "tras fees y slippage antes de abrir)."
        )
        return
    st.dataframe(
        positions,
        use_container_width=True,
        hide_index=True,
        column_config={
            "notional": st.column_config.NumberColumn(format="$%.2f"),
            "funding_collected": st.column_config.NumberColumn(format="$%.4f"),
            "realized_pnl_usd": st.column_config.NumberColumn(format="$%.2f"),
        },
    )


def books_section(books: pd.DataFrame) -> None:
    if books.empty:
        st.info("Sin snapshots de libro todavia.")
        return
    books = books.copy()
    books["spread_bps"] = (
        (books["best_ask"] - books["best_bid"]) / books["best_bid"] * 10_000
    ).round(2)
    st.dataframe(
        books[
            ["exchange", "symbol", "best_bid", "best_ask", "spread_bps", "ts_utc"]
        ],
        use_container_width=True,
        hide_index=True,
        column_config={
            "best_bid": st.column_config.NumberColumn(format="$%.2f"),
            "best_ask": st.column_config.NumberColumn(format="$%.2f"),
            "spread_bps": st.column_config.NumberColumn(format="%.2f bps"),
        },
    )
