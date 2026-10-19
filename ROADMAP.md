# Roadmap — priority fixes

Working through these one by one. Status: `TODO` → `IN PROGRESS` → `DONE`.

---

## 1. Transfers aren't real transfers  `TODO`

**Problem:** `transfer` exists as a transaction *type*, but a transfer is a single row tied
to one account, and the balance calculation ignores it (`ELSE 0` in the income/expense sum at
`app.py:1872`). Moving money between two of your own accounts doesn't debit one and credit the
other — it just vanishes from balances.

**Goal:** A transfer should move money from a *source* account to a *destination* account:
decrease the source balance, increase the destination, and not count as income or expense in
reports.

**Touch points:** `transactions` schema, add/edit transaction form, balance aggregation
(`dashboard`, accounts list), import flow (already detects transfer-like categories).

**Audit notes (2026-10):**
- `is_transfer_like_category` matches the substring "transfer", so Revolut's generic
  "transfers" category (also used for payments to *other people*) forces real spending to
  type transfer and out of expenses/budgets (`upsert_bank_transactions`).
- Moves between own accounts that are *not* detected inflate income and expenses: Revolut
  TOPUP rows (income), Trade Republic `TRANSFER_INBOUND`/`TRANSFER_INSTANT_INBOUND`
  (`TRADE_REPUBLIC_INCOME_TYPES`), B100 outgoing transfers (expense).
- Refunds are booked as income (Revolut CARD_REFUND/REFUND/CHARGEBACK_REVERSAL, negative
  Iberia/Edenred charges). They should reduce spending in their category instead, so budgets
  and reports net them out. Rows imported before the Edenred refund-sign fix keep their old
  type (re-imports no longer overwrite the type).
- Import category matching ignores the transaction type, so income rows can get expense
  categories.

---

## 2. Opening / initial balances are ignored  `TODO`

**Problem:** `accounts.balance` column exists (`app.py:152`) but every displayed balance is
recomputed purely as `income − expense` from transactions (`app.py:1870`). A starting balance
set at account creation never appears, so net worth is wrong unless every account starts at zero.
The `balance` column is effectively dead.

**Goal:** Account balance = opening balance + (income − expense ± transfers). Decide whether to
keep an `opening_balance` field (cleaner) vs. reusing `balance`.

**Touch points:** account create/edit form, balance aggregation everywhere it's computed.

---

## 3. No net-worth-over-time tracking  `TODO`

**Problem:** Investments get snapshots, but cash accounts don't, and nothing combines them into
a single net-worth history over time.

**Goal:** Periodic (or on-demand) net-worth snapshots combining cash account balances +
investment value, in base currency, with a chart of the trend. Depends on #1 and #2 being
correct so the totals are trustworthy.

**Touch points:** new snapshot mechanism for total net worth, reports/dashboard chart.

---

## 4. Investment tracking completeness  `TODO`

From `TODO.md`, still open:

- **CSV import of investments** (holdings and/or operations).
- **Fuller automation of current portfolio values** (price sync exists; make it
  comprehensive/reliable).
- **Realized vs. unrealized gains** handled properly.
- **Dividend tracking as a first-class concept** (currently no dedicated handling).
- **Trade Republic / 1%-card-cashback "fixed income" asset class** — model these cash-yield
  sources as asset classes and account for their returns.

**Touch points:** `investment_holdings`, `investment_operations`, import flow, price sync,
investments dashboard.

**Audit notes (2026-10):**
- Trade Republic: only BUY becomes an investment operation; SELL and DIVIDEND rows fall
  through to income/expense, holdings never decrease, and the `fee`/`tax` columns are ignored.
- Mintos: withdrawals and other unknown transaction types are skipped by `parse_mintos_xlsx`,
  so value is overstated and cost basis never decreases. The current Mintos holding's stored
  valuation returns are off by roughly the withdrawn amount (totals and YTD are now derived
  from value − cost and valuations, so they are not affected).
- YTD for market-priced holdings only counts dividends + realized gains (price movement since
  1 January is not captured without year-start snapshots).
- Historical FX: one current rate per currency pair, so old months and cost bases are
  converted at today's rate.

---

_Order of attack: 1 → 2 → 3 → 4 (each builds on the previous for correct totals)._
