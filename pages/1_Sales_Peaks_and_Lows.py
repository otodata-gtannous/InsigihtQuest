from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from insightquest.analytics import build_sales_periods
from insightquest.config import get_config
from insightquest.data import fetch_sales_data
from insightquest.db import create_db_engine

st.set_page_config(page_title="Sales Peaks and Lows | InsightQuest", layout="wide")


@st.cache_resource
def _load_engine():
    return create_db_engine(get_config())


@st.cache_data(ttl=300)
def _load_sales_data(from_date: date, to_date: date) -> pd.DataFrame:
    return fetch_sales_data(_load_engine(), from_date, to_date)


def _format_periods(periods: pd.DataFrame) -> pd.DataFrame:
    formatted = periods.copy()
    formatted["period_start"] = formatted["period_start"].dt.strftime("%Y-%m-%d")
    return formatted.rename(columns={"period_start": "Period", "sales": "Purchases"})


def main() -> None:
    st.title("Sales peaks and lows")
    st.caption(
        "Purchase volume is counted from owner records linked to devices, "
        "using Ownerships.CreatedDate as the purchase date."
    )

    st.sidebar.header("Sales filters")
    to_date = date.today()
    from_date = st.sidebar.date_input("From date", to_date - timedelta(days=180))
    end_date = st.sidebar.date_input("To date", to_date)
    period = st.sidebar.selectbox("Period", options=["Day", "Week", "Month"], index=1)

    if from_date > end_date:
        st.error("From date must be earlier than or equal to To date.")
        return

    try:
        sales_raw = _load_sales_data(from_date, end_date)
    except ValueError as ex:
        st.error(str(ex))
        st.stop()
    except SQLAlchemyError as ex:
        st.error(
            "Database error while loading sales. Check the `Ownerships` and `Devices` "
            "tables and the configured database connection. "
            "If login fails, verify IQ_DB_AUTH_MODE and credentials in `.env`."
        )
        st.exception(ex)
        st.stop()
    except Exception as ex:  # pragma: no cover - defensive app boundary
        st.error("Unexpected error while loading sales.")
        st.exception(ex)
        st.stop()

    periods = build_sales_periods(sales_raw, period, from_date, end_date)
    if periods.empty:
        st.info("No periods fall within the selected date range.")
        return

    peak = periods.loc[periods["sales"].idxmax()]
    low = periods.loc[periods["sales"].idxmin()]
    total_sales = int(periods["sales"].sum())
    peak_label = pd.Timestamp(peak["period_start"]).strftime("%Y-%m-%d")
    low_label = pd.Timestamp(low["period_start"]).strftime("%Y-%m-%d")

    total_col, peak_col, low_col = st.columns(3)
    total_col.metric("Purchases in range", f"{total_sales:,}")
    peak_col.metric(f"Peak {period.lower()}", f"{int(peak['sales']):,}", peak_label)
    low_col.metric(f"Low {period.lower()}", f"{int(low['sales']):,}", low_label)

    if sales_raw.empty:
        st.warning("No qualifying owner purchases were found in the selected date range.")

    st.subheader(f"Purchase volume by {period.lower()}")
    st.bar_chart(periods, x="period_start", y="sales", use_container_width=True)

    peak_col, low_col = st.columns(2)
    with peak_col:
        st.subheader("Highest-sales periods")
        st.dataframe(
            _format_periods(
                periods.sort_values(["sales", "period_start"], ascending=[False, True]).head(10)
            ),
            use_container_width=True,
            hide_index=True,
        )
    with low_col:
        st.subheader("Lowest-sales periods")
        st.dataframe(
            _format_periods(
                periods.sort_values(["sales", "period_start"], ascending=[True, True]).head(10)
            ),
            use_container_width=True,
            hide_index=True,
        )


if __name__ == "__main__":
    main()
