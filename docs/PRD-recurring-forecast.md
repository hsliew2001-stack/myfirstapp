# PRD — Recurring patterns & forward forecast

**Status:** Implemented (v1) · **Version:** 1.1 · **Date:** 2026-11-14
**App:** Statement → Spreadsheet (`statement-to-spreadsheet`)

> **Implementation notes (v1.1).** Built as specified, with four decisions made during
> the build that are worth recording because they are *safety* behaviours, not details:
>
> 1. **The burn is outflows-only.** Averaging non-recurring income into a daily "burn"
>    makes the projection climb forever, and in an affordability check that is the most
>    dangerous direction to be wrong. Non-recurring income is therefore not assumed to
>    arrive again (see 6.3).
> 2. **Forecasts require 14 days of history** (`MIN_HISTORY_DAYS`). Below that the answer
>    is a confident-sounding guess; the app returns `ready: false` with a reason instead.
> 3. **Same-day repeats are one occurrence.** A bill posts once per period; a same-day
>    pair would otherwise measure a zero-day gap and wreck the median cadence.
> 4. **A purchase's verdict uses the low point, never day zero.** The affordability check
>    inserts the purchase into the full projection and reads the minimum, so rent landing
>    before the purchase date is accounted for.
>
> Recurring detection, the forecast and affordability are covered by
> `backend/tests/test_insights.py`; the endpoints by `backend/tests/test_api.py`
> (46 tests). The SQL is exercised only by inspection — no MySQL/OceanBase was available
> in the build environment.

---

## 1. Summary

Statement → Spreadsheet today answers one question: *"what did I just spend?"* It reads a
screenshot, appends the amounts to a balance formula, and hands the workbook back. The
transaction data is used once and discarded.

This PRD adds the other half of the loop: the app should also answer *"what is about to
happen to my money, and can I afford this purchase?"*

Two features, one shared data foundation:

1. **Recurring-pattern detection** — find the bills, subscriptions and income streams that
   repeat on a schedule, and say when each is next due (including the ones that *didn't*
   arrive this month).
2. **Forward forecast / "can I afford this"** — project the balance day by day over the
   next weeks, show the lowest point, and answer a hypothetical purchase with a
   yes / tight / no verdict.

Both need a transaction history, so a third, enabling capability is in scope: **importing
past transactions** (CSV) and **storing applied transactions** per user.

---

## 2. Problem

- A budget spreadsheet records what already happened. The user only discovers a
  shortfall when they open it and see a negative number.
- People forget recurring charges. A subscription renews, a GIRO lands, and the budget
  silently drifts.
- "Can I afford this?" is answered today by scrolling a spreadsheet and doing arithmetic
  in your head, badly.
- The app already extracts exact, signed, dated transactions — the raw material for both
  answers is already in hand; it is just thrown away after each apply.

**Why now:** the extraction path is reliable and the database (OceanBase) is already
provisioned for the audit trail. The incremental cost is storage plus deterministic
statistics — no new platform services, no new model calls.

---

## 3. Goals & non-goals

**Goals**

- G1 — Detect recurring outflows and inflows with a precision the user would agree with
  (no obvious false positives on unrelated same-merchant purchases).
- G2 — Project balance day-by-day for a configurable horizon, with a stated confidence and
  explicit assumptions.
- G3 — Answer "can I afford X on date D" in one call, with an explainable verdict.
- G4 — Make the features usable on day one, from imported history, not after six months of
  use.
- G5 — Preserve user trust: the app now stores transactions, and must say so plainly and
  let the user erase them.

**Non-goals**

- Not a general ledger or double-entry accounting system.
- No bank API / open-banking integration (import is CSV and screenshots).
- No machine-learning forecasting. The model is deterministic and explainable; every
  number can be traced to transactions the user can see.
- No multi-currency conversion. Rows are stored per currency; projections run in one
  currency and say so.
- No shared/household accounts in v1.

---

## 4. Users & stories

**Priya** keeps a Sheets/Excel budget, screenshots her UOB app weekly, and reconciles by
hand.

| Story | Acceptance |
|---|---|
| As Priya, after importing six months of history, I see my subscriptions listed with the next due date, so I can cancel one. | ≥ 2 occurrences of a series → listed; next date within one cadence of the last. |
| As Priya, I see that my gym membership did **not** charge this month. | A series overdue by more than 25% of its cadence (min 7 days) is flagged. |
| As Priya, I ask "can I afford a S$900 flight on 28 Nov?" and get a verdict plus the lowest my balance will get. | Verdict + low point + date returned in one call. |
| As Priya, I want to know what the forecast is based on. | Assumptions and confidence are always shown, never hidden behind a tooltip. |
| As Priya, I don't want the app keeping my transactions forever. | One action erases all ledger rows for the signed-in user. |

---

## 5. Functional requirements

### F1 — Recurring-pattern detection

| ID | Requirement | Acceptance |
|---|---|---|
| F1.1 | Group transactions into candidate series by merchant key, direction (debit/credit) and currency. | Merchant key strips card numbers, reference digits and payment-rail noise. |
| F1.2 | Classify cadence from the median inter-occurrence interval. | Buckets: weekly 6–8d, fortnightly 12–16d, monthly 26–35d, quarterly 80–100d, annual 350–380d. Outside all buckets → not recurring. |
| F1.3 | Require ≥ 2 occurrences and interval consistency. | ≥ 60% of intervals inside the bucket. |
| F1.4 | Require amount stability. | ≥ 80% of amounts within 20% of the median. Otherwise the row is probably a multiple unrelated purchases at one merchant. |
| F1.5 | Report next expected date, last seen, typical amount, occurrences, cadence, confidence. | `next_expected = last_seen + median_interval`. |
| F1.6 | Flag expected-but-missing series. | `status = overdue` when `next_expected < today`; `missed = true` when more than `max(7d, 25% of cadence)` late. |
| F1.7 | Detect income series too (salary, rent received). | Same algorithm on positive amounts; surfaced separately. |

### F2 — Forward forecast

| ID | Requirement | Acceptance |
|---|---|---|
| F2.1 | Project daily balance from a start balance over a horizon (default 45 days). | One point per day, inclusive of day 0. |
| F2.2 | Include projected recurring occurrences on their expected dates. | Each series advanced by its median interval until the horizon ends. |
| F2.3 | Include variable day-to-day spending. | Mean daily non-recurring outflow over the observed span, applied evenly. |
| F2.4 | Report the low point (amount + date) and the end-of-window balance. | Low point is the minimum across the series. |
| F2.5 | Assign a confidence level with reasons. | high / medium / low from history span, month count, balance supplied, and series count. |
| F2.6 | Never fail the request for lack of data — return `ready: false` with an actionable reason. | No DB, no ledger rows, too-short span all handled. |

### F3 — Affordability check

| ID | Requirement | Acceptance |
|---|---|---|
| F3.1 | Accept an amount, a date, and an optional safety cushion. | Defaults: date = today, cushion = 0. |
| F3.2 | Return yes / tight / no plus the projected low point. | `no` if modelled low < 0; `tight` if low ≥ 0 but low − cushion < 0; else `yes`. |
| F3.3 | Explain the verdict in one sentence referencing the low point and date. | Reason always present. |
| F3.4 | Reject a date outside the horizon with a clear error. | 400 with the allowed window. |

### F4 — History import & storage (enabler)

| ID | Requirement | Acceptance |
|---|---|---|
| F4.1 | Applied transactions are stored per user, per account label. | Rows duplicated by fingerprint are skipped. |
| F4.2 | CSV import with header detection and preview. | Detects date / description / amount, or debit+credit pairs. Preview shows parsed rows and per-line errors; commit is a second call. |
| F4.3 | Rows without a parseable date are rejected on import (not silently dropped). | Error names the line number. |
| F4.4 | Storage failures never fail an apply. | Ledger write is best-effort and logged. |

### F5 — Privacy controls

| ID | Requirement | Acceptance |
|---|---|---|
| F5.1 | The UI states that transactions (not screenshots, not workbooks) are stored. | Visible in the Insights tab. |
| F5.2 | One action erases all of the user's ledger rows. | `DELETE /api/ledger`, confirmed in the UI. |
| F5.3 | All queries are scoped to the signed-in user. | `x-forwarded-email`; anonymous is a single fallback bucket. |

---

## 6. Algorithms

### 6.1 Merchant key

```
lowercase → non-alphanumerics to spaces → drop tokens that
  (a) contain no letter, (b) contain ≥ 2 digits, (c) are payment-rail noise
  (POS, PAYMENT, TRANSFER, PAYNOW, FAST, GIRO, CARD, DEBIT, SGD, SG, PTE, LTD, …)
→ first four surviving tokens
```
Rationale: `FAIRPRICE FINEST #0123 SG` and `FAIRPRICE FINEST 0456` collapse to
`fairprice finest`, while `GIRO TAX` and `GIRO RENT` stay distinct.

### 6.2 Series detection

For each `(merchant_key, direction, currency)` group with `n ≥ 2`:

```
dates     = sorted occurrence dates, collapsed to one per day
intervals = consecutive differences (days)
median_iv = median(intervals)
cadence   = bucket(median_iv)                       # else discard
support   = share of intervals inside the bucket    # must be ≥ 0.60
amounts   = |amount| per occurrence
stability = share within ±20% of median(amounts)    # must be ≥ 0.80
next      = last_date + median_iv
```

Confidence: **high** if `n ≥ 3`, interval spread ≤ 3 days and amount spread ≤ 10%;
**low** if `n == 2`; otherwise **medium**.

### 6.3 Forecast

```
burn      = Σ |debit| for non-recurring rows ÷ days in observed span   (outflows only)
per day d in [as_of, as_of + horizon]:
    balance[d] = balance[d-1] - burn + Σ recurring(amount on d) + planned(amount on d)
low       = min(balance)
```

`burn` counts **outflows only**. Non-recurring income is deliberately not averaged in:
spreading one-off income across every day pushes the burn negative and makes the
projection climb forever, which in an affordability check is the dangerous direction to
be wrong. The consequence is stated in the assumptions — the projection may be
pessimistic about money arriving, never optimistic.

Overdue series are projected `min(7, cadence/7)` days after today and listed in the
assumptions, because their money is still going to leave.

If the observed span is under `MIN_HISTORY_DAYS` (14), no forecast is produced: the
response is `ready: false` with the reason (see 5, F2.6).

### 6.4 Affordability

```
base   = forecast(planned = ∅)
plan   = forecast(planned = [{date: D, amount: −X}])
verdict = low(plan) < 0            → "no"
          low(plan) − cushion < 0  → "tight"
          else                     → "yes"
```

---

## 7. Data model

Single new Flyway migration; no DDL in application code.

```sql
CREATE TABLE tx_ledger (
    id          BIGINT        NOT NULL AUTO_INCREMENT,
    actor       VARCHAR(320)  NOT NULL,          -- x-forwarded-email (or 'anonymous')
    account     VARCHAR(128)  NOT NULL,          -- the label used ("UOB One")
    tx_date     DATE          NOT NULL,
    description VARCHAR(512)  NOT NULL,
    amount      DECIMAL(14,4) NOT NULL,          -- signed: debit < 0
    currency    CHAR(3)       NOT NULL DEFAULT 'SGD',
    fingerprint CHAR(64)      NOT NULL,
    source      VARCHAR(16)   NOT NULL DEFAULT 'screenshot',
    created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_ledger_actor_fp (actor, fingerprint),
    KEY ix_ledger_scope (actor, account, currency, tx_date)
) DEFAULT CHARSET=utf8mb4;
```

Recurring series are **computed on read**, not stored: they are cheap to derive from a
few thousand rows, and a stored table would go stale the moment history changes.

---

## 8. API

All under `/api`, all same-origin, all scoped by `x-forwarded-email`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/insights?balance=&currency=SGD&account=&horizon_days=45&as_of=` | Recurring series + summary + forecast |
| `POST` | `/affordability` | `{amount, date, balance, currency, cushion, horizon_days}` → verdict + forecast |
| `POST` | `/ledger/import` | Multipart CSV (`file`) or `text`, `account`, `currency`, `commit` |
| `POST` | `/ledger/rows` | Manual batch add `{account, currency, rows: [...]}` |
| `GET` | `/ledger?limit=200` | Recent stored transactions (drives the table view) |
| `DELETE` | `/ledger` | Erase the caller's ledger rows |

`GET /insights` response sketch:

```json
{
  "ready": true,
  "currency": "SGD",
  "coverage": {"first": "2026-05-01", "last": "2026-11-12", "days": 195, "transactions": 214},
  "balance": {"supplied": true, "amount": "4200.00"},
  "recurring": [{
    "key": "netflix", "title": "NETFLIX.COM", "direction": "debit",
    "cadence": "monthly", "interval_days": 30, "amount": "-17.98",
    "occurrences": 6, "last_seen": "2026-10-21", "next_expected": "2026-11-20",
    "days_until": 6, "status": "due", "missed": false, "confidence": "high"
  }],
  "summary": {"monthly_income": "5200.00", "monthly_spend": "3410.50",
              "monthly_net": "1789.50", "daily_burn": "41.20", "months": 6},
  "forecast": {"start": "2026-11-14", "end": "2026-12-29",
               "low": {"date": "2026-11-27", "balance": "1120.40"},
               "end_balance": "3902.10", "confidence": "high",
               "points": [{"date": "2026-11-14", "balance": "4200.00"}],
               "assumptions": ["…"]},
  "warnings": []
}
```

---

## 9. UX

- A two-tab header: **Reconcile** (existing flow) and **Insights** (new).
- Insights, top to bottom: coverage line + import disclosure → recurring table →
  forecast hero (low point, end balance, confidence) + chart → affordability form.
- The chart is a single-series line: 2px line, ~10% area wash, hairline grid, direct
  label at the low point and the endpoint, no legend (one series), hover crosshair +
  tooltip, keyboard arrow navigation, and a table view under a disclosure.
- Verdict uses status tokens **with an icon and a word**, never color alone.
- Empty and unready states always say what to do next (import history, supply a balance).

---

## 10. Metrics

- Recurring precision: share of detected series the user does not delete (target ≥ 90%).
- Time-to-first-insight after signup (target < 3 minutes via CSV import).
- Affordability verdicts opened per active user per month.
- Ledger rows per active user (health check; a flat line means imports aren't landing).

## 11. Risks

| Risk | Mitigation |
|---|---|
| False-positive recurring series erode trust | Conservative thresholds; show occurrences and dates in the UI so a wrong series is obvious. |
| Forecast is wrong and a user over-spends | Confidence + assumptions always visible; wording is "projection", never "your balance will be". |
| Storing transactions scares users away | Explicit disclosure, per-user scoping, one-click erase, no screenshots/workbooks stored. |
| Sparse history produces nonsense | `ready:false` below a minimum span; low confidence stated in words. |
| Currency mixing | Projections are single-currency; foreign rows are excluded and reported. |

## 12. Rollout

1. Migration + storage + import (invisible until history exists).
2. Recurring detection + insights endpoint.
3. Insights tab, chart, affordability.
4. Follow-up (out of scope): CSV as a first-class upload path in the Reconcile flow,
   balance read directly from the workbook, monthly "expected but missing" digest.

## 13. Open questions

- Should detected income be corrected when the user's pay varies (median vs most recent)?
  v1 uses the median.
- Should the app offer to auto-apply a detected recurring charge it never saw arrive?
  No — v1 only warns.
- Retention: keep history indefinitely, or auto-prune beyond 24 months? v1 keeps
  everything until the user clears it.