# Forecasted Demand for Operations (v27 capture, v28 presentation)

## Design principle: inform, don't decide

This page exists to give Operations **accurate, useful information, presented plainly** — and
then get out of the way. It does **not** raise alarms, rank "signals", apply hidden weighting,
or tell the user what to do. It shows the numbers (what's planned, when it's needed, how that
compares to recent sales and current stock) and the user decides. (An earlier v27 build added a
"Demand Signals" panel with surge/concentration alerts and command-style wording; that was
removed in v28 as noise that overstepped into decision-making.)

## Why

Before v27 a reseller forecast captured **what** (merchant / product) and **how much**
(value / quantity) — but there was **no time dimension**. Every line was implicitly a flat
"planned monthly" figure, so the Ops page couldn't answer *when* stock is needed, what's needed
in the next **days** vs **weeks** vs **months**, or how a plan compares to normal sales and stock.

## 1. Capture — the timing dimension (v27)

Three columns on `forecast_items` (idempotent migration; existing rows backfilled to a recurring
monthly baseline at medium confidence, so nothing regresses):

| Column | Meaning | Values | Default |
|---|---|---|---|
| `needed_by` | date the reseller wants it available | ISO date, or null | null |
| `period` | one-off need vs a recurring monthly run-rate | `one_off` \| `monthly` | `monthly` |
| `confidence` | how sure the reseller is | `high` \| `medium` \| `low` | `medium` |

**Reseller portal → Purchase Plan.** Each line (merchant or product) has a light quick-select:
**When do you need it?** (This week / This month / Next month / Specific date), **Cadence**
(Every month vs One-off), and an optional **Confidence**. Tokens resolve server-side
(`models._norm_needed_by`: `this_week`→+7d, `this_month`→+30d, `next_month`→+60d, or a date). All
inputs are normalised (`_norm_period`, `_norm_confidence`) so bad values can't corrupt the columns.

**Sales / account manager.** From the sales forecast detail page the account manager can refine a
line's `needed_by` + `confidence` (`models.set_forecast_line_timing`) — they often know the real
timing better than a self-serve client. Ops is notified when timing is refined.

## 2. The data — `models.get_forecast_intelligence(lookback_days=120, window_days=None)`

Reads the live forecast lines (submitted within `lookback_days`) and returns **plain aggregates —
no verdicts, no weighting**. Money is SAR; `window_days` defaults to `buy.forecast_days` (30). It
returns:

- **`buckets`** — value / units / line-count keyed by `week` (≤7d, incl. overdue), `month`
  (8–30d), `quarter` (31–90d), `later` (>90d), `recurring` (monthly cadence), `undated`. Recurring
  lines are the ongoing baseline; one-off lines are placed by how far out `needed_by` is.
- **`timeline`** — 12 weekly buckets of **dated one-off** demand (recurring demand is the smooth
  baseline and is intentionally excluded here).
- **`by_merchant`** — `planned` for the next `window` days next to `baseline` (actual sales over
  the same-length recent window), a plain `ratio` (planned ÷ recent, or null when there are no
  recent sales), and `clients` (how many resellers are behind it). Just the numbers.
- **`by_product`** — `planned_qty` for the window vs `on_hand` stock; `gap` is `max(0, planned −
  on_hand)` (plain arithmetic), with the earliest `needed_by`.
- **`register`** — every forecast with `tier` (`active` = has orders, `new` = none yet), value and
  earliest needed-by, so Ops can weigh how much to trust each plan **themselves**.
- **`totals`**, **`unallocated_budget`** (exploratory starting budgets reported separately).

Numbers are the **raw client figures** — new-vs-existing is surfaced as data (the register `tier`)
rather than silently discounted, so Ops applies their own judgement. (The separate Buy Planner
still applies its configurable weighting; that page is explicit decision-support and unchanged.)

## 3. The page — Ops → 📅 Forecasted Demand

`/ops/forecasts` renders the aggregates as calm tables:

1. **Headline numbers** — total planned, planned for the next 30 days, clients with a plan,
   products where the plan is above stock, budgets not yet allocated.
2. **When it's needed** — the time buckets + the 12-week timeline.
3. **By merchant — planned vs recent sales** — planned, sold, "vs recent" ratio, client count.
4. **By product — planned vs stock on hand** — planned, in stock, gap, needed-by.
5. **Every client's plan** — the register; click through to `/ops/forecasts/<id>` for the
   line-by-line detail with timing and **fulfilment** (units the client has actually ordered since
   submitting — intent vs reality).

## Tests

`tests/e2e_v27.py` — timing capture through the portal, correct time-bucketing, planned-vs-recent
and planned-vs-stock (raw numbers, plain arithmetic gap), the per-customer register + detail +
fulfilment, the account-manager refine flow, that the data carries **no** alerts/verdicts list,
that the page shows **no** alarm-wall/command language, and backward-compat for pre-v27 forecasts.
