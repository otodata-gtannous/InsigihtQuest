"""Colour-blind-safe Altair charts shared by every analysis view, designed for the dark theme.

The dark baseline lives in .streamlit/config.toml; THEME below mirrors it so every chart
colour is defined in this one place. The palette is Okabe-Ito ordered for contrast on dark
backgrounds. Series are also separated by line dash, point shape or text labels so meaning
never depends on colour alone.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

THEME = {
    "background": "#0B1220",  # page background (config.toml backgroundColor)
    "surface": "#131D2F",  # secondary background
    "text": "#E6EDF3",  # titles and axis labels
    "muted_text": "#B6C2D1",  # legend and tick labels
    "grid": "#2A3750",  # subtle gridlines
    "axis": "#51627F",  # axis lines and ticks
    "font": "Source Sans Pro, sans-serif",
}
PALETTE = [
    "#56B4E9",  # sky blue
    "#E69F00",  # orange
    "#2FBF9A",  # bluish green
    "#CC79A7",  # reddish purple
    "#F0E442",  # yellow
    "#D55E00",  # vermillion
    "#0072B2",  # blue (darkest; keep late in the order)
    "#9AA5B8",  # grey
]
ACTUAL_COLOR = PALETTE[0]
FORECAST_COLOR = PALETTE[1]
REFERENCE_COLOR = "#C9D4E3"
STATUS_STYLE = {
    "stockout_risk": ("#E8743B", "▲"),
    "reorder": ("#E69F00", "◆"),
    "healthy": ("#2FBF9A", "●"),
    "overstock": ("#56B4E9", "■"),
    "no_demand": ("#9AA5B8", "○"),
}
MAP_POINT_COLOR = [86, 180, 233, 190]
MAP_STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"
CHART_HEIGHT = 320


def style(chart: alt.Chart | alt.LayerChart) -> alt.Chart | alt.LayerChart:
    """Dark-friendly configuration: transparent background, light text, subtle grid."""
    return (
        chart.configure(background="rgba(0,0,0,0)", font=THEME["font"])
        .configure_view(stroke=None)
        .configure_axis(
            labelColor=THEME["muted_text"], titleColor=THEME["text"], gridColor=THEME["grid"],
            domainColor=THEME["axis"], tickColor=THEME["axis"], labelFontSize=12, titleFontSize=12,
            titleFontWeight="normal",
        )
        .configure_legend(
            labelColor=THEME["muted_text"], titleColor=THEME["text"], labelFontSize=12, titleFontSize=12,
        )
        .configure_title(color=THEME["text"])
    )


def _color_scale(domain: list[str]) -> alt.Scale:
    return alt.Scale(domain=domain, range=[PALETTE[i % len(PALETTE)] for i in range(len(domain))])


def _x_encoding(frame: pd.DataFrame, column: str, title: str) -> alt.X:
    kind = "temporal" if pd.api.types.is_datetime64_any_dtype(frame[column]) else "nominal"
    return alt.X(column, type=kind, title=title)


def line_chart(
    frame: pd.DataFrame,
    x: str,
    y: str,
    x_title: str,
    y_title: str,
    series: str | None = None,
    series_title: str | None = None,
) -> alt.Chart:
    """Line chart with optional colour-and-dash separated series."""
    base = alt.Chart(frame)
    encodings: dict = {
        "x": _x_encoding(frame, x, x_title),
        "y": alt.Y(y, type="quantitative", title=y_title),
    }
    tooltip = [x, y]
    if series:
        names = [str(name) for name in pd.unique(frame[series])]
        dashes = [[1, 0], [6, 3], [2, 2], [8, 3, 2, 3]]
        encodings["color"] = alt.Color(
            series, type="nominal", title=series_title or series, scale=_color_scale(names),
            legend=alt.Legend(labelLimit=320),
        )
        encodings["strokeDash"] = alt.StrokeDash(
            series,
            type="nominal",
            title=series_title or series,
            scale=alt.Scale(domain=names, range=[dashes[i % len(dashes)] for i in range(len(names))]),
            legend=alt.Legend(labelLimit=320),
        )
        tooltip = [series, x, y]
    else:
        encodings["color"] = alt.value(ACTUAL_COLOR)
    return (
        base.mark_line(point=True, strokeWidth=2)
        .encode(tooltip=tooltip, **encodings)
        .properties(height=CHART_HEIGHT)
    )


def forecast_chart(
    actual: pd.DataFrame,
    forecast: pd.DataFrame,
    x: str,
    y: str,
    forecast_y: str,
    x_title: str,
    y_title: str,
    actual_label: str,
    forecast_label: str,
    band_low: str | None = None,
    band_high: str | None = None,
    band_label: str | None = None,
) -> alt.Chart:
    """Solid actual line, dashed forecast line with diamond markers and an optional band."""
    layers: list[alt.Chart] = []
    x_enc = _x_encoding(actual, x, x_title)
    labels = [actual_label, forecast_label]
    legend_color = alt.Color(
        "kind:N", title=None, scale=alt.Scale(domain=labels, range=[ACTUAL_COLOR, FORECAST_COLOR]),
        legend=alt.Legend(orient="top"),
    )
    legend_dash = alt.StrokeDash(
        "kind:N", title=None, scale=alt.Scale(domain=labels, range=[[1, 0], [6, 4]]),
        legend=alt.Legend(orient="top"),
    )
    if band_low and band_high and not forecast.empty:
        layers.append(
            alt.Chart(forecast)
            .mark_area(opacity=0.2, color=FORECAST_COLOR)
            .encode(
                x=_x_encoding(forecast, x, x_title),
                y=alt.Y(band_low, type="quantitative", title=y_title),
                y2=band_high,
                tooltip=[x, band_low, band_high],
            )
        )
    actual_layer = (
        alt.Chart(actual.assign(kind=actual_label))
        .mark_line(point=True, strokeWidth=2)
        .encode(
            x=x_enc, y=alt.Y(y, type="quantitative", title=y_title), color=legend_color,
            strokeDash=legend_dash, tooltip=["kind", x, y],
        )
    )
    layers.append(actual_layer)
    if not forecast.empty:
        layers.append(
            alt.Chart(forecast.assign(kind=forecast_label))
            .mark_line(point=alt.OverlayMarkDef(shape="diamond", size=70, filled=True), strokeWidth=2)
            .encode(
                x=_x_encoding(forecast, x, x_title),
                y=alt.Y(forecast_y, type="quantitative", title=y_title),
                color=legend_color, strokeDash=legend_dash, tooltip=["kind", x, forecast_y],
            )
        )
    return alt.layer(*layers).properties(height=CHART_HEIGHT)


def bar_chart(
    frame: pd.DataFrame,
    x: str,
    y: str,
    x_title: str,
    y_title: str,
    horizontal: bool = False,
    color: str | None = None,
    reference: float | None = None,
    reference_label: str | None = None,
) -> alt.Chart:
    """Single-colour bar chart (or coloured by a category) with an optional reference rule."""
    if horizontal:
        enc = {
            "y": alt.Y(x, type="nominal", sort="-x", title=x_title),
            "x": alt.X(y, type="quantitative", title=y_title),
        }
    else:
        enc = {
            "x": alt.X(x, type="nominal", sort=None, title=x_title),
            "y": alt.Y(y, type="quantitative", title=y_title),
        }
    if color:
        names = [str(name) for name in pd.unique(frame[color])]
        enc["color"] = alt.Color(color, type="nominal", scale=_color_scale(names), title=color)
    else:
        enc["color"] = alt.value(ACTUAL_COLOR)
    chart = alt.Chart(frame).mark_bar().encode(tooltip=[x, y], **enc)
    if reference is not None:
        rule = (
            alt.Chart(pd.DataFrame({"reference": [reference], "label": [reference_label or ""]}))
            .mark_rule(color=REFERENCE_COLOR, strokeDash=[5, 3], strokeWidth=2)
            .encode(**{("x" if horizontal else "y"): "reference:Q"}, tooltip=["label", "reference"])
        )
        chart = chart + rule
    return chart.properties(height=max(CHART_HEIGHT, 28 * len(frame)) if horizontal else CHART_HEIGHT)


def stock_chart(
    frame: pd.DataFrame,
    category: str,
    on_hand: str,
    reorder_point: str,
    max_stock: str,
    category_title: str,
    y_title: str,
) -> alt.Chart:
    """On-hand bars with reorder-point and max-stock tick marks per product type."""
    base = alt.Chart(frame).encode(x=alt.X(category, type="nominal", sort=None, title=category_title))
    bars = base.mark_bar(color=ACTUAL_COLOR, opacity=0.85).encode(
        y=alt.Y(on_hand, type="quantitative", title=y_title), tooltip=[category, on_hand]
    )
    marks = []
    for column, color, shape in ((reorder_point, PALETTE[5], "triangle-up"), (max_stock, PALETTE[1], "square")):
        marks.append(
            base.mark_point(color=color, shape=shape, size=140, filled=True, opacity=1).encode(
                y=alt.Y(column, type="quantitative"), tooltip=[category, column]
            )
        )
    return (bars + marks[0] + marks[1]).properties(height=CHART_HEIGHT)


def show(chart: alt.Chart | alt.LayerChart) -> None:
    st.altair_chart(style(chart), use_container_width=True, theme=None)
