# InsightQuest

InsightQuest is a Streamlit analytics app. All analysis is read from one
configured SQL Server flat table.

## The six questions InsightQuest answers

The app exists to help classify the **installation capacity of our customers**.
It is built around six analysis questions (originally posed in French):

| # | Question (FR) | Question (EN) | Page |
| --- | --- | --- | --- |
| 1 | Pics et creux des achats et installations | Peaks and lows of purchases and installations | Peaks & Lows / Hauts et bas |
| 2 | Écarts par territoire et taille des clients et type de produit | Gaps by territory, customer size, and product type | Market Gaps / Écarts de marché |
| 3 | Délai entre expédition et installation | Delay between shipping and installation | Ship-to-Install Time / Délai expédition-installation |
| 4 | Taux mensuel d'installation | Monthly installation rate | Install Rate / Taux d'installation |
| 5 | Prévisibilité des achats versus les marchés | Purchase predictability versus markets | Buying Outlook / Perspectives d'achats |
| 6 | Seuils critiques pour risques de rupture ou de surstock | Critical thresholds for stockout or overstock risk | Stock Watch / Surveillance des stocks |

See [Question coverage and data readiness](#question-coverage-and-data-readiness)
for what each question needs from the flat table, and
[Design decisions](#design-decisions) for architecture notes.

## Analysis pages

The app opens on a **Home** guide with one card per question (with a
Ready / Partial / Not available badge based on your column mappings). Pages are
grouped in the sidebar; use the language selector for English or French (fr-CA).
The app uses a dark theme by default (`.streamlit/config.toml`). Page URLs are
readable: `/peaks-and-lows`, `/market-gaps`, `/ship-to-install`, `/install-rate`,
`/buying-outlook`, `/stock-watch`, `/install-map`, and `/ask`.

- **Home**: the six questions with a live one-sentence answer each, headline KPIs, and links to each page.
- **Peaks & Lows** (Question 1): purchases and installations by day, week, or month
  (purchases count rows when no amount column is mapped), ranked peaks/lows and
  the cumulative purchase-vs-install gap.
- **Market Gaps** (Question 2): installed rate and gap
  versus the overall rate per group. Customer size is derived from the number of
  devices owned per company (Small < 10, Medium 10-99, Large 100+; adjustable
  with `IQ_CUSTOMER_SIZE_EDGES`).
- **Ship-to-Install Time** (Question 3): median, 90th percentile, histogram,
  and cohort trend. The date filter applies to the shipment date.
- **Install Rate** (Question 4): cohort and population rates with their
  denominators shown, plus a forecast with an uncertainty band.
- **Buying Outlook** (Question 5): markets are your own territories and
  product types, scored High/Medium/Low from purchase history (trend, forecast
  backtest error, seasonality), with share change versus a year ago and each
  market's contribution to volatility. Markets with under 12 months of history
  are listed as "Insufficient history". No external data is needed.
- **Stock Watch** (Question 6): stockout/overstock status per device type from
  adjustable simulation inputs (lead time, safety days, max cover, demand
  window).
- **Install Map**: the installation map, territory trend, and
  source diagnostics.
- **Ask the data** (chat): a chat widget at the bottom right of every page, also
  available full-screen at `/ask`. It needs no setup: without an OpenAI key,
  questions are matched locally to the supported analyses and each page offers
  suggestion chips; answers show the number first, a one-line scope note, a small
  table, and the plan under "Show how this was computed". Answers use the shared
  date range. Optionally set
  `IQ_OPENAI_API_KEY` (or `OPENAI_API_KEY`) and `IQ_OPENAI_MODEL`
  (default `gpt-4o-mini`, with structured-output support) in `.env` to improve
  question interpretation.
  In the default **Private** mode, the server extracts fixed analysis labels from the question locally. Only
  those labels, static instructions, and a fixed response schema go to OpenAI.
  Questions, history, source schema, records, sample rows, IDs, coordinates,
  aggregates, and results are not sent. Labels still disclose the general
  analysis topic; use an internal model if even that must remain confidential.
  The model has no database tools and cannot supply SQL. Its response is
  validated against a fixed allowlist before anything runs.
  Calculations and answer formatting run locally, with no second LLM call.
  Missing keys, ambiguous questions, and planner failures fall back to the
  closest supported question; failures are shown rather than presented as model answers.
  Available analyses include overview, monthly purchases/installations, counts
  by company/type, installation delay, pending installations, and coordinate
  coverage. Each question is independent: arbitrary filters in question text,
  top-N limits, and follow-up references are not applied.
  Results identify their scope and count rows unless labeled distinct.
  Rows/results are not globally cached
  by the chat; displayed results and history are held in the user's Streamlit
  session and are lost when the page is reloaded.
  This does not add authentication or tenant authorization: use only within
  an authorized deployment and restrict the database account/source accordingly.
  Do not enable request-body logging or external tracing of questions/results
  in deployment infrastructure.

### Guided aggregate queries (opt-in)

Set `IQ_AI_ALLOW_EXTERNAL_DATA=true` and restart Streamlit. The chat then shows
two opt-in toggles, both off by default: **Let the AI interpret my question**
(sends the question text to OpenAI) and **Let the AI explain results** (sends
aggregated numbers to OpenAI). Keep this disabled
unless your organization permits sharing questions and aggregates with OpenAI.
For database mode, configure the `IQ_DB_*` credentials as a SELECT-only account
restricted to the approved source table/view, then set
`IQ_AI_DB_READ_ONLY_CONFIRMED=true`. This is an administrator attestation, not
permission enforcement. Do not use `sa` or another write-capable account.
Synthetic demo mode does not need database credentials or this attestation.

1. With interpretation enabled, review your question, remove confidential
   values/identifiers, and send it to OpenAI. The planner also receives the
   current date, static instructions, and a fixed schema, but no source schema,
   source records, sample rows, results, or history.
2. Review the returned plan ("The AI read your question as...") and run it
   locally with **Run this analysis**. The server
   independently validates every plan field. No model-generated SQL or code
   is accepted; only fixed operations and administrator-mapped fields are used.
3. Inspect the local result and the exact outbound aggregate payload ("What would
   be sent to OpenAI"). With explanation enabled, **Explain with AI** sends that
   payload. The question is not resent. Results remain usable without this call.

Supported metrics are purchase/installation **row counts**, purchased rows
without a valid installation date, and nonnegative purchase-to-install delay
(mean/median days and contributing row count). Group by day/week/month, device
type, or no dimension; apply one inclusive purchase/install date range; return
1-50 groups. Period groups use purchase dates for purchases/pending and install
dates for installations/delays, and sort chronologically. Type groups sort by
count (delay: median days), with ascending/descending order and deterministic
label tie-breaking. Invalid/missing dates and negative delays are excluded from
the applicable metric and reported locally. All counts are source rows, not
distinct devices, customers, or eligible shipped units.

For example: "How many installations were there by month in 2026?" or
"Show the five device types with the longest median purchase-to-install delay."
Company/device filters, category-value filters, raw-record requests, coordinates,
revenue, forecasts, distinct counts, multiple metrics, rankings of periods by
value, and conversational follow-ups are unsupported. Review the plan for
semantic accuracy; the model can still misinterpret a question. Rephrase or use
Private mode rather than assuming unsupported conditions were applied.

Queries are computed inside the database as parameterized, bracket-validated
SELECT aggregates (`GROUP BY` counts; delay mean/median/min/max via `AVG`,
`PERCENTILE_CONT` over millisecond differences), so no source rows are loaded and
there is no source-row cap: only the grouped result (at most 100,000 groups, then
the 1-50 shown) reaches the app. Source-row counts in the scope notes come from
SQL `COUNT`. The database query timeout (`IQ_DB_QUERY_TIMEOUT_SECONDS`) bounds
each query; external calls use a 30-second timeout, no automatic
retries, and bounded output tokens. Results show when top-N omits groups.

External explanations replace device-type labels with temporary `Type group N`
pseudonyms in displayed row order; the mapping stays local. Only aggregate
counts/mean/median, period labels, normalized query scope, and an omission flag
are sent. Source IDs, coordinates, type labels, raw rows, source-row totals, and
excluded-row counts are not sent. If **any** contributing group has fewer than
10 rows, the entire explanation is blocked, including when that group lies
outside top-N. Empty results cannot be shared. Local results remain visible.

This is a conservative disclosure check, **not** a guarantee of anonymity or
differential privacy: rows may describe the same entity, repeated overlapping
queries may enable inference, and business aggregates may themselves be
confidential. There is no query-history privacy budget. Restrict access to
authorized users; this feature adds no authentication or tenant isolation.
Do not enable external tracing or request-body logging of questions/results.
AI explanations are labeled as generated text and should be checked against
the authoritative local result. No live provider call is needed by the tests.

In database mode, each page checks the configured flat-table schema and reports
missing mappings or columns. Set `IQ_DEMO_MODE=true` in `.env` to use the
included synthetic `demo_data.csv` instead; this mode does not connect to or
read the database. Demo values are fictional and should not be used for business
decisions. Set `IQ_DEMO_CSV` to select a different CSV file.
Use the **Export results to Excel** button on a page to download the available
tables and chart data as an `.xlsx` workbook.

## Question coverage and data readiness

Every question is answered from the single flat table (one row per device).
Pages never invent missing data: when a required column is not mapped or does
not exist, the page shows a readiness message instead of a result.

| # | Status | How it is answered | Caveat |
| --- | --- | --- | --- |
| 1 | Ready | Purchase and installation counts per period (rows are counted when no amount is mapped) | `PurchaseDate` is a warranty-start proxy |
| 2 | Ready | Territory comes from device → ownership → company → territory (`IQ_COL_TERRITORY=Territory`); customer size is derived from devices per company | Size buckets are a first definition and subject to review |
| 3 | Ready (proxy) | Lag from `PurchaseDate` (used as the shipment date, `IQ_COL_SHIPPED_AT`) to installation | A true shipment date would replace the proxy; the UI labels it as a proxy |
| 4 | Ready (proxy) | Cohort rate (installed / purchased in the cohort month) and population rate | Same proxy; recent cohorts are still maturing |
| 5 | Ready | Per-territory and per-product-type trend, forecast and backtest error from purchase history | Needs 12+ months per market; small markets are flagged |
| 6 | Ready (simulated) | On hand = devices without an install date, per device type; demand from trailing installs; thresholds from the lead-time, safety, and max-cover inputs on the page | On hand overstates real warehouse stock; thresholds are simulation inputs, not business-confirmed values |

## How each question is answered

1. **Purchase/install peaks and lows:** daily, weekly, or monthly counts ranked by period, with the cumulative purchase-vs-install gap.
2. **Territory, customer size, and product differences:** per group, installed rate and gap versus overall, distinct-company counts, and small-group flags. Customer size buckets devices-per-company (default Small < 10, Medium 10-99, Large 100+); devices with no company are "Unassigned".
3. **Shipping-to-installation delay:** whole-day lag with mean, median, and 90th percentile, a histogram, and a cohort trend. Missing and negative lags are counted separately. The date filter applies to the shipment date.
4. **Monthly installation rate:** cohort rate (devices installed from a month's purchase cohort / devices in that cohort) and rate versus the purchased-to-date population; denominators are shown on the page.
5. **Purchase predictability by market:** markets are territories and product types. Each gets a trend, Holt-Winters forecast (seasonal-naive fallback on short history), rolling-backtest errors (MAE, RMSE, MAPE, MASE), and a High/Medium/Low score. Descriptive, not causal. An optional external market series is supported in code (`IQ_MARKET_CSV`) but off by default and not required.
6. **Stockout/overstock thresholds:** per device type, on hand versus a reorder point (demand × lead time) and a maximum (demand × max cover days); statuses are Stockout risk, Reorder, Healthy, Overstock, and No demand, with a projected stockout date.

**AI used:** forecasting uses statistical time-series methods; the other analytical
pages use aggregations or explicit business rules. **Ask the data** optionally
uses an LLM for planning and, in guided mode, explaining approved aggregates;
all data calculations run locally.

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

For an installation-selected Azure sample, set `IQ_SAMPLE_INSTALLATIONS` to its
size and `IQ_PURCHASE_DATE_IS_PROXY=true` when purchase dates are warranty-start
proxies. The dashboard displays scope/interpretation warnings. Such samples
exclude older/uninstalled devices and must not be treated as all-time totals or
unbiased forecasting data. See the [Azure refresh instructions](extractor/README.md).

The app uses a single consolidated flat table for every dataset:
- `IQ_SOURCE_MODE=flat-table`
- `IQ_SOURCE_TABLE=<schema.table>` (for example `dbo.Insight Project`)

Copy `.env.example` to `.env` and map each `IQ_COL_*` setting to a column that
exists in this table. Column names are administrator-controlled configuration,
not values entered in the UI. Coordinate mappings are `IQ_COL_LATITUDE` and
`IQ_COL_LONGITUDE`; installation, customer, territory, purchase, shipment,
market, and inventory mappings are only needed for pages using those analyses.

Normal and demo defaults use these columns:

| Setting | Column | Meaning |
| --- | --- | --- |
| `IQ_COL_DEVICE` | `Id` | DeviceId / device identifier |
| `IQ_COL_SHIPPING_KEY` | `Id` | Stable device key |
| `IQ_COL_SALES_DATE` | `PurchaseDate` | Purchase date (warranty-start proxy) |
| `IQ_COL_SHIPPED_AT` | `PurchaseDate` | Shipment date; the same proxy until a real source exists (`IQ_SHIPPED_AT_IS_PROXY=true` labels it in the UI) |
| `IQ_COL_PRODUCT_TYPE` | `DeviceType` | Product/device type |
| `IQ_COL_INSTALLED_AT` | `TankInstallDate` | Preferred installation date; `DeviceInitialInstallDate` is the alternative |
| `IQ_COL_CUSTOMER` | `CompanyId` | Customer/company (the device's owner) |
| `IQ_COL_TERRITORY` | `Territory` | Territory of the owning company (extractor-built tables) |
| `IQ_COL_LATITUDE` | `Latitude` | Numeric latitude |
| `IQ_COL_LONGITUDE` | `Longitude` | Numeric longitude |

Derived without any extra columns:

- **Customer size:** devices per `CompanyId`, bucketed with
  `IQ_CUSTOMER_SIZE_EDGES` (default `10,100` → Small < 10, Medium 10-99,
  Large 100+).
- **Inventory on hand:** devices with no install date, by device type.

These `IQ_COL_*` mappings are optional and blank by default:

- `IQ_COL_LOCATION`: a combined location field; the map uses the separate
  latitude/longitude mappings instead.
- `IQ_COL_SALES_AMOUNT`: when mapped, Q1 and Q5 use purchase amounts instead of
  row counts.
- `IQ_COL_CUSTOMER_SIZE`, `IQ_COL_RATE_DENOMINATOR`, `IQ_COL_MARKET_INDICATOR`,
  `IQ_COL_MARKET_INDICATOR_AT`, and the `IQ_COL_INVENTORY_*`,
  `IQ_COL_REPLENISHMENT_DAYS`, `IQ_COL_REORDER_POINT`, `IQ_COL_MAX_STOCK`
  settings are reserved for future sources and are not required by the pages.

A local `.env` that maps columns missing from the demo CSV (for example
`IQ_COL_INSTALLED_AT=AnalysisInstalledAt`) overrides the demo defaults; in demo
mode the app shows a banner naming the variables to blank.

### Other settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `IQ_CACHE_TTL_SECONDS` | `300` | How long query results are cached; use **Refresh data** to clear |
| `IQ_DB_QUERY_TIMEOUT_SECONDS` | `60` | Timeout for dashboard queries |
| `IQ_DB_POOL_SIZE`, `IQ_DB_POOL_TIMEOUT_SECONDS` | `5`, `30` | Connection pool |
| `IQ_MARKET_CSV` | empty | Optional external `month,value` series; off by default |

### Required source fields by analysis

| Analysis | Flat-table fields |
| --- | --- |
| Install map and diagnostics | Device key, installation date, coordinates |
| Q1 Peaks and lows | Purchase date, installation date (optional: purchase amount) |
| Q2 Gaps | Installation date, territory, customer, product type |
| Q3 Delay / Q4 Rate | Shipment (proxy) date, installation date |
| Q5 Predictability | Purchase date, territory, product type |
| Q6 Stock thresholds | Product type, installation date |

The installation rate is installed devices divided by devices in each
purchase-month cohort; recent cohorts may be incomplete.
Inventory thresholds are simulation inputs (lead time, safety days, max cover,
demand window) set on the Q6 page, not values read from the data.

### Coordinate options

Use either:
- `IQ_COL_LATITUDE` and `IQ_COL_LONGITUDE` for explicit numeric columns, or
- `IQ_COL_LOCATION` containing values like `45.5017,-73.5673`

## 3) Run

```bash
streamlit run dashboard.py
```

## Docker deployment

Create a local `.env` from `.env.example` and configure the data source. Start
the app from the repository root:

```bash
docker compose up --build -d
```

Open `http://localhost:8501`. The image is built from the current code, so run
`docker compose up --build -d` again after pulling changes; a running container
keeps serving the old build. To stop the app:

```bash
docker compose down
```

For demo mode, set `IQ_DEMO_MODE=true` in `.env`. For database mode, configure
a SQL login and set `IQ_DB_SERVER` to a host reachable from the container (not
`localhost` unless SQL Server is in the same container). The image includes
Microsoft ODBC Driver 18. Do not copy secrets into the image or commit `.env`;
provide production secrets through the deployment platform's environment or
secret-management settings.

## Design decisions

### Is a star schema needed?

**No, not at present.** The flat table is one row per device, so all six
questions are answered from it with exact device counts. Territory comes from
device → ownership → company → territory, customer size is derived from devices
per company, and inventory is the set of devices without an install date. Keep
`dbo.[Insight Project]` as the app's read source.

Revisit a star schema (or small side tables) only if you add data at a
different grain: a real shipment date or orders (shipment ≠ warranty start),
real warehouse stock snapshots (item × date), or an external market series
(indicator × month). If you do, expose them through views and point
`IQ_SOURCE_TABLE` / `IQ_COL_*` at the view columns. Customer-level attributes
must be counted with `COUNT(DISTINCT CompanyId)`, never summed across device
rows.

### Backend data access

Direct SQL Server access from Streamlit stays (internal tool). Implemented:

- Aggregation runs in SQL (`GROUP BY`) with parameterized queries and validated,
  bracketed identifiers; only the install map still reads row-level coordinates.
- Results are cached with a TTL (`IQ_CACHE_TTL_SECONDS`) and a **Refresh data**
  button; cache keys never include secrets.
- The ODBC connection string is built with escaped values, with explicit pool
  size/timeout and a query timeout. A best-effort check warns when the database
  login can write to the source table; use a dedicated SELECT-only login.
- Demo CSV and database modes return the same aggregates (contract tests compare
  the SQL text and the pandas reference implementation).
- The Ask-the-data flow is unchanged in design: the model only returns a plan
  validated against a fixed allowlist, SQL is built locally from mapped columns
  with bound parameters, and no row data is sent to the model.

Index the purchase date, install date, company, and device-type columns of the
flat table for large datasets (the Azure table has about 4 million rows).

### UI and structure

- `dashboard.py` is a thin entry point that sets up navigation and language;
  each page is a small script in `views/`, with shared scaffolding in
  `insightquest/page_helpers.py` (filters, readiness, empty/error states,
  refresh, exports).
- Computation lives in `insightquest/analytics/`, SQL access in
  `insightquest/data.py` with caching in `insightquest/cache.py`.
- UI text is in `insightquest/locales/` (English and fr-CA).
- Exports offer Excel (with a Filters sheet) and CSV.

## Notes on projection

Purchase and installation projections are built as separate monthly series.
Forecasting uses:
- Holt-Winters exponential smoothing (primary model)
- Seasonal naive fallback when history is too short for stable model fitting

Rolling backtests report:
- MAE
- RMSE
- MAPE (when actual values are non-zero)
- MASE

Forecast results are projections, not actuals. Q6 evaluates on-hand devices
against reorder and maximum levels derived from trailing install demand and the
simulation inputs on the page; those thresholds are not read from the data.

## Tests

```bash
pip install pytest
python -m pytest tests -q
```
