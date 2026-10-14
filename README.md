# InsightQuest

InsightQuest is a Streamlit analytics app. All analysis is read from one
configured SQL Server flat table.

## Analysis pages

- **Overview** (`dashboard.py`): installation map, territory trend, projections,
  and source diagnostics where the configured columns are available.
- **Purchases & installations**: compare purchase measure and installation
  peaks/lows by day, week, or month.
- **Territory, customer & product mix**: compare installation counts by the
  configured dimensions.
- **Shipping to installation**: calculate shipment-to-installation delay for
  records with paired dates and a stable key.
- **Installation rate & forecasts**: show shipment-cohort installation rate,
  purchase/install forecasts, and market indicator context when mapped.
- **Inventory risk**: calculate stockout/overstock indicators from confirmed
  inventory and demand inputs.

In database mode, each page checks the configured flat-table schema and reports
missing mappings or columns. Set `IQ_DEMO_MODE=true` in `.env` to use the
included synthetic `demo_data.csv` instead; this mode does not connect to or
read the database. Demo values are fictional and should not be used for business
decisions. Set `IQ_DEMO_CSV` to select a different CSV file.
Use the **Export results to Excel** button on a page to download the available
tables and chart data as an `.xlsx` workbook.

## 1) Setup

1. Create and activate a Python virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and fill the database settings.
   For a local demo without SQL Server, set `IQ_DEMO_MODE=true`.
4. Choose auth mode:
   - `IQ_DB_AUTH_MODE=sql` for SQL login (`IQ_DB_USER`/`IQ_DB_PASSWORD`)
   - `IQ_DB_AUTH_MODE=windows` for Windows integrated auth
   - `IQ_DB_AUTH_MODE=auto` to infer from config (`IQ_DB_USER` empty => windows)

## 2) Configure the flat-table source

The app uses a single consolidated flat table for every dataset:
- `IQ_SOURCE_MODE=flat-table`
- `IQ_SOURCE_TABLE=<schema.table>` (for example `dbo.Insight Project`)

Copy `.env.example` to `.env` and map each `IQ_COL_*` setting to a column that
exists in this table. Column names are administrator-controlled configuration,
not values entered in the UI. Coordinate mappings are `IQ_COL_LATITUDE` and
`IQ_COL_LONGITUDE`; installation, customer, territory, purchase, shipment,
market, and inventory mappings are only needed for pages using those analyses.

The current database table inspected during implementation exposes
`Id`, `CompanyId`, `DeviceType`, `PurchaseDate`, `DeviceInitialInstallDate`,
and `TankInstallDate`. The default installation date mapping is
`TankInstallDate`; latitude and longitude are configured as `DeviceLat` and
`DeviceLong` as requested. The schema inspection did not return those coordinate
columns, so the map will remain unavailable until they are present on the
configured table. Purchase amount, shipment, customer-size, territory, market,
and inventory inputs are also not currently present, so analyses requiring
them remain unavailable until the flat table is extended and those columns are
mapped.

### Required source fields by analysis

| Analysis | Flat-table fields |
| --- | --- |
| Overview map and installation analysis | Device key/name, installation date, coordinates; customer/territory for their respective breakdowns |
| Purchase and installation peaks/lows | Purchase date, purchase measure, installation date |
| Territory/customer/product mix | Installation date, territory, customer-size segment, product type |
| Shipping-to-installation delay | Shipment date, installation date, stable shipment/device key |
| Shipment-cohort installation rate | Shipment date, installation date, eligible shipped-unit denominator |
| Forecasts and market context | Purchase date/measure, installation date; market indicator and its date for context |
| Inventory risk | Item/SKU, snapshot date, on-hand quantity, daily demand, replenishment lead days, reorder point, maximum stock |

The installation rate is installed eligible units to date divided by eligible
units shipped in each shipment-month cohort; recent cohorts may be incomplete.
The market indicator is displayed as context, not as a causal forecast input.
Inventory quantities, daily demand, and thresholds must use matching units.
Map these values only after their definitions and source grain are confirmed.

### Coordinate options

Use either:
- `IQ_COL_LATITUDE` and `IQ_COL_LONGITUDE` for explicit numeric columns, or
- `IQ_COL_LOCATION` containing values like `45.5017,-73.5673`

## 3) Run

```bash
streamlit run dashboard.py
```

## Notes on projection

Purchase and installation projections are built as separate monthly series.
Forecasting uses:
- Holt-Winters exponential smoothing (primary model)
- Seasonal naive fallback when history is too short for stable model fitting

Rolling backtests report:
- MAE
- RMSE
- MAPE (when actual values are non-zero)

Forecast results are projections, not actuals. The inventory page evaluates
projected on-hand as current on-hand minus daily demand times replenishment
lead time; it requires validated flat-table inputs and does not create a
forecast when those inputs are missing.
