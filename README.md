# OneCard — Reseller Operations Platform (v4)

A **5-role web platform** that digitizes OneCard's full reseller lifecycle — from first contact
to contract, wallet funding, ordering, and monthly commitment tracking. Built with Flask + SQLite
as a **product model / prototype**: the technical team connects it to the live company systems
via the integration points documented in `models.py`.

## Roles & Responsibilities

| Role | What they do |
|------|--------------|
| **Admin / BD Manager** | Tier rules, master catalogue, all resellers, user accounts (all roles), compliance check, full visibility over every approval queue. |
| **Sales Manager** | Registers resellers (with business profile: client type + operating countries), previews their portals, receives purchase forecasts, signs contracts, requests special merchant discounts from the CCO. |
| **CCO** | Approves/rejects special discount requests with full profit before/after economics. Approved rates apply **automatically**. |
| **Finance** | Verifies bank-transfer receipts and credits reseller wallets. |
| **Reseller** | Browses the catalogue with tier pricing, submits purchase plans (forecasts), tops up a wallet, places orders, and tracks their own purchase analysis. |

## The Customer Journey (end to end)

1. **Registration** — Sales manager creates the account: expected monthly sales → auto-assigns the pricing plan; client type + countries → power personalized recommendations.
2. **Discovery** — Reseller browses catalogue (filters: merchant, category, country, region, currency), profit calculator supports a **multi-item basket** (products + merchants combined), and a **Recommended page** tailored to their business type and markets.
3. **Forecast** — Reseller submits a purchase plan (merchant totals and/or product quantities) → notifies the sales manager. No commitment yet.
4. **Contract** — Sales manager signs the contract → ordering unlocks.
5. **Wallet** — Reseller transfers to OneCard's bank, uploads the receipt → Finance verifies → balance credited.
6. **Orders** — Reseller orders products with quantities, paid from wallet, with a live **forecast vs actual** comparison (ordering above plan is allowed).
7. **Special discounts** — Sales manager requests a higher margin share on a specific merchant (with current + projected sales) → CCO sees OneCard profit before/after → approval applies the override automatically.
8. **Compliance** — Monthly automatic check: below commitment → warning + one grace month (everyone notified) → still below → **automatic downgrade one tier**.
9. **Analysis** — Reseller sees spend, gains, merchant/category mix, monthly trend and growth insights.

## Business Rules

- **Margin-sharing pricing**: `reseller_price = max(default_price − (oc_margin × margin_share%), cost)` — per-merchant CCO overrides take precedence over the tier share.
- **Whole numbers only**: all money values display without decimals.
- **No exports** for resellers and sales managers (data confidentiality). Admin retains export.
- **Preview mode is read-only** — staff previewing a reseller portal cannot act on their behalf.
- **Receipts are private** — only Finance/Admin and the owning reseller can view them.

## Default Accounts (seeded)

| Role | Email | Password |
|------|-------|----------|
| Admin | `admin@onecard.com` | `OneCard2025!` |
| Sales | `sales@onecard.com` | `Sales2025!` |
| CCO | `cco@onecard.com` | `Cco2025!` |
| Finance | `finance@onecard.com` | `Finance2025!` |

> Change these before any non-local deployment.

## Quick Start

```bash
pip install -r requirements.txt   # flask, pandas, openpyxl, bcrypt
python seed_products.py           # first run — import Full Catalogue.xls
python app.py                     # http://localhost:8000
```

An existing v3 database upgrades **in place automatically** (migrations run at startup).

## Project Files

| File | Description |
|------|-------------|
| `app.py` | Flask routes for all 5 roles + workflows |
| `models.py` | SQLite schema v4, CRUD, pricing engine, compliance engine — **all integration points documented here** |
| `auth.py` | bcrypt login + per-role access decorators |
| `seed_products.py` | Catalogue import (column mapping = the integration contract with the company system) |
| `templates/` | Jinja2 UI — `partials/` (role navs), `admin/`, `sales/`, `reseller/`, `cco/`, `finance/` |
| `uploads/receipts/` | Bank-transfer receipts (private, served with access control) |
| `onecard.db` | SQLite database (generated locally; not tracked in git) |

## Integration Notes (for the technical team)

All reads/writes go through `models.py`. To connect production systems, swap the bodies of:

- **Catalogue feed** → `seed_products.py` (`PREFERRED` column map) + `products` table
- **Actual reseller sales** → `get_month_total_orders()` (compliance currently uses platform orders)
- **Payments** → `wallet_transactions` (manual finance verification in v4; replace with bank API/webhooks)

---

*Built for OneCard Digital Distribution — v4, July 2026*
