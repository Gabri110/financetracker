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

---

_Order of attack: 1 → 2 → 3 → 4 (each builds on the previous for correct totals)._
