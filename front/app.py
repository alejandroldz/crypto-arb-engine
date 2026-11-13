"""Streamlit front for the crypto-arb-engine soak.

Run:  streamlit run front/app.py
Read-only view over the engine's data files; the engine never imports this.
"""
import streamlit as st

from front import views
from front.data import account_health, funding_log, latest_books, positions

st.set_page_config(
    page_title="Crypto Arb Engine",
    page_icon="📈",
    layout="wide",
)


@st.fragment(run_every=30)
def dashboard() -> None:
    """Auto-refreshes every 30s without a full page reload."""
    health = account_health()
    funding = funding_log()
    pos = positions()

    views.status_pill(health)
    views.kpi_row(funding, pos, health)

    tab_resumen, tab_funding, tab_pos, tab_books = st.tabs(
        ["Cuentas", "Funding", "Posiciones", "Libros"]
    )
    with tab_resumen:
        views.health_cards(health)
    with tab_funding:
        views.funding_section(funding)
    with tab_pos:
        views.positions_section(pos)
    with tab_books:
        views.books_section(latest_books())


st.title("Crypto Arb Engine")
st.caption(
    "Paper trading delta-neutral (spot + short perp) - Kraken y OKX. "
    "Vista de solo lectura, se actualiza sola cada 30s."
)
dashboard()
