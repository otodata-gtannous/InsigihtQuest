"""Analytics package: pure pandas functions, one module per business question.

``legacy`` holds the original page-level helpers; every previous public name is
re-exported so ``from insightquest.analytics import X`` keeps working.
"""

from ._common import PROXY_LABEL, TRUE_DATE_LABEL, AnalysisResult
from .legacy import (
    ForecastBacktest,
    ShippingLagSummary,
    TrendProjection,
    backtest_trend_forecast,
    build_cohort_installation_rate,
    build_installation_periods,
    build_inventory_risk,
    build_map_clusters,
    build_monthly_seasonality,
    build_purchase_install_comparison,
    build_sales_periods,
    build_segment_periods,
    build_shipping_lag,
    build_territory_trend_series,
    build_trend_series,
    project_trend,
)
from .q1_peaks import build_activity_series, compare_purchases_installs, rank_peaks_and_lows
from .q2_segments import (
    DEFAULT_SIZE_EDGES,
    add_customer_size,
    breakdown_by_group,
    build_gap_breakdowns,
    customer_size_definitions,
    default_size_labels,
)
from .q3_lag import LagAnalysis, analyze_install_lag, lag_histogram, summarize_lags
from .q4_rate import monthly_installation_rate
from .q5_markets import MarketPredictability, assess_all_markets, assess_market_predictability
from .q5_predictability import (
    MarketComparison,
    PredictabilityResult,
    assess_purchase_predictability,
    compare_with_market,
    forecast_series,
    trend_summary,
)
from .q6_inventory import (
    DEFAULT_PARAMS as DEFAULT_INVENTORY_PARAMS,
    classify_stock,
    simulate_inventory_risk,
    validate_inventory_params,
)

__all__ = [
    "AnalysisResult", "PROXY_LABEL", "TRUE_DATE_LABEL",
    "ForecastBacktest", "ShippingLagSummary", "TrendProjection",
    "backtest_trend_forecast", "build_cohort_installation_rate", "build_installation_periods",
    "build_inventory_risk", "build_map_clusters", "build_monthly_seasonality",
    "build_purchase_install_comparison", "build_sales_periods", "build_segment_periods",
    "build_shipping_lag", "build_territory_trend_series", "build_trend_series", "project_trend",
    "build_activity_series", "compare_purchases_installs", "rank_peaks_and_lows",
    "DEFAULT_SIZE_EDGES", "add_customer_size", "breakdown_by_group", "build_gap_breakdowns",
    "customer_size_definitions", "default_size_labels",
    "LagAnalysis", "analyze_install_lag", "lag_histogram", "summarize_lags",
    "monthly_installation_rate",
    "MarketPredictability", "assess_all_markets", "assess_market_predictability",
    "MarketComparison", "PredictabilityResult", "assess_purchase_predictability",
    "compare_with_market", "forecast_series", "trend_summary",
    "DEFAULT_INVENTORY_PARAMS", "classify_stock", "simulate_inventory_risk",
    "validate_inventory_params",
]
