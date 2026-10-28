from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from insightquest.analytics import (
    backtest_trend_forecast,
    build_cohort_installation_rate,
    build_installation_periods,
    build_sales_periods,
    project_trend,
)
from insightquest.data import fetch_flat_table_rows
from insightquest.exports import render_excel_download
from insightquest.page_helpers import (
    app_page_icon,
    get_source_context,
    render_analysis_page,
    render_date_filters,
    show_source_readiness,
    warn_if_row_limit_reached,
)

st.set_page_config(
    page_title="Installation Rate & Forecasts | InsightQuest",
    page_icon=app_page_icon(),
    layout="wide",
)

render_analysis_page(
    "Installation rate & forecasts",
    "Review shipment-cohort installation rates and forecast purchase and installation activity separately.",
)
st.sidebar.header("Analysis filters")
from_date, to_date = render_date_filters(default_days=730)
forecast_periods = st.sidebar.slider("Months to forecast", 2, 24, 8)
install_limit = int(
    st.sidebar.number_input(
        "Maximum installation rows",
        min_value=100,
        max_value=200_000,
        value=20_000,
        step=100,
    )
)

if from_date > to_date:
    st.error("From date must be earlier than or equal to To date.")
    st.stop()

try:
    config, engine, source_columns = get_source_context()
except (SQLAlchemyError, ValueError) as ex:
    st.error("Unable to inspect the configured flat-table source.")
    st.exception(ex)
    st.stop()

export_sheets: dict[str, pd.DataFrame] = {}


def render_forecast(title: str, history: pd.DataFrame, metric_name: str, periods: int) -> None:
    st.subheader(title)
    if history.empty:
        st.info(f"No {metric_name.lower()} history is available for forecasting.")
        return
    forecast = project_trend(history, periods, "Month")
    backtest = backtest_trend_forecast(history, "Month")
    if not backtest.details.empty:
        residuals = (
            backtest.details["actual"].astype(float)
            - backtest.details["predicted"].astype(float)
        )
        forecast.projection["lower_bound"] = (
            forecast.projection["installs"] + residuals.quantile(0.1)
        ).clip(lower=0)
        forecast.projection["upper_bound"] = (
            forecast.projection["installs"] + residuals.quantile(0.9)
        ).clip(lower=0)
    else:
        forecast.projection["lower_bound"] = pd.NA
        forecast.projection["upper_bound"] = pd.NA
    historical = forecast.history.assign(series="Historical")
    projected = forecast.projection.assign(series="Projected")
    chart = pd.concat([historical, projected], ignore_index=True)
    export_sheets[f"{metric_name} forecast"] = chart
    if not backtest.details.empty:
        export_sheets[f"{metric_name} backtest"] = backtest.details
    st.line_chart(chart, x="period_start", y="installs", color="series", use_container_width=True)
    metrics = st.columns(4)
    metrics[0].metric("Forecast model", backtest.model_name)
    metrics[1].metric("Backtest folds", f"{backtest.folds:,}")
    metrics[2].metric("MAE", f"{backtest.mae:.2f}" if backtest.mae is not None else "N/A")
    metrics[3].metric("RMSE", f"{backtest.rmse:.2f}" if backtest.rmse is not None else "N/A")
    if backtest.mape is not None:
        st.caption(f"Backtest MAPE: {backtest.mape:.2f}%")
    if not forecast.projection.empty:
        st.dataframe(
            forecast.projection.rename(
                columns={
                    "installs": metric_name,
                    "lower_bound": "Backtest lower envelope",
                    "upper_bound": "Backtest upper envelope",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )
        if not backtest.details.empty:
            st.caption(
                "The forecast envelope uses the 10th–90th percentile of rolling "
                "backtest residuals; it is an empirical range, not a formal confidence interval."
            )
        else:
            st.caption("Not enough backtest history to estimate a forecast envelope.")

installation_requirements = {"Installation date": "col_installed_at"}
installation_ready = show_source_readiness(
    config, source_columns, installation_requirements
)
if installation_ready:
    try:
        installation_rows = fetch_flat_table_rows(
            engine,
            config,
            {"installed_at": "col_installed_at"},
            date_field="col_installed_at",
            from_date=from_date,
            to_date=to_date,
            limit=install_limit,
        )
        warn_if_row_limit_reached(len(installation_rows), install_limit)
        installation_history = build_installation_periods(
            installation_rows, "Month", from_date, to_date
        ).rename(columns={"installs": "installs"})
        render_forecast(
            "Monthly installation forecast",
            installation_history,
            "Installations",
            forecast_periods,
        )
    except (SQLAlchemyError, ValueError) as ex:
        st.error("Unable to load installation history.")
        st.exception(ex)

purchase_requirements = {
    "Purchase date": "col_sales_date",
    "Purchase measure": "col_sales_amount",
}
purchase_history_for_market: pd.DataFrame | None = None
if show_source_readiness(config, source_columns, purchase_requirements):
    try:
        purchase_rows = fetch_flat_table_rows(
            engine,
            config,
            {"purchase_date": "col_sales_date", "sales": "col_sales_amount"},
            date_field="col_sales_date",
            from_date=from_date,
            to_date=to_date,
            limit=install_limit,
        )
        warn_if_row_limit_reached(len(purchase_rows), install_limit)
        purchase_history = build_sales_periods(
            purchase_rows, "Month", from_date, to_date
        ).rename(columns={"sales": "installs"})
        purchase_history_for_market = purchase_history.rename(
            columns={"installs": "purchase_measure"}
        )
        render_forecast(
            "Monthly purchase-measure forecast",
            purchase_history,
            "Purchase measure",
            forecast_periods,
        )
        st.caption(
            "The purchase forecast projects the configured purchase amount measure. "
            "It is not a purchase count unless that source column represents counts."
        )
    except (SQLAlchemyError, ValueError) as ex:
        st.error("Unable to load purchase history.")
        st.exception(ex)

rate_requirements = {
    "Shipment date": "col_shipped_at",
    "Installation date": "col_installed_at",
    "Eligible shipped-unit denominator": "col_rate_denominator",
}
st.subheader("Monthly shipment-cohort installation rate")
if show_source_readiness(config, source_columns, rate_requirements):
    try:
        cohort_data = fetch_flat_table_rows(
            engine,
            config,
            {
                "shipping_date": "col_shipped_at",
                "installed_at": "col_installed_at",
                "eligible_units": "col_rate_denominator",
            },
            date_field="col_shipped_at",
            from_date=from_date,
            to_date=to_date,
            limit=install_limit,
        )
        warn_if_row_limit_reached(len(cohort_data), install_limit)
        cohorts = build_cohort_installation_rate(cohort_data, "eligible_units")
    except (SQLAlchemyError, ValueError) as ex:
        st.error("Unable to calculate shipment-cohort installation rates.")
        st.exception(ex)
    else:
        if cohorts.empty:
            st.info("No valid shipped-unit denominator is available in the selected range.")
        else:
            export_sheets["Shipment cohort rates"] = cohorts
            st.line_chart(
                cohorts,
                x="cohort_month",
                y="installation_rate_pct",
                use_container_width=True,
            )
            st.dataframe(cohorts, use_container_width=True, hide_index=True)
            st.caption(
                "Rate is the eligible units installed to date divided by eligible units "
                "shipped in each shipment-month cohort. Recent cohorts may be incomplete."
            )

market_requirements = {
    "Market indicator date": "col_market_indicator_at",
    "Market indicator": "col_market_indicator",
}
st.subheader("Market indicator context")
if show_source_readiness(config, source_columns, market_requirements):
    try:
        market_rows = fetch_flat_table_rows(
            engine,
            config,
            {
                "market_date": "col_market_indicator_at",
                "market_indicator": "col_market_indicator",
            },
            date_field="col_market_indicator_at",
            from_date=from_date,
            to_date=to_date,
            limit=install_limit,
        )
        warn_if_row_limit_reached(len(market_rows), install_limit)
    except (SQLAlchemyError, ValueError) as ex:
        st.error("Unable to load the configured market indicator.")
        st.exception(ex)
    else:
        market_rows["market_indicator"] = pd.to_numeric(
            market_rows["market_indicator"], errors="coerce"
        )
        market_rows["market_month"] = (
            pd.to_datetime(market_rows["market_date"], errors="coerce")
            .dt.to_period("M")
            .dt.to_timestamp()
        )
        market_monthly = (
            market_rows.dropna(subset=["market_month", "market_indicator"])
            .groupby("market_month", as_index=False)["market_indicator"]
            .mean()
        )
        if market_monthly.empty:
            st.info("No valid market indicator values are available for this period.")
        else:
            export_sheets["Market indicator"] = market_monthly
            if purchase_history_for_market is None:
                st.line_chart(
                    market_monthly,
                    x="market_month",
                    y="market_indicator",
                    use_container_width=True,
                )
                st.info("Map purchase date and measure to compare the market series.")
            else:
                aligned = purchase_history_for_market.merge(
                    market_monthly,
                    left_on="period_start",
                    right_on="market_month",
                    how="inner",
                )
                aligned = aligned.dropna(
                    subset=["purchase_measure", "market_indicator"]
                )
                export_sheets["Market purchase comparison"] = aligned
                if len(aligned) >= 2:
                    purchase_base = aligned["purchase_measure"].iloc[0]
                    market_base = aligned["market_indicator"].iloc[0]
                    if purchase_base != 0 and market_base != 0:
                        aligned["Purchases (index 100)"] = (
                            aligned["purchase_measure"] / purchase_base * 100
                        )
                        aligned["Market indicator (index 100)"] = (
                            aligned["market_indicator"] / market_base * 100
                        )
                        st.line_chart(
                            aligned,
                            x="period_start",
                            y=[
                                "Purchases (index 100)",
                                "Market indicator (index 100)",
                            ],
                            use_container_width=True,
                        )
                    if aligned["purchase_measure"].nunique() > 1 and aligned[
                        "market_indicator"
                    ].nunique() > 1:
                        correlation = aligned[
                            ["purchase_measure", "market_indicator"]
                        ].corr().iloc[0, 1]
                        st.metric(
                            "Historical purchase/market correlation",
                            f"{correlation:.2f}",
                        )
                else:
                    st.info("At least two aligned months are needed for comparison.")
            st.caption(
                "The market comparison is descriptive and does not imply causation "
                "or feed the purchase forecast."
            )

render_excel_download(export_sheets, "installation-rate-and-forecasts.xlsx")
