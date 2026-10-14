from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pydeck as pdk
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from insightquest.analytics import (
    backtest_trend_forecast,
    build_map_clusters,
    build_monthly_seasonality,
    build_territory_trend_series,
    build_trend_series,
    project_trend,
)
from insightquest.config import get_config
from insightquest.data import (
    fetch_data_source_diagnostics,
    fetch_install_map_data,
    fetch_install_territory_trend_data,
    fetch_install_trend_data,
)
from insightquest.db import create_db_engine

st.set_page_config(page_title="InsightQuest", layout="wide")


@st.cache_resource
def _load_engine():
    config = get_config()
    return create_db_engine(config)


@st.cache_data(ttl=300)
def _load_map_data(from_date: date, to_date: date, limit: int) -> pd.DataFrame:
    config = get_config()
    engine = _load_engine()
    return fetch_install_map_data(engine, config, from_date, to_date, limit)


@st.cache_data(ttl=300)
def _load_trend_data(from_date: date, to_date: date, limit: int) -> pd.DataFrame:
    config = get_config()
    engine = _load_engine()
    return fetch_install_trend_data(engine, config, from_date, to_date, limit)


@st.cache_data(ttl=300)
def _load_territory_trend_data(from_date: date, to_date: date, limit: int) -> pd.DataFrame:
    engine = _load_engine()
    return fetch_install_territory_trend_data(engine, from_date, to_date, limit)


@st.cache_data(ttl=300)
def _load_diagnostics(from_date: date, to_date: date) -> pd.DataFrame:
    engine = _load_engine()
    return fetch_data_source_diagnostics(engine, from_date, to_date)


def main() -> None:
    st.title("InsightQuest")
    st.caption("Explore monitor installations, trends, projections, and sales by territory")

    st.sidebar.header("Filters")
    default_to = date.today()
    default_from = default_to - timedelta(days=180)

    from_date = st.sidebar.date_input("From date", default_from)
    to_date = st.sidebar.date_input("To date", default_to)
    map_limit = st.sidebar.number_input(
        "Max rows to load",
        min_value=100,
        max_value=50000,
        value=5000,
        step=100,
    )
    trend_limit = st.sidebar.number_input(
        "Max trend rows to load",
        min_value=100,
        max_value=200000,
        value=20000,
        step=100,
    )
    trend_period = st.sidebar.selectbox("Trend period", options=["Day", "Week", "Month"], index=1)
    projection_periods = st.sidebar.slider("Projection periods", min_value=2, max_value=24, value=8)
    cluster_rounding = st.sidebar.slider(
        "Cluster precision (decimals)",
        min_value=1,
        max_value=4,
        value=2,
        help="Lower precision creates larger clusters.",
    )

    if from_date > to_date:
        st.error("From date must be earlier than or equal to To date.")
        return

    try:
        map_data = _load_map_data(from_date, to_date, int(map_limit))
        trend_raw = _load_trend_data(from_date, to_date, int(trend_limit))
        territory_trend_raw = _load_territory_trend_data(
            from_date, to_date, int(trend_limit)
        )
        diagnostics = _load_diagnostics(from_date, to_date)
    except ValueError as ex:
        st.error(str(ex))
        st.stop()
    except SQLAlchemyError as ex:
        st.error(
            "Database error while loading install data. "
            "Check `.env` values for server, credentials, table, and column names."
        )
        st.exception(ex)
        st.stop()
    except Exception as ex:  # pragma: no cover - defensive app boundary
        st.error("Unexpected error while loading install data.")
        st.exception(ex)
        st.stop()

    map_tab, sales_tab, trend_tab, diag_tab = st.tabs(
        ["Install map", "Sales by territory", "Trend & projection", "Data diagnostics"]
    )

    with map_tab:
        if map_data.empty:
            st.warning(
                "No mappable rows found for the selected period. "
                "If your `DeviceLocation` is not 'lat,lon', configure IQ_COL_LATITUDE and IQ_COL_LONGITUDE."
            )
        else:
            st.success(f"Loaded {len(map_data):,} install rows for map.")
            clustered = build_map_clusters(map_data, cluster_rounding)
            if clustered.empty:
                st.warning("No cluster points available from the current map data.")
            else:
                viewport = pdk.ViewState(
                    latitude=float(clustered["lat"].mean()),
                    longitude=float(clustered["lon"].mean()),
                    zoom=3.5,
                    pitch=30,
                )
                layer = pdk.Layer(
                    "ScatterplotLayer",
                    data=clustered,
                    get_position="[lon, lat]",
                    get_radius="radius",
                    get_fill_color=[0, 128, 255, 180],
                    pickable=True,
                )
                tooltip = {
                    "html": (
                        "<b>Installs:</b> {installs}<br/>"
                        "<b>Customers:</b> {customers}<br/>"
                        "<b>Top monitors:</b> {top_devices}"
                    )
                }
                st.pydeck_chart(
                    pdk.Deck(
                        initial_view_state=viewport,
                        layers=[layer],
                        tooltip=tooltip,
                    ),
                    use_container_width=True,
                )

            summary = (
                clustered.groupby(["lat", "lon"], dropna=False)
                .agg(
                    installs=("installs", "sum"),
                    customers=("customers", "max"),
                    latest_install=("latest_install", "max"),
                    top_devices=("top_devices", "first"),
                )
                .sort_values("installs", ascending=False)
                .head(25)
                .reset_index()
            )
            st.subheader("Top install clusters")
            st.dataframe(summary, use_container_width=True)

    with sales_tab:
        st.caption(
            "Sales are represented by device installs. Each install is attributed to "
            "the territory of the device's current owner company."
        )
        if territory_trend_raw.empty:
            st.warning("No installation rows found for territory sales in the selected period.")
        else:
            territory_options = sorted(
                territory_trend_raw["territory_name"].dropna().astype(str).unique().tolist()
            )
            selected_territories = st.multiselect(
                "Territories",
                options=territory_options,
                default=territory_options,
            )
            if not selected_territories:
                st.info("Select at least one territory to display sales trends.")
            else:
                territory_series = build_territory_trend_series(
                    territory_trend_raw,
                    trend_period,
                    territory_filter=selected_territories,
                )
                territory_totals = (
                    territory_series.groupby("territory_name")["installs"]
                    .sum()
                    .sort_values(ascending=False)
                    .rename("installs")
                    .reset_index()
                )

                st.success(f"Loaded {len(territory_trend_raw):,} device installs.")
                st.subheader(f"Device installs by territory and {trend_period.lower()}")
                st.line_chart(
                    territory_series,
                    x="period_start",
                    y="installs",
                    color="territory_name",
                    use_container_width=True,
                )

                st.subheader("Device install totals by territory")
                st.bar_chart(
                    territory_totals,
                    x="territory_name",
                    y="installs",
                    use_container_width=True,
                )
                st.dataframe(territory_totals, use_container_width=True)

    with trend_tab:
        if trend_raw.empty:
            st.warning("No installation rows found for trend in the selected period.")
            return

        if "customer_name" not in trend_raw.columns:
            trend_raw["customer_name"] = "Unknown"
        customer_options = sorted(trend_raw["customer_name"].dropna().astype(str).unique().tolist())
        selected_customers = st.multiselect(
            "Customers",
            options=customer_options,
            default=customer_options[: min(5, len(customer_options))],
            help="Select one or more customers for trend/projection.",
        )
        if not selected_customers:
            st.info("Select at least one customer to display trend and projection.")
            return

        history = build_trend_series(trend_raw, trend_period, customer_filter=selected_customers)
        projection = project_trend(history, periods_ahead=projection_periods, period=trend_period)
        backtest = backtest_trend_forecast(history, period=trend_period)

        st.success(f"Loaded {len(trend_raw):,} install rows for trend.")

        if projection.history.empty:
            st.warning("Not enough data to build trend.")
            return

        trend_chart_df = projection.history.copy()
        trend_chart_df["series"] = "Historical"

        if not projection.projection.empty:
            forecast_df = projection.projection.copy()
            forecast_df["series"] = "Projected"
            chart_df = pd.concat([trend_chart_df, forecast_df], ignore_index=True)
        else:
            chart_df = trend_chart_df

        st.subheader(f"Install trend by {trend_period.lower()}")
        st.line_chart(
            chart_df,
            x="period_start",
            y="installs",
            color="series",
            use_container_width=True,
        )

        st.subheader("Forecast quality (rolling backtest)")
        if backtest.folds == 0:
            st.info(
                "Not enough history for backtesting. "
                "Use a longer date range to compute MAE/RMSE/MAPE."
            )
        else:
            bt_col1, bt_col2, bt_col3, bt_col4 = st.columns(4)
            bt_col1.metric("Model", backtest.model_name)
            bt_col2.metric("MAE", f"{backtest.mae:.2f}" if backtest.mae is not None else "N/A")
            bt_col3.metric("RMSE", f"{backtest.rmse:.2f}" if backtest.rmse is not None else "N/A")
            bt_col4.metric("MAPE", f"{backtest.mape:.2f}%" if backtest.mape is not None else "N/A")
            st.caption(f"Backtest folds: {backtest.folds}")
            st.dataframe(
                backtest.details.sort_values("period_start", ascending=False).head(24),
                use_container_width=True,
            )

        customer_breakdown = (
            trend_raw[trend_raw["customer_name"].isin(selected_customers)]
            .groupby("customer_name")
            .size()
            .rename("installs")
            .sort_values(ascending=False)
            .reset_index()
        )
        st.subheader("Selected customer install totals")
        st.dataframe(customer_breakdown, use_container_width=True)

        seasonality_df = build_monthly_seasonality(
            trend_raw,
            customer_filter=selected_customers,
        )
        st.subheader("Monthly seasonality profile")
        if seasonality_df.empty:
            st.info("No seasonal data available for the selected customers/date range.")
        else:
            st.bar_chart(
                seasonality_df,
                x="month_name",
                y="avg_installs",
                use_container_width=True,
            )
            st.dataframe(seasonality_df, use_container_width=True)

        st.subheader("Trend and projection data")
        st.dataframe(
            chart_df.sort_values(["period_start", "series"]).reset_index(drop=True),
            use_container_width=True,
        )

    with diag_tab:
        if diagnostics.empty:
            st.warning("No diagnostics data available for the selected period.")
            return

        row = diagnostics.iloc[0]
        total = int(row["total_devices_in_range"])
        with_coords = int(row["with_coordinates"])
        with_customer = int(row["with_customer"])
        from_initial = int(row["from_initial_install_date"])
        from_detected = int(row["from_detected_install_date"])
        from_tanksensor = int(row["from_tanksensor_created_date"])

        col1, col2, col3 = st.columns(3)
        col1.metric("Devices in range", f"{total:,}")
        col2.metric(
            "With coordinates",
            f"{with_coords:,}",
            delta=f"{(with_coords / total * 100) if total else 0:.1f}%",
        )
        col3.metric(
            "With customer",
            f"{with_customer:,}",
            delta=f"{(with_customer / total * 100) if total else 0:.1f}%",
        )

        st.subheader("Install date source breakdown")
        source_df = pd.DataFrame(
            [
                {
                    "install_source": "InitialInstallDate",
                    "devices": from_initial,
                },
                {
                    "install_source": "DetectedInstallDate",
                    "devices": from_detected,
                },
                {
                    "install_source": "TankSensors.CreatedDate",
                    "devices": from_tanksensor,
                },
            ]
        )
        source_df["percentage"] = source_df["devices"].apply(
            lambda value: f"{(value / total * 100) if total else 0:.1f}%"
        )
        st.dataframe(source_df, use_container_width=True)


if __name__ == "__main__":
    main()
