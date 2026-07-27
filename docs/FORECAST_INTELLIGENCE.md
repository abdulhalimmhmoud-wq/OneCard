# Forecast Intelligence for Operations (v27)

## Why

Before v27 a reseller forecast captured **what** (merchant / product) and **how much**
(value / quantity) — but there was **no time dimension**. Every line was implicitly a flat
"planned monthly" figure, and the Ops page just summed those over a rolling 90 days. That
page couldn't answer the questions Operations actually needs to plan sourcing:

- *When* will each client need stock?
- What's needed in the next few **days** vs **weeks** vs **months**?
- Where is demand **spiking** vs the normal run-rate?
- Can we **cover** the forecast with what's on hand?
- **Who** is driving a spike — and can we trust them?

v27 captures a light timing hint at the source and turns the raw lines into an operational
picture.

## 1. Capture — the timing dimension

Three columns were added to `forecast_items` (idempotent migration; existing rows backfilled
to a recurring monthly baseline at medium confidence, so nothing regresses):

| Column | Meaning | Values | Default |
|---|---|---|---|
| `needed_by` | date the reseller wants it available | ISO date, or null | null |
| `period` | one-off need vs a recurring monthly run-rate | `one_off` \| `monthly` | `monthly` |
| `confidence` | how sure the reseller is | `high` \| `medium` \| `low` | `medium` |

**Reseller portal → Purchase Plan.** Each line (merchant or product) has a light quick-select:

- **When do you need it?** — *This week · This month · Next month · Specific date…*
- **Cadence** — *Every month* (recurring) or *One-off*
- **Confidence** — optional high / medium / low

The quick-select tokens resolve server-side (`models._norm_needed_by`): `this_week` → +7d,
`this_month` → +30d, `next_month` → +60d, or an explicit date. All inputs are normalised
(`_norm_period`, `_norm_confidence`) so bad values can't corrupt the columns the bucketing
keys off.

**Sales / account manager.** From the sales forecast detail page the account manager can
**refine a line's `needed_by` + `confidence`** (`models.set_forecast_line_timing`). They often
know the real timing better than a self-serve client (e.g. a client that stocks up before a
seasonal peak). Ops is notified when timing is refined.

## 2. The engine — `models.get_forecast_intelligence()`

One function (`get_forecast_intelligence(lookback_days=120, window_days=None, spike_ratio=1.5,
concentration=0.6)`) computes everything from the live forecast lines (those submitted within
`lookback_days`). Money is SAR. `window_days` defaults to `buy.forecast_days` (the near-term
horizon, 30d). It returns:

- **`buckets`** — value / units / line-count keyed by `week` (≤7d, incl. overdue), `month`
  (8–30d), `quarter` (31–90d), `later` (>90d), `recurring` (monthly cadence), `undated`
  (one-off with no date). Recurring lines are the ongoing baseline; one-off lines are placed
  by how far out `needed_by` is.
- **`timeline`** — 12 weekly buckets of **dated one-off** demand (this is where spikes show;
  recurring demand is the smooth baseline and is intentionally excluded here).
- **`by_merchant`** — near-term forecast (risk-adjusted) vs the **trailing actual run-rate**
  (the same-length window of real sales), a spike `ratio`, a `signal`, and single-client
  `top_share` / `top_new`.
- **`by_product`** — near-term (weighted) quantity vs **stock on hand**, the coverage `short`
  and `days_cover`, and the earliest `needed_by`.
- **`signals`** — a ranked, ready-to-act list (see below).
- **`register`** — every forecast with tier (active vs new/unproven), value, earliest needed-by.
- **`totals`**, **`unallocated_budget`** (exploratory starting budgets are reported separately).

### Risk adjustment

A forecast from a **new/unproven** reseller (no orders yet) is speculative, so its quantity
and value are discounted by `buy.new_client_forecast_weight` (default 40%) — the **same weight
the Buy Planner uses**, so the two stay consistent. A proven active client's forecast counts
in full.

### Signal definitions

| Signal | Trigger | Severity |
|---|---|---|
| **surge** | merchant near-term forecast ≥ `spike_ratio` × trailing run-rate | high |
| **new_demand** | near-term forecast with ~zero recent sales history | medium |
| **cooling** | near-term forecast < 0.5 × trailing run-rate | (row only) |
| **concentration** | one client drives ≥ `concentration` of a merchant's near-term demand | high if that client is new, else medium |
| **short** | a product's near-term (weighted) need exceeds stock on hand | high |

## 3. The console — Ops → 📊 Forecast Intelligence

`/ops/forecasts` renders the engine output:

1. **Headline** — total forecasted value, near-term (risk-adjusted), signal count, resellers,
   unallocated budget.
2. **Demand Signals** — the decisions to make, most-severe first, each with a link to the Buy
   Planner.
3. **When demand lands** — the time buckets + the 12-week timeline.
4. **Demand by Merchant vs baseline** — near-term vs trailing run-rate with the spike chip and
   concentration note.
5. **Coverage** — products we can't yet meet (short N, by when).
6. **Forecast Register** — every customer's plan; click through to
   `/ops/forecasts/<id>` for the **line-by-line detail with fulfilment** (units the client has
   actually ordered since submitting — intent vs reality) and on-hand context.

## Tuning

The near-term window and the risk weight come from the existing Ops-editable buy settings
(`buy.forecast_days`, `buy.new_client_forecast_weight` — Ops → Buy Planner → ⚙️ Tune weights).
`spike_ratio` and `concentration` thresholds are parameters of `get_forecast_intelligence`.

## Tests

`tests/e2e_v27.py` — timing capture through the portal, correct bucketing, spike / new-demand /
concentration / coverage-shortfall signals, new-client risk-discounting, the per-customer
register + detail + fulfilment, the account-manager refine flow, and backward-compat for
pre-v27 (untimed) forecasts.
