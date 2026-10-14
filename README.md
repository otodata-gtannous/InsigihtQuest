# InsightQuest

InsightQuest is a Streamlit app for monitor installation analytics from `otodatawebportal_dev`.

Current scope (v1):
- Connect to SQL Server
- Load monitor installation rows
- Show clustered installations on a map with install/customer counts and top monitor types
- Show installation trend by day/week/month
- Show a seasonality-aware projection for future periods
- Show monthly seasonality profile (average installs by month)
- Filter trend/projection by customer
- Show sales peaks and lows by day/week/month using non-deleted owner purchases
- Show data diagnostics (coverage and install-date source mix)
- Compare device installation counts by territory over time

## 1) Setup

1. Create and activate a Python virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and fill the database settings.
4. Choose auth mode:
   - `IQ_DB_AUTH_MODE=sql` for SQL login (`IQ_DB_USER`/`IQ_DB_PASSWORD`)
   - `IQ_DB_AUTH_MODE=windows` for Windows integrated auth
   - `IQ_DB_AUTH_MODE=auto` to infer from config (`IQ_DB_USER` empty => windows)

## 2) Configure data source

The app uses the consolidated flat source table for all datasets:
- `IQ_SOURCE_MODE=flat-table`
- `IQ_SOURCE_TABLE=<your table>` (for example `dbo.Insight Project`)

Flat-table mode uses your configured columns, including:
- `IQ_COL_LATITUDE` (for example `DeviceLat`)
- `IQ_COL_LONGITUDE` (for example `DeviceLong`)

If your schema differs, set `IQ_MAP_SQL` and `IQ_TREND_SQL` in `.env`.

The **Sales peaks and lows** page reads from your flat table using:
- `IQ_COL_SALES_DATE`
- `IQ_COL_SALES_AMOUNT`

### Coordinate options

Use either:
- `IQ_COL_LATITUDE` and `IQ_COL_LONGITUDE` for explicit numeric columns, or
- `IQ_COL_LOCATION` containing values like `45.5017,-73.5673`

Optionally, set `IQ_MAP_SQL` with custom SQL returning aliases:
- `device_name`
- `customer_name`
- `device_location`
- `installed_at`
- `latitude`
- `longitude`

For trend/projection, you can optionally set `IQ_TREND_SQL` returning:
- `installed_at`
- `customer_name`

The Sales by territory page uses `IQ_COL_TERRITORY` from the flat table.

## 3) Run

```bash
streamlit run dashboard.py
```

## Notes on projection

The current projection uses seasonal forecasting with:
- Holt-Winters exponential smoothing (primary model)
- Seasonal naive fallback when history is too short for stable model fitting

The Trend page also shows rolling backtest quality metrics:
- MAE
- RMSE
- MAPE (when actual values are non-zero)
