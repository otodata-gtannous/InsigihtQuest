# InsightQuest

InsightQuest is a Streamlit analytics app. All analysis is read from one
configured SQL Server flat table.

## The six questions InsightQuest answers

The app exists to help classify the **installation capacity of our customers**.
It is built around six analysis questions (originally posed in French):

| # | Question (FR) | Question (EN) | Page |
| --- | --- | --- | --- |
| 1 | Pics et creux des achats et installations | Peaks and lows of purchases and installations | Purchases & installations |
| 2 | Écarts par territoire et taille des clients et type de produit | Gaps by territory, customer size, and product type | Territory, customer & product mix |
| 3 | Délai entre expédition et installation | Delay between shipping and installation | Shipping to installation |
| 4 | Taux mensuel d'installation | Monthly installation rate | Installation rate & forecasts |
| 5 | Prévisibilité des achats versus les marchés | Purchase predictability versus markets | Installation rate & forecasts |
| 6 | Seuils critiques pour risques de rupture ou de surstock | Critical thresholds for stockout or overstock risk | Inventory risk |

See [Question coverage and data readiness](#question-coverage-and-data-readiness)
for what each question needs from the flat table, and
[Design decisions](#design-decisions-and-roadmap) for architecture notes.

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
- **Ask the data**: choose private analysis or opt-in guided aggregate queries. Set
  `IQ_OPENAI_API_KEY` (or `OPENAI_API_KEY`) and optionally `IQ_OPENAI_MODEL`
  (default `gpt-4o-mini`, with structured-output support) in `.env`.
  In the default **Private** mode, the server extracts fixed analysis labels from the question locally. Only
  those labels, static instructions, and a fixed response schema go to OpenAI.
  Questions, history, source schema, records, sample rows, IDs, coordinates,
  aggregates, and results are not sent. Labels still disclose the general
  analysis topic; use an internal model if even that must remain confidential.
  The model has no database tools and cannot supply SQL. Its response is
  validated against a fixed allowlist, then the user confirms an analysis.
  Calculations and answer formatting run locally, with no second LLM call.
  Missing keys, ambiguous questions, and planner failures allow explicit manual
  selection; failures are shown rather than presented as model answers.
  Available analyses include overview, monthly purchases/installations, counts
  by company/type, installation delay, pending installations, and coordinate
  coverage. Each question is independent: arbitrary filters in question text,
  top-N limits, and follow-up references are not applied. Review the plan and
  set inclusive purchase/install date filters in the local controls.
  Results identify their scope, count rows unless labeled distinct, and warn
  when the source row limit is reached. Rows/results are not globally cached
  by the chat page; displayed results are held in the user's Streamlit session.
  This does not add authentication or tenant authorization: use only within
  an authorized deployment and restrict the database account/source accordingly.
  Do not enable request-body logging or external tracing of questions/results
  in deployment infrastructure. Existing analytics pages are unchanged.

### Guided aggregate queries (opt-in)

Set `IQ_AI_ALLOW_EXTERNAL_DATA=true` and restart Streamlit, then choose
**Guided (approved external sharing)** on **Ask the data**. Keep this disabled
unless your organization permits sharing questions and aggregates with OpenAI.
For database mode, configure the `IQ_DB_*` credentials as a SELECT-only account
restricted to the approved source table/view, then set
`IQ_AI_DB_READ_ONLY_CONFIRMED=true`. This is an administrator attestation, not
permission enforcement. Do not use `sa` or another write-capable account.
Synthetic demo mode does not need database credentials or this attestation.

1. Review your question, remove confidential values/identifiers, and approve
   sending it to OpenAI. The planner also receives the current date, static
   instructions, and a fixed schema, but no source schema, source records,
   sample rows, results, or history.
2. Inspect the returned JSON plan and explicitly run it locally. The server
   independently validates every plan field. No model-generated SQL or code
   is accepted; only fixed operations and administrator-mapped fields are used.
3. Inspect the local result and the exact optional outbound aggregate payload.
   A separate approval sends that payload for an AI explanation. The question
   is not resent. Results remain usable without this second call.

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

Queries load only the required mapped date/type columns and use the existing
parameterized SELECT reader. At most 100,000 source rows may be analyzed: an
extra sentinel row detects overflow, which fails with a request to narrow the
dates rather than producing partial totals. Guided database connections use a
30-second ODBC query timeout; external calls use a 30-second timeout, no automatic
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

Pages never invent missing data: when a required column is not mapped or does
not exist, the page shows a readiness message instead of a result. Status below
reflects the confirmed flat-table columns (see section 2) plus the extractor's
`Territory` column.

| # | Status | What works today | What is missing to be complete |
| --- | --- | --- | --- |
| 1 | Partial | Installation peaks/lows by day/week/month; purchase peaks/lows when a purchase measure is mapped | `IQ_COL_SALES_AMOUNT` (no purchase amount column exists; purchase row counts are not yet a fallback) |
| 2 | Partial | Product type (`DeviceType`) and customer (`CompanyId`) work by default; territory works after `IQ_COL_TERRITORY=Territory` | Customer size: no column exists. A derived segment (for example, device-count buckets per `CompanyId`) must be added in the extractor and mapped to `IQ_COL_CUSTOMER_SIZE` |
| 3 | Not yet | Page and calculations are ready (median, P90, exclusions) | A shipment date mapped to `IQ_COL_SHIPPED_AT`; `PurchaseDate` is derived and must not be used silently as a proxy |
| 4 | Partial | Monthly installation counts and forecast | A shipment date and eligible-unit denominator for the true rate |
| 5 | Partial | Separate Holt-Winters forecasts with backtests (MAE, RMSE, MAPE) | A purchase measure and an external monthly market indicator (`IQ_COL_MARKET_INDICATOR`, `IQ_COL_MARKET_INDICATOR_AT`) |
| 6 | Not yet | Page and risk rules are ready | An inventory snapshot source: item, as-of date, on-hand, demand, replenishment days, reorder point, maximum stock |

Questions 5 and 6 need data that is not a device attribute (market series,
inventory snapshots); see the design notes below.

## How each question is answered

1. **Purchase/install peaks and lows:** daily, weekly, or monthly totals ranked by period.
2. **Territory, customer size, and product differences:** installation counts grouped by the configured dimensions; no segments are inferred.
3. **Shipping-to-installation delay:** per-record elapsed days, median, and 90th percentile; invalid or negative delays are excluded.
4. **Monthly installation rate:** installed eligible units divided by shipped eligible units for each shipment-month cohort.
5. **Purchase forecasts versus markets:** separate purchase and installation forecasts use Holt-Winters smoothing, with a seasonal-naive fallback and rolling backtests. Market comparison is descriptive, not causal.
6. **Stockout/overstock thresholds:** rule-based checks compare projected stock against configured reorder and maximum levels.

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

The confirmed columns in `dbo.[Insight Project]` are `Id`, `PurchaseDate`,
`DeviceType`, `TankInstallDate`, `DeviceInitialInstallDate`, `CompanyId`,
`Longitude`, and `Latitude`. Normal and demo defaults use only these confirmed
source columns:

| Setting | Column | Meaning |
| --- | --- | --- |
| `IQ_COL_DEVICE` | `Id` | DeviceId / device identifier |
| `IQ_COL_SHIPPING_KEY` | `Id` | Stable device key |
| `IQ_COL_SALES_DATE` | `PurchaseDate` | Purchase date |
| `IQ_COL_PRODUCT_TYPE` | `DeviceType` | Product/device type |
| `IQ_COL_INSTALLED_AT` | `TankInstallDate` | Preferred installation date; `DeviceInitialInstallDate` is the alternative |
| `IQ_COL_CUSTOMER` | `CompanyId` | Customer/company identifier |
| `IQ_COL_LATITUDE` | `Latitude` | Numeric latitude |
| `IQ_COL_LONGITUDE` | `Longitude` | Numeric longitude |

All other `IQ_COL_*` mappings are blank in both normal and demo defaults because
no matching source column was confirmed:

- `IQ_COL_LOCATION`: no combined location field; the Overview map uses the
  separate latitude/longitude mappings instead.
- `IQ_COL_TERRITORY` and `IQ_COL_CUSTOMER_SIZE`: territory trends and the
  territory/customer-size sections of **Territory, customer & product mix** are
  unavailable; mapped customer and product breakdowns remain available.
- `IQ_COL_SALES_AMOUNT`: purchase totals in **Purchases & installations** and
  purchase forecasts in **Installation rate & forecasts** are unavailable;
  installation analyses remain available.
- `IQ_COL_SHIPPED_AT` and `IQ_COL_RATE_DENOMINATOR`: **Shipping to installation**
  lacks its shipment date; shipment-cohort installation rates in
  **Installation rate & forecasts** lack shipment dates and eligible-unit inputs.
- `IQ_COL_MARKET_INDICATOR` and `IQ_COL_MARKET_INDICATOR_AT`: market context in
  **Installation rate & forecasts** is unavailable.
- `IQ_COL_INVENTORY_ITEM`, `IQ_COL_INVENTORY_AS_OF`, `IQ_COL_INVENTORY_ON_HAND`,
  `IQ_COL_INVENTORY_DEMAND`, `IQ_COL_REPLENISHMENT_DAYS`, `IQ_COL_REORDER_POINT`,
  and `IQ_COL_MAX_STOCK`: **Inventory risk** is unavailable.

The demo CSV uses `Latitude` and `Longitude` too. Additional synthetic fields
remain in the CSV but are not mapped by default. Map unsupported analyses only
after confirming corresponding source columns and their business definitions.

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

## Docker deployment

Create a local `.env` from `.env.example` and configure the data source. Start
the app from the repository root:

```bash
docker compose up --build -d
```

Open `http://localhost:8501`. To stop the app:

```bash
docker compose down
```

For demo mode, set `IQ_DEMO_MODE=true` in `.env`. For database mode, configure
a SQL login and set `IQ_DB_SERVER` to a host reachable from the container (not
`localhost` unless SQL Server is in the same container). The image includes
Microsoft ODBC Driver 18. Do not copy secrets into the image or commit `.env`;
provide production secrets through the deployment platform's environment or
secret-management settings.

## Design decisions and roadmap

These come from a review of the repository against the six questions.

### Is a star schema needed?

**Not now.** The flat table is one row per device, so questions 1–4 are exact
device counts and do not need dimension tables. Keep `dbo.[Insight Project]`
as the app's read source. A full star schema is only worth it if more grains
(orders, shipments, several markets) are added later.

Questions 5 and 6 need data at a different grain and should be small side
tables, not columns repeated on every device row (repeating snapshots per
device would inflate any `SUM`):

- Inventory snapshot: item × as-of date (on-hand, demand, replenishment days,
  reorder point, maximum stock).
- Market indicator: indicator × month.
- Optional customer table (`CompanyId`, size, territory) if size or territory
  is not clean on the flat table; optional shipment table if shipments are not
  one-to-one with devices.

Expose them through views, point `IQ_SOURCE_TABLE` / `IQ_COL_*` at the view
columns, and add them as extractor migrations. Customer-level attributes must
be counted with `COUNT(DISTINCT CompanyId)`, never summed across device rows.

### Backend data access

Direct SQL Server access from Streamlit is appropriate for this internal tool
and stays. Planned changes, in priority order:

1. Push aggregation into SQL (`GROUP BY`) instead of loading up to 100,000 raw
   rows into pandas; index the date, company, and type columns.
2. Cache query results with a TTL and add a refresh button.
3. Use a dedicated SELECT-only database login instead of relying on the
   `IQ_AI_DB_READ_ONLY_CONFIRMED` attestation alone.
4. Run the demo CSV and the database through the same query path so the two
   modes behave identically.
5. Build the ODBC connection with escaped values and set explicit pool and
   query-timeout settings.

The Ask-the-data flow keeps its current design: the model only returns a plan
validated against a fixed allowlist, SQL is built locally from mapped columns
with bound parameters, and no row data is sent to the model.

### UI and structure

Planned changes:

- Make the home page a guide to the six questions with links to each page.
- Prefix page titles with the question number and give question 5 its own page
  (it currently shares a page with question 4).
- Extract the repeated page scaffolding (filters, row limit, readiness checks,
  error handling) into a shared helper, and show friendly errors instead of
  stack traces.
- Cache page loaders and split `insightquest/analytics.py` per question.
- Add French (fr-CA) UI text alongside English.
- Remove the empty `src/insightquest/` folder; the package lives at
  `insightquest/`.

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
