from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from statsmodels.tsa.holtwinters import ExponentialSmoothing


def build_sales_periods(
    sales_df: pd.DataFrame,
    period: str,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    columns = ["period_start", "sales"]
    if from_date > to_date:
        raise ValueError("From date must be earlier than or equal to To date.")
    if period not in {"Day", "Week", "Month"}:
        raise ValueError("Period must be Day, Week, or Month.")

    frame = sales_df.copy()
    if frame.empty:
        frame = pd.DataFrame(columns=["purchase_date", "sales"])
    frame["purchase_date"] = pd.to_datetime(
        frame["purchase_date"], errors="coerce"
    ).dt.normalize()
    frame["sales"] = pd.to_numeric(frame["sales"], errors="coerce").fillna(0)
    frame = frame.dropna(subset=["purchase_date"])

    calendar = pd.date_range(from_date, to_date, freq="D")
    daily_sales = (
        frame.groupby("purchase_date")["sales"]
        .sum()
        .reindex(calendar, fill_value=0)
    )

    if period == "Day":
        result = daily_sales.rename_axis("period_start").reset_index()
        return result[columns]

    if period == "Week":
        period_starts = daily_sales.index - pd.to_timedelta(
            daily_sales.index.dayofweek, unit="D"
        )
        first_period = pd.Timestamp(calendar[0]) - pd.Timedelta(
            int(calendar[0].dayofweek), unit="D"
        )
        last_period = pd.Timestamp(calendar[-1]) - pd.Timedelta(
            int(calendar[-1].dayofweek), unit="D"
        )
        all_periods = pd.date_range(first_period, last_period, freq="7D")
    else:
        period_starts = daily_sales.index.to_period("M").to_timestamp()
        first_period = calendar[0].to_period("M").to_timestamp()
        last_period = calendar[-1].to_period("M").to_timestamp()
        all_periods = pd.date_range(first_period, last_period, freq="MS")

    period_sales = daily_sales.groupby(period_starts).sum().reindex(
        all_periods, fill_value=0
    )
    return period_sales.rename_axis("period_start").reset_index()[columns]


@dataclass(frozen=True)
class TrendProjection:
    history: pd.DataFrame
    projection: pd.DataFrame


@dataclass(frozen=True)
class ForecastBacktest:
    model_name: str
    folds: int
    mae: float | None
    rmse: float | None
    mape: float | None
    details: pd.DataFrame


@dataclass(frozen=True)
class ShippingLagSummary:
    matched_rows: int
    missing_date_rows: int
    negative_lag_rows: int
    median_days: float | None
    percentile_90_days: float | None


def build_installation_periods(
    installs_df: pd.DataFrame,
    period: str,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    if installs_df.empty:
        frame = pd.DataFrame(columns=["purchase_date", "sales"])
    else:
        frame = installs_df[["installed_at"]].rename(
            columns={"installed_at": "purchase_date"}
        ).copy()
        frame["sales"] = 1
    return build_sales_periods(frame, period, from_date, to_date).rename(
        columns={"sales": "installs"}
    )


def build_purchase_install_comparison(
    purchases_df: pd.DataFrame,
    installs_df: pd.DataFrame,
    period: str,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    purchases = build_sales_periods(
        purchases_df, period, from_date, to_date
    ).rename(columns={"sales": "purchases"})
    installs = build_installation_periods(
        installs_df, period, from_date, to_date
    )
    return purchases.merge(installs, on="period_start", how="outer").fillna(
        {"purchases": 0, "installs": 0}
    )


def build_segment_periods(
    data: pd.DataFrame,
    date_column: str,
    segment_column: str,
    period: str,
) -> pd.DataFrame:
    if period not in {"Day", "Week", "Month"}:
        raise ValueError("Period must be Day, Week, or Month.")
    columns = ["period_start", "segment", "count"]
    if data.empty:
        return pd.DataFrame(columns=columns)

    frame = data[[date_column, segment_column]].copy()
    frame[date_column] = pd.to_datetime(frame[date_column], errors="coerce")
    frame = frame.dropna(subset=[date_column])
    frame["segment"] = frame[segment_column].fillna("Unknown").astype(str).str.strip()
    frame.loc[frame["segment"] == "", "segment"] = "Unknown"
    dates = frame[date_column].dt.normalize()
    if period == "Day":
        frame["period_start"] = dates
    elif period == "Week":
        frame["period_start"] = dates - pd.to_timedelta(
            dates.dt.dayofweek, unit="D"
        )
    else:
        frame["period_start"] = dates.dt.to_period("M").dt.to_timestamp()

    return (
        frame.groupby(["period_start", "segment"])
        .size()
        .rename("count")
        .reset_index()[columns]
        .sort_values(["period_start", "segment"])
        .reset_index(drop=True)
    )


def build_shipping_lag(data: pd.DataFrame) -> tuple[pd.DataFrame, ShippingLagSummary]:
    columns = ["shipping_date", "installed_at", "lag_days"]
    if data.empty:
        return pd.DataFrame(columns=columns), ShippingLagSummary(0, 0, 0, None, None)

    frame = data[["shipping_date", "installed_at"]].copy()
    frame["shipping_date"] = pd.to_datetime(frame["shipping_date"], errors="coerce")
    frame["installed_at"] = pd.to_datetime(frame["installed_at"], errors="coerce")
    missing_dates = frame["shipping_date"].isna() | frame["installed_at"].isna()
    complete = frame.loc[~missing_dates].copy()
    complete["lag_days"] = (
        complete["installed_at"] - complete["shipping_date"]
    ).dt.total_seconds() / 86_400
    negative_lag = complete["lag_days"] < 0
    valid = complete.loc[~negative_lag, columns].reset_index(drop=True)
    summary = ShippingLagSummary(
        matched_rows=len(valid),
        missing_date_rows=int(missing_dates.sum()),
        negative_lag_rows=int(negative_lag.sum()),
        median_days=float(valid["lag_days"].median()) if not valid.empty else None,
        percentile_90_days=(
            float(valid["lag_days"].quantile(0.9)) if not valid.empty else None
        ),
    )
    return valid, summary


def build_cohort_installation_rate(
    data: pd.DataFrame,
    denominator_column: str,
) -> pd.DataFrame:
    columns = [
        "cohort_month",
        "shipped_units",
        "installed_units",
        "installation_rate_pct",
    ]
    if data.empty:
        return pd.DataFrame(columns=columns)

    frame = data[["shipping_date", "installed_at", denominator_column]].copy()
    frame["shipping_date"] = pd.to_datetime(frame["shipping_date"], errors="coerce")
    frame["installed_at"] = pd.to_datetime(frame["installed_at"], errors="coerce")
    frame[denominator_column] = pd.to_numeric(
        frame[denominator_column], errors="coerce"
    )
    frame = frame.dropna(subset=["shipping_date", denominator_column])
    frame = frame[frame[denominator_column] > 0].copy()
    frame["cohort_month"] = frame["shipping_date"].dt.to_period("M").dt.to_timestamp()
    installed = frame["installed_at"].notna() & (
        frame["installed_at"] >= frame["shipping_date"]
    )
    frame["shipped_units"] = frame[denominator_column]
    frame["installed_units"] = np.where(installed, frame[denominator_column], 0)
    result = (
        frame.groupby("cohort_month")[["shipped_units", "installed_units"]]
        .sum()
        .reset_index()
    )
    result["installation_rate_pct"] = (
        result["installed_units"] / result["shipped_units"] * 100
    ).round(2)
    return result[columns].sort_values("cohort_month").reset_index(drop=True)


def build_inventory_risk(data: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "item",
        "on_hand",
        "daily_demand",
        "lead_time_days",
        "coverage_days",
        "projected_on_hand",
        "reorder_point",
        "max_stock",
        "risk",
    ]
    if data.empty:
        return pd.DataFrame(columns=columns)

    frame = data.rename(
        columns={
            "item_name": "item",
            "inventory_on_hand": "on_hand",
            "inventory_daily_demand": "daily_demand",
            "replenishment_days": "lead_time_days",
        }
    ).copy()
    numeric_columns = [
        "on_hand",
        "daily_demand",
        "lead_time_days",
        "reorder_point",
        "max_stock",
    ]
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=numeric_columns)
    frame = frame[
        (frame["on_hand"] >= 0)
        & (frame["daily_demand"] >= 0)
        & (frame["lead_time_days"] >= 0)
        & (frame["reorder_point"] >= 0)
        & (frame["max_stock"] >= frame["reorder_point"])
    ].copy()
    frame["coverage_days"] = np.where(
        frame["daily_demand"] > 0,
        frame["on_hand"] / frame["daily_demand"],
        np.inf,
    )
    frame["projected_on_hand"] = (
        frame["on_hand"] - frame["daily_demand"] * frame["lead_time_days"]
    )
    frame["risk"] = np.select(
        [
            frame["projected_on_hand"] < frame["reorder_point"],
            frame["on_hand"] > frame["max_stock"],
        ],
        ["Stockout risk", "Overstock risk"],
        default="Within thresholds",
    )
    return frame[columns].sort_values(["risk", "item"]).reset_index(drop=True)


def build_trend_series(
    installs_df: pd.DataFrame,
    period: str,
    customer_filter: list[str] | None = None,
) -> pd.DataFrame:
    if installs_df.empty:
        return pd.DataFrame(columns=["period_start", "installs"])

    frame = installs_df.copy()
    frame["installed_at"] = pd.to_datetime(frame["installed_at"], errors="coerce")
    frame = frame.dropna(subset=["installed_at"])
    if "customer_name" not in frame.columns:
        frame["customer_name"] = "Unknown"

    if customer_filter:
        frame = frame[frame["customer_name"].isin(customer_filter)].copy()
        if frame.empty:
            return pd.DataFrame(columns=["period_start", "installs"])

    if period == "Day":
        freq = "D"
    elif period == "Week":
        freq = "W-MON"
    else:
        freq = "MS"

    grouped = (
        frame.groupby(pd.Grouper(key="installed_at", freq=freq))
        .size()
        .rename("installs")
        .reset_index()
    )
    grouped = grouped.rename(columns={"installed_at": "period_start"})
    return grouped


def build_monthly_seasonality(
    installs_df: pd.DataFrame,
    customer_filter: list[str] | None = None,
) -> pd.DataFrame:
    columns = ["month_number", "month_name", "avg_installs", "total_installs"]
    if installs_df.empty:
        return pd.DataFrame(columns=columns)

    frame = installs_df.copy()
    frame["installed_at"] = pd.to_datetime(frame["installed_at"], errors="coerce")
    frame = frame.dropna(subset=["installed_at"])
    if "customer_name" not in frame.columns:
        frame["customer_name"] = "Unknown"

    if customer_filter:
        frame = frame[frame["customer_name"].isin(customer_filter)].copy()
        if frame.empty:
            return pd.DataFrame(columns=columns)

    frame["year"] = frame["installed_at"].dt.year
    frame["month_number"] = frame["installed_at"].dt.month
    monthly_counts = (
        frame.groupby(["year", "month_number"])
        .size()
        .rename("installs")
        .reset_index()
    )
    summary = (
        monthly_counts.groupby("month_number")
        .agg(
            avg_installs=("installs", "mean"),
            total_installs=("installs", "sum"),
        )
        .reset_index()
        .sort_values("month_number")
    )
    summary["month_name"] = pd.to_datetime(summary["month_number"], format="%m").dt.strftime("%b")
    summary["avg_installs"] = summary["avg_installs"].round(2)
    return summary[columns]


def build_territory_trend_series(
    installs_df: pd.DataFrame,
    period: str,
    territory_filter: list[str] | None = None,
) -> pd.DataFrame:
    columns = ["period_start", "territory_name", "installs"]
    if installs_df.empty:
        return pd.DataFrame(columns=columns)

    frame = installs_df.copy()
    frame["installed_at"] = pd.to_datetime(frame["installed_at"], errors="coerce")
    frame = frame.dropna(subset=["installed_at"])
    if "territory_name" not in frame.columns:
        frame["territory_name"] = "Unknown Territory"
    frame["territory_name"] = frame["territory_name"].fillna("Unknown Territory").astype(str)

    if territory_filter is not None:
        frame = frame[frame["territory_name"].isin(territory_filter)].copy()
        if frame.empty:
            return pd.DataFrame(columns=columns)

    if period == "Day":
        freq = "D"
    elif period == "Week":
        freq = "W-MON"
    else:
        freq = "MS"

    grouped = (
        frame.groupby(["territory_name", pd.Grouper(key="installed_at", freq=freq)])
        .size()
        .rename("installs")
        .reset_index()
        .rename(columns={"installed_at": "period_start"})
    )
    periods = pd.date_range(
        start=grouped["period_start"].min(),
        end=grouped["period_start"].max(),
        freq=freq,
    )
    territories = sorted(grouped["territory_name"].unique().tolist())
    complete_index = pd.MultiIndex.from_product(
        [periods, territories],
        names=["period_start", "territory_name"],
    )
    complete = complete_index.to_frame(index=False)
    return (
        complete.merge(grouped, on=["period_start", "territory_name"], how="left")
        .fillna({"installs": 0})
        .astype({"installs": int})
        .sort_values(["period_start", "territory_name"])
        .reset_index(drop=True)
    )


def build_map_clusters(map_df: pd.DataFrame, rounding_decimals: int) -> pd.DataFrame:
    if map_df.empty:
        return pd.DataFrame()

    frame = map_df.copy()
    frame["cluster_lat"] = frame["latitude"].round(rounding_decimals)
    frame["cluster_lon"] = frame["longitude"].round(rounding_decimals)
    if "customer_name" not in frame.columns:
        frame["customer_name"] = "Unknown"

    grouped = (
        frame.groupby(["cluster_lat", "cluster_lon"], dropna=False)
        .agg(
            installs=("device_name", "size"),
            customers=("customer_name", "nunique"),
            latest_install=("installed_at", "max"),
        )
        .reset_index()
        .rename(columns={"cluster_lat": "lat", "cluster_lon": "lon"})
    )

    top_devices = (
        frame.groupby(["cluster_lat", "cluster_lon", "device_name"])
        .size()
        .rename("count")
        .reset_index()
        .sort_values(["cluster_lat", "cluster_lon", "count"], ascending=[True, True, False])
    )
    top_devices = (
        top_devices.groupby(["cluster_lat", "cluster_lon"])
        .head(3)
        .groupby(["cluster_lat", "cluster_lon"])["device_name"]
        .apply(lambda values: ", ".join(values.astype(str).tolist()))
        .rename("top_devices")
        .reset_index()
    )
    top_devices = top_devices.rename(columns={"cluster_lat": "lat", "cluster_lon": "lon"})

    clustered = grouped.merge(top_devices, on=["lat", "lon"], how="left")
    clustered["top_devices"] = clustered["top_devices"].fillna("Unknown")
    clustered["radius"] = np.sqrt(clustered["installs"]).clip(lower=1) * 250
    return clustered.sort_values("installs", ascending=False)


def _frequency_for_period(period: str) -> str:
    return {"Day": "D", "Week": "W-MON", "Month": "MS"}.get(period, "D")


def _season_length_for_period(period: str) -> int:
    return {"Day": 7, "Week": 4, "Month": 12}.get(period, 7)


def _normalize_history(history_df: pd.DataFrame, period: str) -> pd.DataFrame:
    history = history_df.copy()
    history["period_start"] = pd.to_datetime(history["period_start"], errors="coerce")
    history["installs"] = pd.to_numeric(history["installs"], errors="coerce").fillna(0)
    history = history.dropna(subset=["period_start"]).sort_values("period_start")
    if history.empty:
        return pd.DataFrame(columns=["period_start", "installs"])

    freq = _frequency_for_period(period)
    complete_index = pd.date_range(
        start=history["period_start"].min(),
        end=history["period_start"].max(),
        freq=freq,
    )
    merged = (
        pd.DataFrame({"period_start": complete_index})
        .merge(history[["period_start", "installs"]], on="period_start", how="left")
        .fillna({"installs": 0})
    )
    merged["installs"] = merged["installs"].astype(int)
    return merged


def _seasonal_naive_forecast(
    values: np.ndarray,
    horizon: int,
    seasonal_periods: int,
) -> np.ndarray:
    if len(values) == 0 or horizon <= 0:
        return np.array([], dtype=float)
    if len(values) < seasonal_periods:
        return np.repeat(values[-1], horizon).astype(float)

    pattern = values[-seasonal_periods:]
    repeats = int(np.ceil(horizon / seasonal_periods))
    forecast = np.tile(pattern, repeats)[:horizon]
    return forecast.astype(float)


def _holt_winters_forecast(
    values: np.ndarray,
    horizon: int,
    seasonal_periods: int,
) -> np.ndarray:
    model = ExponentialSmoothing(
        values,
        trend="add",
        seasonal="add",
        seasonal_periods=seasonal_periods,
        initialization_method="estimated",
    )
    fit = model.fit(optimized=True, remove_bias=True)
    forecast = fit.forecast(horizon)
    return np.asarray(forecast, dtype=float)


def _forecast_values(values: np.ndarray, horizon: int, period: str) -> tuple[np.ndarray, str]:
    seasonal_periods = _season_length_for_period(period)
    min_points_for_hw = max(8, seasonal_periods * 2)

    if len(values) >= min_points_for_hw:
        try:
            forecast = _holt_winters_forecast(values, horizon, seasonal_periods)
            return np.maximum(0, forecast), "Holt-Winters"
        except (ValueError, np.linalg.LinAlgError):
            pass

    fallback = _seasonal_naive_forecast(values, horizon, seasonal_periods)
    return np.maximum(0, fallback), "Seasonal Naive"


def backtest_trend_forecast(history_df: pd.DataFrame, period: str) -> ForecastBacktest:
    history = _normalize_history(history_df, period)
    if history.empty:
        return ForecastBacktest(
            model_name="N/A",
            folds=0,
            mae=None,
            rmse=None,
            mape=None,
            details=pd.DataFrame(columns=["period_start", "actual", "predicted", "method"]),
        )

    y = history["installs"].astype(float).to_numpy()
    seasonal_periods = _season_length_for_period(period)
    min_train_points = max(8, seasonal_periods * 2)
    if len(y) <= min_train_points:
        return ForecastBacktest(
            model_name="N/A",
            folds=0,
            mae=None,
            rmse=None,
            mape=None,
            details=pd.DataFrame(columns=["period_start", "actual", "predicted", "method"]),
        )

    rows: list[dict[str, object]] = []
    methods_used: set[str] = set()
    for index in range(min_train_points, len(y)):
        train_values = y[:index]
        prediction_arr, method = _forecast_values(train_values, horizon=1, period=period)
        prediction = float(prediction_arr[0])
        actual = float(y[index])
        methods_used.add(method)
        rows.append(
            {
                "period_start": history["period_start"].iloc[index],
                "actual": int(round(actual)),
                "predicted": int(round(prediction)),
                "method": method,
            }
        )

    details = pd.DataFrame(rows)
    if details.empty:
        return ForecastBacktest(
            model_name="N/A",
            folds=0,
            mae=None,
            rmse=None,
            mape=None,
            details=details,
        )

    errors = details["predicted"].astype(float) - details["actual"].astype(float)
    mae = float(np.mean(np.abs(errors)))
    rmse = float(np.sqrt(np.mean(np.square(errors))))
    nonzero_mask = details["actual"].astype(float) != 0
    if nonzero_mask.any():
        mape = float(
            (
                np.abs(errors[nonzero_mask]) / details.loc[nonzero_mask, "actual"].astype(float)
            ).mean()
            * 100
        )
    else:
        mape = None

    model_name = " + ".join(sorted(methods_used))
    return ForecastBacktest(
        model_name=model_name,
        folds=len(details),
        mae=mae,
        rmse=rmse,
        mape=mape,
        details=details,
    )


def project_trend(history_df: pd.DataFrame, periods_ahead: int, period: str) -> TrendProjection:
    history = _normalize_history(history_df, period)
    if history.empty or len(history) < 2 or periods_ahead <= 0:
        return TrendProjection(history=history, projection=pd.DataFrame())

    y = history["installs"].astype(float).to_numpy()
    forecast_values, _ = _forecast_values(y, periods_ahead, period)
    freq = _frequency_for_period(period)
    future_dates = pd.date_range(
        start=history["period_start"].iloc[-1],
        periods=periods_ahead + 1,
        freq=freq,
    )[1:]
    projection = pd.DataFrame(
        {
            "period_start": future_dates,
            "installs": np.rint(np.maximum(0, forecast_values)).astype(int),
        }
    )
    return TrendProjection(history=history, projection=projection)
