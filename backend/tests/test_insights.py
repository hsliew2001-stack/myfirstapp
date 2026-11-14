"""Tests for the recurring/forecast maths. No database, no network — plain rows in.

Run with ``cd backend && python -m unittest discover -s tests -v``.

These encode the acceptance criteria in docs/PRD-recurring-forecast.md, so a change to a
threshold shows up here as a failing expectation rather than a quietly worse app.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import insights  # noqa: E402

AS_OF = date(2026, 11, 14)


def tx(day: str, description: str, amount: str, currency: str = "SGD") -> dict:
    return {"date": day, "description": description, "amount": amount, "currency": currency}


def monthly(description: str, amount: str, days: list[str]) -> list[dict]:
    return [tx(day, description, amount) for day in days]


class MerchantKeyTests(unittest.TestCase):
    def test_reference_numbers_and_rails_are_folded_away(self):
        self.assertEqual(
            insights.merchant_key("FAIRPRICE FINEST #0123 SG"),
            insights.merchant_key("FAIRPRICE FINEST 0456"),
        )

    def test_payment_rail_noise_does_not_merge_different_payees(self):
        self.assertNotEqual(
            insights.merchant_key("GIRO TAX PAYMENT"), insights.merchant_key("GIRO RENT")
        )

    def test_short_digit_tokens_survive_when_they_carry_a_letter(self):
        self.assertEqual(insights.merchant_key("UOB One Account"), "uob one account")


class DetectionTests(unittest.TestCase):
    def test_monthly_subscription_is_detected(self):
        rows = monthly(
            "NETFLIX.COM #0012",
            "-17.98",
            ["2026-06-21", "2026-07-21", "2026-08-21", "2026-09-21", "2026-10-21"],
        )
        series = insights.detect_recurring(rows, as_of=AS_OF)
        self.assertEqual(len(series), 1)
        item = series[0]
        self.assertEqual(item["cadence"], "monthly")
        self.assertEqual(item["amount"], "-17.98")
        self.assertEqual(item["occurrences"], 5)
        self.assertEqual(item["last_seen"], "2026-10-21")
        self.assertEqual(item["next_expected"], "2026-11-20")
        self.assertEqual(item["days_until"], 6)
        self.assertEqual(item["status"], "due")
        self.assertFalse(item["missed"])
        self.assertEqual(item["confidence"], "high")

    def test_two_unrelated_purchases_at_the_same_merchant_are_not_a_series(self):
        # Same merchant, but three days apart and very different amounts: a shopper,
        # not a subscription. Both gates must reject it.
        rows = monthly(
            "COLD STORAGE",
            "-30.00",
            ["2026-11-01", "2026-11-04"],
        ) + monthly("COLD STORAGE", "-110.00", ["2026-11-08"])
        self.assertEqual(insights.detect_recurring(rows, as_of=AS_OF), [])

    def test_amount_instability_rejects_the_series(self):
        rows = [
            tx("2026-09-01", "SOME MERCHANT", "-10.00"),
            tx("2026-10-01", "SOME MERCHANT", "-90.00"),
        ]
        self.assertEqual(insights.detect_recurring(rows, as_of=AS_OF), [])

    def test_irregular_intervals_reject_the_series(self):
        rows = [
            tx("2026-06-01", "RANDOM SHOP", "-20.00"),
            tx("2026-08-15", "RANDOM SHOP", "-20.00"),  # 75 days: no bucket
        ]
        self.assertEqual(insights.detect_recurring(rows, as_of=AS_OF), [])

    def test_single_occurrence_is_not_a_series(self):
        rows = [tx("2026-10-01", "ONE OFF", "-20.00")]
        self.assertEqual(insights.detect_recurring(rows, as_of=AS_OF), [])

    def test_income_is_detected_separately_from_spending(self):
        rows = [
            tx("2026-09-25", "ACME PTE LTD SALARY", "5200.00"),
            tx("2026-10-25", "ACME PTE LTD SALARY", "5200.00"),
            tx("2026-11-25", "ACME PTE LTD SALARY", "5200.00"),
        ]
        series = insights.detect_recurring(rows, as_of=AS_OF)
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["direction"], "credit")
        self.assertEqual(series[0]["amount"], "5200.00")

    def test_same_merchant_debit_and_credit_are_two_series(self):
        rows = [
            tx("2026-10-01", "PAYNOW JANE", "-50.00"),
            tx("2026-11-01", "PAYNOW JANE", "-50.00"),
            tx("2026-10-02", "PAYNOW JANE", "50.00"),
            tx("2026-11-02", "PAYNOW JANE", "50.00"),
        ]
        directions = {s["direction"] for s in insights.detect_recurring(rows, as_of=AS_OF)}
        self.assertEqual(directions, {"debit", "credit"})

    def test_missing_charge_is_flagged_missed_but_still_projected(self):
        # Last charge 21 Sep, monthly cadence, so 21 Oct was expected. As of 14 Nov that is
        # 23 days late — well past the max(7d, 25% of 31d) ≈ 8-day grace — so it is missed.
        rows = monthly(
            "GYM MEMBERSHIP", "-80.00", ["2026-06-21", "2026-07-21", "2026-08-21", "2026-09-21"]
        )
        item = insights.detect_recurring(rows, as_of=AS_OF)[0]
        self.assertEqual(item["status"], "overdue")
        self.assertTrue(item["missed"])
        # It has still not been charged, so the forecast must still spend the money.
        plan = insights.forecast(rows, [item], Decimal("1000.00"), as_of=AS_OF, horizon_days=30)
        self.assertLess(Decimal(plan["end_balance"]), Decimal("1000.00"))

    def test_series_just_past_its_date_is_not_called_missed(self):
        # Due 13 Nov, seen on 14 Nov: one day late is normal posting lag, not a miss.
        rows = monthly(
            "SPOTIFY", "-10.90",
            ["2026-06-13", "2026-07-13", "2026-08-13", "2026-09-13", "2026-10-13"],
        )
        item = insights.detect_recurring(rows, as_of=AS_OF)[0]
        self.assertEqual(item["status"], "overdue")
        self.assertFalse(item["missed"])


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.rows = insights.normalize_rows(
            monthly(
                "NETFLIX.COM",
                "-17.98",
                ["2026-06-21", "2026-07-21", "2026-08-21", "2026-09-21", "2026-10-21"],
            )
            + [tx("2026-11-02", "GROCERY RUN", "-40.00"), tx("2026-11-09", "GROCERY RUN", "-40.00")]
        )
        self.recurring = insights.detect_recurring(self.rows, as_of=AS_OF)

    def test_projection_covers_every_day_inclusive(self):
        plan = insights.forecast(
            self.rows, self.recurring, Decimal("1000.00"), as_of=AS_OF, horizon_days=45
        )
        self.assertEqual(len(plan["points"]), 46)
        self.assertEqual(plan["points"][0]["date"], "2026-11-14")
        self.assertEqual(plan["points"][-1]["date"], "2026-12-29")

    def test_recurring_charge_lands_on_its_expected_date(self):
        plan = insights.forecast(
            self.rows, self.recurring, Decimal("1000.00"), as_of=AS_OF, horizon_days=30
        )
        points = {p["date"]: Decimal(p["balance"]) for p in plan["points"]}
        # 20 Nov is the expected date, so the drop lands *on* it; the 19th must not already
        # have been charged (the forecast must not pull the due date earlier).
        self.assertEqual(points["2026-11-19"] - points["2026-11-20"], Decimal("17.98"))
        # The low point is the *last* time the balance is that low, not the first: the
        # weekly grocery series seen on 2 and 9 Nov is projected forward too, and each of
        # those is a deeper dip. What matters is that the balance is down, not up.
        self.assertLess(Decimal(plan["low"]["balance"]), Decimal("1000.00"))
        self.assertLessEqual(plan["low"]["date"], "2026-12-14")

    def test_planned_purchase_shifts_the_low_point(self):
        base = insights.forecast(
            self.rows, self.recurring, Decimal("1000.00"), as_of=AS_OF, horizon_days=30
        )
        with_purchase = insights.forecast(
            self.rows,
            self.recurring,
            Decimal("1000.00"),
            as_of=AS_OF,
            horizon_days=30,
            planned=[{"date": date(2026, 11, 20), "amount": Decimal("-50.00")}],
        )
        self.assertEqual(
            Decimal(base["low"]["balance"]) - Decimal(with_purchase["low"]["balance"]),
            Decimal("50.00"),
        )

    def test_missing_balance_is_announced_not_assumed(self):
        plan = insights.forecast(self.rows, self.recurring, None, as_of=AS_OF)
        self.assertIsNone(plan["start_balance"])
        self.assertTrue(any("No balance was supplied" in a for a in plan["assumptions"]))

    def test_daily_burn_excludes_recurring_spend(self):
        summary = insights.summarize(self.rows, self.recurring)
        # 80.00 of the 80.00 non-recurring spend over ~144 days is ~0.56/day; the
        # Netflix charges must not inflate it.
        self.assertLess(Decimal(summary["daily_burn"]), Decimal("1.00"))


class AffordabilityTests(unittest.TestCase):
    def setUp(self):
        # Six months of a realistic pattern: rent on the 1st, salary on the 25th, and a
        # little daily spending — enough history for a projection to mean something.
        rows = []
        for month in range(6, 12):
            rows.append(tx(f"2026-{month:02d}-01", "RENT PAYMENT", "-2000.00"))
            if month < 11:
                rows.append(tx(f"2026-{month:02d}-25", "ACME PTE LTD SALARY", "5000.00"))
            for n in (5, 12, 19, 26):
                rows.append(tx(f"2026-{month:02d}-{n:02d}", "KOPITIAM", "-8.50"))
        self.rows = insights.normalize_rows(rows)
        self.recurring = insights.detect_recurring(self.rows, as_of=AS_OF)

    def test_affordable_purchase_says_yes(self):
        result = insights.affordability(
            self.rows, self.recurring, Decimal("5000.00"), Decimal("200.00"),
            date(2026, 11, 20), as_of=AS_OF,
        )
        self.assertEqual(result["verdict"], "yes")
        self.assertIn("It fits", result["reason"])

    def test_verdict_does_not_depend_only_on_start_balance(self):
        # No income in this history, so nothing refills the balance. 2500.00 covers a
        # 1000.00 purchase today — a day-zero check says yes — but rent lands on 2 Dec
        # first, leaving 500.00. Judged on the low point, it does not fit.
        rows = insights.normalize_rows(
            [tx(f"2026-{m:02d}-01", "RENT PAYMENT", "-2000.00") for m in range(6, 12)]
            + [
                tx(f"2026-{m:02d}-{n:02d}", "KOPITIAM", "-8.50")
                for m in range(6, 12)
                for n in (5, 12, 19, 26)
            ]
        )
        recurring = insights.detect_recurring(rows, as_of=AS_OF)
        result = insights.affordability(
            rows, recurring, Decimal("2500.00"), Decimal("1000.00"),
            date(2026, 12, 5), as_of=AS_OF,
        )
        self.assertEqual(result["verdict"], "no")
        self.assertIn("below zero", result["reason"])

    def test_cushion_turns_yes_into_tight(self):
        plain = insights.affordability(
            self.rows, self.recurring, Decimal("5000.00"), Decimal("10.00"),
            date(2026, 11, 15), as_of=AS_OF,
        )
        self.assertEqual(plain["verdict"], "yes")
        cushioned = insights.affordability(
            self.rows, self.recurring, Decimal("5000.00"), Decimal("10.00"),
            date(2026, 11, 15), as_of=AS_OF, cushion=Decimal("100000.00"),
        )
        self.assertEqual(cushioned["verdict"], "tight")

    def test_cushion_of_zero_leaves_the_yes_alone(self):
        result = insights.affordability(
            self.rows, self.recurring, Decimal("5000.00"), Decimal("10.00"),
            date(2026, 11, 15), as_of=AS_OF, cushion=Decimal("0"),
        )
        self.assertEqual(result["verdict"], "yes")

    def test_past_date_is_rejected(self):
        with self.assertRaises(ValueError):
            insights.affordability(
                self.rows, self.recurring, Decimal("100.00"), Decimal("10.00"),
                date(2026, 11, 1), as_of=AS_OF,
            )

    def test_date_beyond_horizon_is_rejected(self):
        with self.assertRaises(ValueError):
            insights.affordability(
                self.rows, self.recurring, Decimal("100.00"), Decimal("10.00"),
                date(2027, 6, 1), as_of=AS_OF, horizon_days=45,
            )


class InsightsTests(unittest.TestCase):
    def test_empty_history_is_not_ready_and_says_what_to_do(self):
        result = insights.insights([], Decimal("100.00"), as_of=AS_OF)
        self.assertFalse(result["ready"])
        self.assertIn("Import a CSV", result["reason"])

    def test_full_payload_is_serializable_and_hides_internals(self):
        rows = monthly("NETFLIX.COM", "-17.98", ["2026-08-21", "2026-09-21", "2026-10-21"]) + [
            tx(f"2026-11-0{n}", "CAFE", f"-{n}.50") for n in range(1, 6)
        ]
        result = insights.insights(rows, Decimal("1200.00"), as_of=AS_OF)
        self.assertTrue(result["ready"])
        self.assertTrue(result["recurring"])
        self.assertFalse(any(k.startswith("_") for k in result["recurring"][0]))
        self.assertIn("confidence", result["forecast"])
        self.assertIn("coverage", result)

    def test_foreign_currency_rows_are_excluded(self):
        rows = monthly("NETFLIX.COM", "-17.98", ["2026-09-21", "2026-10-21"]) + [
            tx("2026-10-01", "USD THING", "-5.00", currency="USD")
        ]
        result = insights.insights(rows, Decimal("100.00"), currency="SGD", as_of=AS_OF)
        self.assertEqual(result["coverage"]["transactions"], 2)

    def test_short_history_lowers_confidence_and_warns(self):
        rows = monthly("NETFLIX.COM", "-17.98", ["2026-10-21", "2026-11-05"])
        result = insights.insights(rows, Decimal("100.00"), as_of=AS_OF)
        self.assertEqual(result["forecast"]["confidence"], "low")
        self.assertTrue(any("days of history" in w for w in result["warnings"]))


if __name__ == "__main__":
    unittest.main()