"""Recurring-pattern detection and a forward balance forecast.

**Deliberately model-free.** Every number this module returns can be traced back to
transactions the user can see, and every rule is a threshold you can read below. A
forecast the user cannot interrogate is worse than no forecast: the point is to be
*right about the mechanism*, not to look clever.

Nothing here touches the network, the database or the filesystem — it takes plain rows
and returns plain dicts, which is what makes it testable (see ``tests/test_insights.py``).

The two features share one foundation:

* ``detect_recurring`` groups transactions into series that repeat on a schedule;
* ``forecast`` projects a daily balance by applying those series plus observed
  day-to-day spending;
* ``affordability`` re-runs that forecast with one hypothetical purchase added.
"""

from __future__ import annotations

import re
import statistics
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

# --------------------------------------------------------------------- cadence table

#: (name, low, high, nominal) — an interval bucket, in days. The nominal value is what we
#: would expect *a priori*; the detected median interval is what we actually project
#: with, because banks post on business days and a 30-day median drifts.
CADENCES = (
    ("weekly", 6, 8, 7),
    ("fortnightly", 12, 16, 14),
    ("monthly", 26, 35, 30),
    ("quarterly", 80, 100, 91),
    ("annual", 350, 380, 365),
)

#: Below this much observed history a projection is guesswork dressed as arithmetic, so
#: the app declines to answer rather than answering badly. Users met the empty state and
#: import more; they do not get a confident wrong number.
MIN_HISTORY_DAYS = 14

MIN_INTERVAL_SUPPORT = 0.60  # share of gaps that must fall inside the bucket
MIN_AMOUNT_STABILITY = 0.80  # share of amounts within ±AMOUNT_TOLERANCE of the median
AMOUNT_TOLERANCE = Decimal("0.20")
MISSED_GRACE_MIN_DAYS = 7  # a series is "missed" only after this much lateness
MISSED_GRACE_FRACTION = 0.25  # ...or a quarter of its cadence, whichever is larger

DEFAULT_HORIZON_DAYS = 45
MAX_HORIZON_DAYS = 180
MIN_HORIZON_DAYS = 7

# Tokens that describe *how* money moved, not *who* received it. Dropping them is what
# makes "GIRO RENT NOV" and "GIRO RENT DEC" one series instead of two.
_NOISE_TOKENS = frozenset(
    """
    pos paymt payment txn trans transaction trf transfer paynow paylah fast giro nets
    card debit credit atm cash withdrawal deposit purchase sale ecommerce ecomm
    visa mastercard amex nets sg sgd pte ltd com www http https ref otp approval
    """.split()
)

MAX_KEY_TOKENS = 4

_CENTS = Decimal("0.01")


# ------------------------------------------------------------------------- helpers


def money(value: Decimal | int | float | str) -> str:
    """Render an amount with two decimals, as a plain string (no exponent, no grouping)."""
    return format(Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP), "f")


def merchant_key(description: str) -> str:
    """Collapse a printed payee line to a stable key.

    ``FAIRPRICE FINEST #0123 SG`` and ``FAIRPRICE FINEST 0456`` both become
    ``fairprice finest``, while ``GIRO TAX`` and ``GIRO RENT`` stay apart. Digits are
    dropped from multi-digit tokens because they are references, store numbers and card
    fragments — never the identity of the merchant.
    """
    text = re.sub(r"[^a-z0-9]+", " ", (description or "").lower())
    tokens: list[str] = []
    for token in text.split():
        if not any(ch.isalpha() for ch in token):
            continue  # "0123", "1234"
        if sum(ch.isdigit() for ch in token) >= 2:
            continue  # "AB1234", "S1234567D"
        if token in _NOISE_TOKENS:
            continue
        tokens.append(token)
        if len(tokens) == MAX_KEY_TOKENS:
            break
    return " ".join(tokens)


def _title(key: str, fallback: str) -> str:
    """A human label for a series: the key, title-cased, else the printed description."""
    source = key or fallback or ""
    return source.strip().title()[:120] or "Unknown"


def _bucket(interval_days: float) -> tuple[str, int, int] | None:
    for name, low, high, nominal in CADENCES:
        if low <= interval_days <= high:
            return name, low, high
    return None


def _median_int(values: list[int]) -> int:
    return int(round(statistics.median(values)))


def parse_day(value: object) -> date | None:
    """Coerce a row's date to a ``date``. Accepts str, date or datetime; None if unusable."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
            try:
                from datetime import datetime

                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return None


def _amount(value: object) -> Decimal:
    raw = str(value).replace(",", "").replace("$", "").replace("−", "-").replace("–", "-")
    return Decimal(raw.strip()).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def normalize_rows(rows: list[dict]) -> list[dict]:
    """Keep only rows usable for analysis, sorted oldest first.

    A row without a date cannot be placed on a timeline, so it is dropped here — the
    importer reports those to the user instead of storing them.
    """
    out = []
    for row in rows:
        day = parse_day(row.get("date") or row.get("tx_date"))
        if day is None:
            continue
        try:
            amount = _amount(row.get("amount"))
        except Exception:  # noqa: BLE001 - an unparseable amount cannot be analysed
            continue
        if amount == 0:
            continue
        out.append(
            {
                "date": day,
                "description": str(row.get("description") or "").strip(),
                "amount": amount,
                "currency": (str(row.get("currency") or "SGD").strip() or "SGD").upper()[:3],
            }
        )
    out.sort(key=lambda r: r["date"])
    return out


# ------------------------------------------------------------------------- detection


def detect_recurring(rows: list[dict], as_of: date | None = None) -> list[dict]:
    """Find series of transactions that repeat on a schedule.

    Groups by (merchant key, direction) rather than by description, because a bank
    reprints the same payee differently every month ("NETFLIX.COM SG", "NETFLIX.COM
    #0012"). Two occurrences sharing a merchant key but nothing else look like a series
    and aren't — the interval and amount gates below are what reject them.
    """
    today = as_of or date.today()
    rows = normalize_rows(rows)
    groups: dict[tuple[str, int], list[dict]] = {}
    for row in rows:
        key = merchant_key(row["description"])
        if not key:
            continue
        groups.setdefault((key, 1 if row["amount"] > 0 else -1), []).append(row)

    series: list[dict] = []
    for (key, direction), items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda r: r["date"])
        # Collapse same-day repeats to one occurrence. A bill posts once per period; two
        # charges on one day are two purchases. Without this, a same-day pair measures a
        # zero-day gap and drags the median (and so the cadence) toward nothing.
        deduped: list[dict] = []
        for item in items:
            if deduped and deduped[-1]["date"] == item["date"]:
                deduped[-1] = item
            else:
                deduped.append(item)
        items = deduped
        if len(items) < 2:
            continue
        gaps = [
            (items[i]["date"] - items[i - 1]["date"]).days for i in range(1, len(items))
        ]
        positive = [g for g in gaps if g > 0]
        if len(positive) < 1:
            continue  # every occurrence on one day: a burst, not a rhythm

        median_gap = _median_int(positive)
        bucket = _bucket(median_gap)
        if bucket is None:
            continue
        cadence, low, high = bucket
        support = sum(1 for g in positive if low <= g <= high) / len(positive)
        if support < MIN_INTERVAL_SUPPORT:
            continue

        magnitudes = [abs(item["amount"]) for item in items]
        median_amount = statistics.median(magnitudes)
        if median_amount == 0:
            continue
        tolerance = median_amount * AMOUNT_TOLERANCE
        stability = sum(
            1 for value in magnitudes if abs(value - median_amount) <= tolerance
        ) / len(magnitudes)
        if stability < MIN_AMOUNT_STABILITY:
            continue

        last = items[-1]["date"]
        nxt = last + timedelta(days=median_gap)
        days_until = (nxt - today).days
        grace = max(MISSED_GRACE_MIN_DAYS, int(median_gap * MISSED_GRACE_FRACTION))
        missed = days_until < -grace

        spread = max(positive) - min(positive)
        amount_spread = float(
            (max(magnitudes) - min(magnitudes)) / median_amount if median_amount else 1
        )
        if len(items) >= 3 and spread <= 3 and amount_spread <= 0.10:
            confidence = "high"
        elif len(items) >= 3:
            confidence = "medium"
        else:
            confidence = "low"

        if days_until < 0:
            status = "overdue"
        elif days_until <= 7:
            status = "due"
        else:
            status = "upcoming"

        series.append(
            {
                "key": key,
                "title": _title(key, items[-1]["description"]),
                "direction": "credit" if direction > 0 else "debit",
                "cadence": cadence,
                "interval_days": median_gap,
                "amount": money(median_amount if direction > 0 else -median_amount),
                "occurrences": len(items),
                "last_seen": last.isoformat(),
                "next_expected": nxt.isoformat(),
                "days_until": days_until,
                "status": status,
                "missed": missed,
                "confidence": confidence,
                "currency": items[-1]["currency"],
                # Private fields for the forecast pass; stripped before serialization.
                "_last_date": last,
                "_next_date": nxt,
                "_signed": median_amount if direction > 0 else -median_amount,
            }
        )

    # Soonest first; overdue items lead because they are the ones needing attention.
    series.sort(key=lambda s: (s["last_seen"]), reverse=False)
    series.sort(key=lambda s: s["days_until"])
    return series


def public_series(series: list[dict]) -> list[dict]:
    return [{k: v for k, v in s.items() if not k.startswith("_")} for s in series]


# -------------------------------------------------------------------------- forecast


def _months(days: float) -> float:
    return max(days, 1) / 30.4375


def summarize(rows: list[dict], recurring: list[dict]) -> dict:
    """Monthly income / spend / net, plus the variable daily burn used by the forecast.

    The burn excludes recurring rows on purpose: those are projected individually on
    their own dates, and counting them twice would double the forecast's spending.

    It also counts **outflows only**. Spreading one-off income into a daily "burn"
    would push the burn negative and make the forecast climb forever — which, in an
    affordability check, is the most dangerous direction to be wrong in. Income that is
    not detected as recurring is simply not assumed to arrive again.
    """
    if not rows:
        return {
            "monthly_income": "0.00",
            "monthly_spend": "0.00",
            "monthly_net": "0.00",
            "daily_burn": "0.00",
            "months": 0,
        }
    span = max((rows[-1]["date"] - rows[0]["date"]).days, 1)
    months = _months(span)

    income = sum((r["amount"] for r in rows if r["amount"] > 0), Decimal("0"))
    spend = sum((-r["amount"] for r in rows if r["amount"] < 0), Decimal("0"))

    recurring_keys = {(s["key"], 1 if s["direction"] == "credit" else -1) for s in recurring}
    variable_spend = sum(
        (
            -r["amount"]
            for r in rows
            if r["amount"] < 0
            and (merchant_key(r["description"]), -1) not in recurring_keys
        ),
        Decimal("0"),
    )
    # Outflows only — see the docstring. Non-recurring income is deliberately ignored
    # rather than averaged, so the projection can only be too pessimistic, never too
    # optimistic about money arriving.
    burn = variable_spend / Decimal(str(span))

    return {
        "monthly_income": money(income / Decimal(str(months))),
        "monthly_spend": money(spend / Decimal(str(months))),
        "monthly_net": money((income - spend) / Decimal(str(months))),
        "daily_burn": money(burn),
        "months": int(round(months)),
    }


def _occurrence_dates(item: dict, start: date, end: date) -> list[date]:
    """Dates a series is expected to land between ``start`` and ``end`` (inclusive)."""
    interval = max(int(item.get("interval_days") or 0), 1)
    nxt = item.get("_next_date") or item.get("next_expected")
    if not isinstance(nxt, date):
        nxt = parse_day(nxt)
    if nxt is None:
        return []
    if nxt < start:
        # Overdue: the money has not left yet, so assume it leaves shortly rather than
        # pretending it never will. Capped at a week so a stale annual series does not
        # fire at the far end of the window.
        nxt = start + timedelta(days=min(7, max(1, interval // 7)))
    out: list[date] = []
    guard = 0
    while nxt <= end and guard < 400:
        out.append(nxt)
        nxt = nxt + timedelta(days=interval)
        guard += 1
    return out


def forecast(
    rows: list[dict],
    recurring: list[dict],
    start_balance: Decimal | None,
    as_of: date | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    planned: list[dict] | None = None,
    currency: str = "SGD",
) -> dict:
    """Project a daily balance.

    ``planned`` is a list of ``{"date": date, "amount": Decimal}`` one-off events layered
    on top of history — this is how the affordability check works, by running the same
    forecast with the purchase inserted.

    When ``start_balance`` is None the projection starts at zero and the caller must
    present it as *change* rather than a balance; the assumptions say so explicitly.
    """
    today = as_of or date.today()
    horizon = max(MIN_HORIZON_DAYS, min(int(horizon_days or DEFAULT_HORIZON_DAYS), MAX_HORIZON_DAYS))
    end = today + timedelta(days=horizon)

    # Callers hand us whatever `detect_recurring` was given, which may be raw string
    # dates from the database. Normalize here rather than trusting the caller.
    rows = normalize_rows(rows)
    summary = summarize(rows, recurring)
    burn = Decimal(summary["daily_burn"])

    events: dict[date, Decimal] = {}
    for item in recurring:
        for when in _occurrence_dates(item, today, end):
            events[when] = events.get(when, Decimal("0")) + item["_signed"]
    for item in planned or []:
        when = item["date"]
        if today <= when <= end:
            events[when] = events.get(when, Decimal("0")) + Decimal(str(item["amount"]))

    balance = Decimal("0") if start_balance is None else Decimal(str(start_balance))
    points = [{"date": today.isoformat(), "balance": money(balance)}]
    low_date, low_balance = today, balance
    day = today
    while day < end:
        day = day + timedelta(days=1)
        balance = balance + events.get(day, Decimal("0")) - burn
        points.append({"date": day.isoformat(), "balance": money(balance)})
        if balance < low_balance:
            low_date, low_balance = day, balance

    assumptions = [
        f"Recurring items are projected on their own dates; other spending is averaged at "
        f"{money(burn)} a day, based on your last {summary['months']} month(s) of history.",
        "Only transactions already imported or applied are modelled. Anything you pay "
        "without recording it here is not included.",
    ]
    if start_balance is None:
        assumptions.insert(
            0,
            "No balance was supplied, so this shows the expected change from today, "
            "not your absolute balance.",
        )
    overdue = [s for s in recurring if s.get("days_until", 0) < 0]
    if overdue:
        names = ", ".join(s["title"] for s in overdue[:4])
        assumptions.insert(
            1,
            f"{len(overdue)} recurring item(s) are overdue and were projected to land "
            f"within a week rather than on their original date ({names}).",
        )

    return {
        "start": today.isoformat(),
        "end": end.isoformat(),
        "horizon_days": horizon,
        "currency": currency,
        "start_balance": None if start_balance is None else money(start_balance),
        "low": {"date": low_date.isoformat(), "balance": money(low_balance)},
        "end_balance": money(balance),
        "points": points,
        "assumptions": assumptions,
    }


def confidence_for(rows: list[dict], recurring: list[dict], has_balance: bool) -> tuple[str, list[str]]:
    """A confidence level plus the reasons behind it, so it is never a bare label."""
    reasons: list[str] = []
    if not rows:
        return "low", ["There is no transaction history yet."]
    span = max((rows[-1]["date"] - rows[0]["date"]).days, 1)
    months = _months(span)
    strong = [s for s in recurring if s["confidence"] == "high"]

    if span < 30:
        reasons.append(f"Only {span} days of history — a month or more is needed to see a rhythm.")
    if months < 3:
        reasons.append("Less than three months of history.")
    if not has_balance:
        reasons.append("No starting balance was supplied.")
    if not strong:
        reasons.append("No recurring series seen often enough to be sure of.")

    if span >= 90 and months >= 3 and has_balance and len(strong) >= 2:
        level = "high"
        reasons = reasons or ["Three or more months of history, a starting balance, and "
                              f"{len(strong)} well-established recurring series."]
    elif span >= 30 and (has_balance or strong):
        level = "medium"
    else:
        level = "low"
    return level, reasons


# ------------------------------------------------------------------- affordability


def affordability(
    rows: list[dict],
    recurring: list[dict],
    start_balance: Decimal | None,
    amount: Decimal,
    when: date,
    as_of: date | None = None,
    cushion: Decimal = Decimal("0"),
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    currency: str = "SGD",
) -> dict:
    """Answer "can I afford this?" by re-running the forecast with the purchase added.

    A single projection cannot answer it, because the answer depends on whether money
    runs out *before* the purchase. So we compute the plan and read the low point of the
    planned curve: if it dips below zero the purchase (or something before it) cannot be
    met; if it dips below the cushion, the purchase fits but leaves nothing spare.
    """
    today = as_of or date.today()
    horizon = max(MIN_HORIZON_DAYS, min(int(horizon_days or DEFAULT_HORIZON_DAYS), MAX_HORIZON_DAYS))
    end = today + timedelta(days=horizon)
    # Without a starting balance the projection begins at zero, so every verdict would
    # read "below zero" — technically consistent and completely useless. The plain
    # forecast can show *change* without a balance; a yes/no cannot.
    if start_balance is None:
        raise ValueError(
            "Enter your current balance first — whether you can afford something depends "
            "on what is already in the account."
        )
    if when < today:
        raise ValueError("Pick a date in the future — a past date cannot be planned for.")
    if when > end:
        raise ValueError(
            f"That date is beyond the {horizon}-day forecast window. Pick a date before "
            f"{end.isoformat()} or raise the horizon."
        )

    plan = forecast(
        rows,
        recurring,
        start_balance,
        as_of=today,
        horizon_days=horizon,
        planned=[{"date": when, "amount": -abs(amount)}],
        currency=currency,
    )

    low = Decimal(plan["low"]["balance"])
    if low < 0:
        verdict = "no"
    elif low - cushion < 0:
        verdict = "tight"
    else:
        verdict = "yes"

    low_date = plan["low"]["date"]
    if verdict == "no":
        reason = (
            f"Adding {money(amount)} on {when.isoformat()} takes your projected balance "
            f"to {money(low)} on {low_date} — it would go below zero."
        )
    elif verdict == "tight":
        reason = (
            f"It fits, but the lowest you get is {money(low)} on {low_date}, inside the "
            f"{money(cushion)} cushion you asked to keep."
        )
    else:
        reason = (
            f"It fits. The lowest your balance gets is {money(low)} on {low_date}, and you "
            f"end the window at {money(plan['end_balance'])}."
        )
    return {
        "verdict": verdict,
        "reason": reason,
        "amount": money(amount),
        "date": when.isoformat(),
        "cushion": money(cushion),
        "currency": currency,
        "low": plan["low"],
        "end_balance": plan["end_balance"],
        "start_balance": plan["start_balance"],
        "forecast": plan,
    }


# ------------------------------------------------------------------------ headline


def insights(
    rows: list[dict],
    balance: Decimal | None,
    currency: str = "SGD",
    as_of: date | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> dict:
    """Everything the Insights tab needs, in one pass."""
    today = as_of or date.today()
    rows = normalize_rows([r for r in rows if r.get("currency", currency) == currency])
    recurring = detect_recurring(rows, as_of=today)

    warnings: list[str] = []
    if not rows:
        return {
            "ready": False,
            "currency": currency,
            "reason": (
                "There is no transaction history yet. Import a CSV of past transactions, "
                "or apply a few screenshots, and this fills in."
            ),
            "recurring": [],
            "summary": summarize([], []),
            "forecast": None,
            "warnings": [],
        }

    span = max((rows[-1]["date"] - rows[0]["date"]).days, 1)
    if span < MIN_HISTORY_DAYS:
        return {
            "ready": False,
            "currency": currency,
            "reason": (
                f"That history covers {span} day(s). A forecast needs at least "
                f"{MIN_HISTORY_DAYS} days of transactions to see a spending rhythm."
            ),
            "recurring": public_series(recurring),
            "summary": summarize(rows, recurring),
            "forecast": None,
            "warnings": [],
        }
    if span < 30:
        warnings.append(
            f"Only {span} days of history. Recurring items need at least two occurrences "
            "a schedule apart, so the list will be sparse until you import more."
        )

    level, reasons = confidence_for(rows, recurring, balance is not None)
    projection = forecast(
        rows, recurring, balance, as_of=today, horizon_days=horizon_days, currency=currency
    )
    projection["confidence"] = level
    projection["confidence_reasons"] = reasons

    missed = [s for s in recurring if s["missed"]]
    if missed:
        warnings.append(
            "Expected but not seen: "
            + ", ".join(s["title"] for s in missed[:5])
            + ". They are included in the forecast as still to come."
        )

    return {
        "ready": True,
        "currency": currency,
        "coverage": {
            "first": rows[0]["date"].isoformat(),
            "last": rows[-1]["date"].isoformat(),
            "days": span,
            "transactions": len(rows),
        },
        "balance": {
            "supplied": balance is not None,
            "amount": None if balance is None else money(balance),
        },
        "recurring": public_series(recurring),
        "summary": summarize(rows, recurring),
        "forecast": projection,
        "warnings": warnings,
    }