"""Streamlit front for the crypto-arb-engine soak.

Run:  streamlit run front/app.py
Read-only view over the engine's data files; the engine never imports this.
"""
import streamlit as st

from front.data import account_health, funding_log, latest_books, positions
from front.views import (
    account_health_section,
    books_section,
    funding_section,
    positions_section,
)

st.set_page_config(page_title="Crypto Arb Engine", layout="wide")
st.title("Crypto Arb Engine - paper soak")
st.caption("Solo lectura sobre los datos del motor. Se actualiza al recargar.")

account_health_section(account_health())
funding_section(funding_log())
positions_section(positions())
books_section(latest_books())
