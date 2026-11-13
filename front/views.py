"""Streamlit sections. Each takes plain dataframes/dicts from front.data."""
from __future__ import annotations

import pandas as pd
import streamlit as st


def account_health_section(health: list[dict]) -> None:
    st.subheader("Cuentas reales (solo lectura)")
    if not health:
        st.info("Sin datos de salud todavia (el motor escribe una linea cada 60s).")
        return
    cols = st.columns(len(health))
    for col, h in zip(cols, health):
        col.metric(h["venue"], f"equity {h['equity']}", f"mmr {h['mmr']}")


def funding_section(funding: pd.DataFrame) -> None:
    st.subheader("Funding rates")
    if funding.empty:
        st.info("Sin snapshots de funding todavia.")
        return
    latest = funding.sort_values("ts").groupby(["exchange", "symbol"]).tail(1)
    cols = st.columns(max(1, len(latest)))
    for col, (_, row) in zip(cols, latest.iterrows()):
        col.metric(
            f"{row['exchange']}",
            f"{row['rate'] * 100:.4f}%",
            row["ts"].strftime("%H:%M:%S"),
        )
    st.caption("Historico (cada punto es un snapshot, funding por intervalo de 8h)")
    chart = funding.pivot_table(index="ts", columns="exchange", values="rate")
    st.line_chart(chart * 100)


def books_section(books: pd.DataFrame) -> None:
    st.subheader("Libros de ordenes (ultimo snapshot)")
    if books.empty:
        st.info("Sin snapshots de libro todavia.")
        return
    st.dataframe(books, use_container_width=True)


def positions_section(positions: pd.DataFrame) -> None:
    st.subheader("Posiciones paper")
    if positions.empty:
        st.info("Sin posiciones paper abiertas (normal: el gate es estricto).")
        return
    st.dataframe(positions, use_container_width=True)
