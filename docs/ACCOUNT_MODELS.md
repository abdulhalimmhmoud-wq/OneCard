# Account Models, Contracts & Settlement — Design Spec

This is the living spec for the three commercial arrangements OneCard offers
resellers, the contract-signing workflow that provisions them, and the
governance around credit. Built in phases; each section notes its phase.

## 1. Account types (the financial gate)

Every reseller has an `account_type`. Ordering is gated by a single function
`available_to_spend(profile)`; each type answers it differently and an order
has a type-specific side effect. `SAR` is always the internal base.

| Type | `available_to_spend` | Order effect | How they pay |
|------|----------------------|--------------|--------------|
| `prepaid` (cash) | `wallet_balance` | debit `wallet_balance` | transfer first → upload receipt → Finance credits wallet (today's flow) |
| `credit` | full: `credit_limit − outstanding`<br>staged: `min(credit_tranche, credit_limit − outstanding)` | increase `credit_outstanding` | settle the drawn amount each billing cycle (net terms); limit resets on settlement |
| `consignment` | `credit_limit − outstanding` | increase `credit_outstanding` (accrues to the open statement) | pulls card-by-card (usually via API); pays the monthly/period statement |

Every money movement is written to `wallet_transactions` (types: `topup`,
`order`, `credit_draw`, `consignment_draw`, `settlement`, `adjustment`) — one
audit trail for all three models.

### Credit release (configurable per client)

- `credit_disbursement = 'full'` → the whole limit is available at once.
- `credit_disbursement = 'staged'` → only a **tranche** (`credit_tranche`) is
  exposed at a time. As the client draws it down, the tranche **replenishes
  continuously** (not on a timer), with cumulative draws capped at
  `credit_limit` for the cycle. This is exactly:
  `available = min(credit_tranche, credit_limit − outstanding)`.

  *Example:* limit 500,000/mo, tranche 150,000. The client never has more than
  150,000 available at once, but can keep drawing (replenished) up to 500,000
  total for the month, then settles.

### Additional-credit requests (Phase 2)

When a client reaches their limit (`available == 0`) the team is notified. The
sales manager can request a limit bump, flagged **temporary** (with an end date,
e.g. a season) or **permanent**. The request goes to CCO **and** Finance for
approval; the team is notified on both "limit reached" and "bump requested".

### Due date / auto-charge (Phase 2, no PSP)

There is no payment gateway yet. On the due date, an unpaid statement flips to
`overdue`, ordering auto-freezes (`available_to_spend → 0`), and reseller +
Finance + CCO are escalated. Real card auto-charge is a documented integration
point (`charge_card()` hook) for later.

## 2. Contract signing (Phase 1)

A `contracts` row drives provisioning; `contract_events` is the append-only
audit log ("every action recorded").

1. **Sales** uploads a draft contract file and sets the proposed `account_type`,
   `credit_limit`, terms → status `sent`; reseller notified.
2. **Reseller** downloads it, signs offline, uploads the signed copy → status
   `signed_uploaded`; Sales + CCO notified.
3. **Activation** applies the agreed terms to the profile and sets
   `contract_status = 'contracted'` (ordering unlocks) → status `active`.

Files live in a private dir, served only to Sales-owner / CCO / Finance / admin
and the owning reseller (same pattern as wallet receipts).

## 3. Governance (approval authority)

- **Prepaid** or **credit/consignment with `credit_limit ≤ AUTO_APPROVE_CAP`**:
  the sales manager can activate directly. Finance is notified to acknowledge.
- **credit_limit > AUTO_APPROVE_CAP**: activation requires **CCO** approval;
  **Finance** is also notified and acknowledges (mix of authorities).
- Additional-credit bumps: **CCO + Finance** approve, with temp/permanent flag.

`AUTO_APPROVE_CAP` is a configurable threshold (default 100,000 SAR).

## 4. What changes per role

- **Sales** — pick account_type + terms at registration / My Resellers; upload
  draft contract; view the signed upload; track status; request credit bumps.
- **Reseller** — new **Contract** page (download draft / upload signed / status);
  account-aware **Wallet** (prepaid top-up | credit limit/released/outstanding +
  statements | consignment accrual + statements).
- **CCO** — approval queue for large limits + bump requests; exposure KPIs
  (outstanding, overdue, consignment liability).
- **Finance** — beyond top-ups: issue/track statements, approve settlement
  receipts, aging/overdue, acknowledge new credit lines.
- **Operations** — flag consignment/API resellers (real-time demand, not
  stocked) and surface live draw-down vs stock, extending the v12 demand page.
- **API v1** — ordering gate uses `available_to_spend` (credit/consignment can
  pull without prepaid balance); `/api/v1/account` + `/api/v1/statement`;
  webhooks `statement.issued` / `statement.overdue`.

## 5. Phases

- **Phase 1** — contracts workflow + account_type foundation + `available_to_spend`
  gate for all three types (draws work). *Shipped v13.*
- **Phase 2** — credit engine: statements, settlement + receipts, overdue freeze,
  additional-credit requests (CCO + Finance two-sign-off, temporary/permanent),
  exposure dashboards. *Shipped v14.*
- **Phase 3** — consignment engine: period statements, API card-by-card without
  prepaid, Ops/CCO liability dashboards.
- **Phase 4** — reports, webhooks (`statement.issued`/`overdue` already emit),
  `/api/v1/account` + `/statement`, polish, API guide update.
