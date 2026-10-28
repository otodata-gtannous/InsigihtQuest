from __future__ import annotations

import pandas as pd
import pydeck as pdk
import streamlit as st

from insightquest import charts
from insightquest.aggregates import monthly_seasonality
from insightquest.analytics import backtest_trend_forecast, build_map_clusters, project_trend
from insightquest.cache import cached_event_counts
from insightquest.i18n import format_int, format_number, format_percent, t
from insightquest.page_helpers import (
    PageContext,
    cached_diagnostics,
    cached_map_rows,
    data_table,
    describe_missing,
    export_frame,
    mapped_status,
    run_analysis_page,
)

GRAINS = {"Day": "day", "Week": "week", "Month": "month"}
MAP_REQUIREMENTS = ("col_device", "col_customer", "col_installed_at", "col_latitude", "col_longitude")


def need(ctx: PageContext, *attributes: str) -> bool:
    status = mapped_status(ctx.config, ctx.columns, attributes)
    missing = {a: s for a, s in status.items() if s != "ok"}
    if missing:
        st.info(t("state.columns_missing", columns=describe_missing(ctx.config, missing)))
    return not missing


def render_map(ctx: PageContext, limit: int, rounding: int) -> dict[str, pd.DataFrame]:
    if not need(ctx, *MAP_REQUIREMENTS):
        return {}
    rows = cached_map_rows(ctx.config, ctx.from_date, ctx.to_date, limit)
    ctx.check_row_limit(len(rows), limit)
    if rows.empty:
        st.warning(t("overview.map_empty"))
        return {}
    clustered = build_map_clusters(rows, rounding)
    if clustered.empty:
        st.warning(t("overview.map_no_clusters"))
        return {}
    viewport = pdk.data_utils.compute_view(clustered[["lon", "lat"]])
    viewport.zoom = min(viewport.zoom, 12)
    viewport.pitch = 0
    layer = pdk.Layer(
        "ScatterplotLayer", data=clustered, get_position="[lon, lat]", get_radius="radius",
        radius_min_pixels=5, radius_max_pixels=40, get_fill_color=charts.MAP_POINT_COLOR, pickable=True,
    )
    tooltip = {"html": (
        f"<b>{t('overview.installs')}:</b> {{installs}}<br/><b>{t('overview.customers')}:</b> {{customers}}<br/>"
        f"<b>{t('overview.top_devices')}:</b> {{top_devices}}"
    )}
    st.pydeck_chart(pdk.Deck(initial_view_state=viewport, layers=[layer], tooltip=tooltip, map_style=charts.MAP_STYLE), use_container_width=True)
    summary = (
        clustered.groupby(["lat", "lon"], dropna=False)
        .agg(installs=("installs", "sum"), customers=("customers", "max"),
             latest_install=("latest_install", "max"), top_devices=("top_devices", "first"))
        .sort_values("installs", ascending=False).head(25).reset_index()
    )
    st.caption(t("overview.map_summary", rows=format_int(len(rows)), clusters=format_int(len(clustered))))
    st.subheader(t("overview.top_clusters"))
    data_table(summary, "common.data_table")
    return {t("overview.top_clusters"): export_frame(summary)}


def render_territories(ctx: PageContext, period: str) -> dict[str, pd.DataFrame]:
    if not need(ctx, "col_installed_at", "col_territory"):
        return {}
    counts = cached_event_counts(ctx.config, "installs", GRAINS[period], ctx.from_date, ctx.to_date, "territory")
    if counts.empty:
        ctx.empty()
        return {}
    options = sorted(counts["segment"].unique())
    chosen = st.multiselect(t("overview.territories"), options, default=options, help=t("overview.territories_help"))
    if not chosen:
        st.info(t("overview.pick_territory"))
        return {}
    series = counts[counts["segment"].isin(chosen)].rename(columns={"period": "period_start"})
    charts.show(charts.line_chart(
        series, "period_start", "events", t("common.period_start"), t("overview.axis_installs"),
        series="segment", series_title=t("q2.territory"),
    ))
    totals = series.groupby("segment", as_index=False)["events"].sum().sort_values("events", ascending=False)
    charts.show(charts.bar_chart(totals, "segment", "events", t("q2.territory"), t("overview.axis_installs"), horizontal=True))
    st.caption(t("overview.territory_summary", top=totals.iloc[0]["segment"], count=format_int(totals.iloc[0]["events"])))
    data_table(with_period(series))
    return {t("overview.territory_totals"): export_frame(totals)}


def with_period(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(period_start=pd.to_datetime(frame["period_start"]).dt.date)


def render_projection(ctx: PageContext, period: str, periods_ahead: int) -> dict[str, pd.DataFrame]:
    if not need(ctx, "col_installed_at", "col_customer"):
        return {}
    counts = cached_event_counts(ctx.config, "installs", GRAINS[period], ctx.from_date, ctx.to_date, "customer")
    if counts.empty:
        ctx.empty()
        return {}
    ranked = counts.groupby("segment")["events"].sum().sort_values(ascending=False)
    chosen = st.multiselect(
        t("overview.customers_filter"), sorted(ranked.index), default=sorted(ranked.index[:5]),
        help=t("overview.customers_help"),
    )
    if not chosen:
        st.info(t("overview.pick_customer"))
        return {}
    history = (
        counts[counts["segment"].isin(chosen)].groupby("period", as_index=False)["events"].sum()
        .rename(columns={"period": "period_start", "events": "installs"})
    )
    projection = project_trend(history, periods_ahead, period)
    if projection.history.empty:
        st.warning(t("overview.not_enough_data"))
        return {}
    backtest = backtest_trend_forecast(history, period)
    forecast = projection.projection
    if forecast.empty:
        charts.show(charts.line_chart(projection.history, "period_start", "installs", t("common.period_start"), t("overview.axis_installs")))
    else:
        charts.show(charts.forecast_chart(
            projection.history, forecast, "period_start", "installs", "installs", t("common.period_start"),
            t("overview.axis_installs"), t("common.actual"), t("common.forecast"),
        ))
    st.caption(t("overview.projection_summary", periods=periods_ahead, period=t(f"period.{period.lower()}")))

    st.subheader(t("overview.backtest_title"))
    if backtest.folds == 0:
        st.info(t("overview.backtest_none"))
    else:
        cols = st.columns(4)
        cols[0].metric(t("overview.model"), backtest.model_name)
        cols[1].metric("MAE", format_number(backtest.mae, 2))
        cols[2].metric("RMSE", format_number(backtest.rmse, 2))
        cols[3].metric("MAPE", f"{format_number(backtest.mape, 2)}%" if backtest.mape is not None else "N/A")
        st.caption(t("overview.backtest_folds", folds=backtest.folds))
        data_table(with_period(backtest.details.sort_values("period_start", ascending=False).head(24)))

    monthly = (
        cached_event_counts(ctx.config, "installs", "month", ctx.from_date, ctx.to_date, "customer")
        .query("segment in @chosen").groupby("period", as_index=False)["events"].sum()
        .rename(columns={"period": "period_start", "events": "installs"})
    )
    seasonality = monthly_seasonality(monthly)
    if not seasonality.empty:
        st.subheader(t("overview.seasonality_title"))
        charts.show(charts.bar_chart(seasonality, "month_name", "avg_installs", t("common.month"), t("overview.axis_avg_installs")))
        data_table(seasonality)
    sheets = {t("overview.history"): export_frame(projection.history)}
    if not forecast.empty:
        sheets[t("overview.projection")] = export_frame(forecast)
    return sheets


def render_diagnostics(ctx: PageContext) -> dict[str, pd.DataFrame]:
    if not need(ctx, "col_installed_at", "col_customer", "col_latitude", "col_longitude"):
        return {}
    diagnostics = cached_diagnostics(ctx.config, ctx.from_date, ctx.to_date)
    if diagnostics.empty:
        ctx.empty()
        return {}
    row = diagnostics.iloc[0]
    total = int(row["total_devices_in_range"])
    cols = st.columns(3)
    cols[0].metric(t("overview.devices_in_range"), format_int(total))
    for column, key, label in ((cols[1], "with_coordinates", "overview.with_coordinates"), (cols[2], "with_customer", "overview.with_customer")):
        share = int(row[key]) / total if total else 0
        column.metric(t(label), format_int(row[key]), format_percent(share, 1), delta_color="off")
    sources = pd.DataFrame({
        "install_source": ["InitialInstallDate", "DetectedInstallDate", "TankSensors.CreatedDate"],
        "devices": [int(row["from_initial_install_date"]), int(row["from_detected_install_date"]), int(row["from_tanksensor_created_date"])],
    })
    sources["share"] = sources["devices"].map(lambda v: format_percent(v / total if total else 0, 1))
    st.subheader(t("overview.install_sources"))
    st.dataframe(sources, use_container_width=True, hide_index=True)
    st.caption(t("overview.diagnostics_summary", total=format_int(total)))
    return {t("overview.diagnostics"): export_frame(diagnostics), t("overview.install_sources"): sources}


def body(ctx: PageContext) -> dict[str, pd.DataFrame] | None:
    period = st.sidebar.selectbox(
        t("common.period"), list(GRAINS), index=1, format_func=lambda v: t(f"period.{v.lower()}"),
        help=t("common.period_help"),
    )
    periods_ahead = st.sidebar.slider(t("overview.projection_periods"), 2, 24, 8, help=t("overview.projection_help"))
    rounding = st.sidebar.slider(t("overview.cluster_precision"), 1, 4, 2, help=t("overview.cluster_help"))
    map_limit = ctx.row_limit_input(default=5_000, maximum=50_000, key="iq_overview_map_limit")
    ctx.filters[t("common.period")] = t(f"period.{period.lower()}")

    sheets: dict[str, pd.DataFrame] = {}
    map_tab, territory_tab, trend_tab, diagnostics_tab = st.tabs([
        t("overview.tab_map"), t("overview.tab_territory"), t("overview.tab_trend"), t("overview.tab_diagnostics"),
    ])
    with map_tab:
        sheets.update(render_map(ctx, map_limit, rounding))
    with territory_tab:
        sheets.update(render_territories(ctx, period))
    with trend_tab:
        sheets.update(render_projection(ctx, period, periods_ahead))
    with diagnostics_tab:
        sheets.update(render_diagnostics(ctx))
    return sheets


run_analysis_page(None, body, title_key="page.overview", description_key="page.overview_desc", export_page="overview")
