"""Endpoint tests for the insights/ledger API.

The ledger is replaced with an in-memory double: this machine has no MySQL/OceanBase
server (and ``docker`` is unavailable), so the SQL itself is verified by inspection
rather than execution. Everything above the SQL — request parsing, scoping by user,
the import preview/commit contract, and the insights/affordability payloads — is
exercised for real through FastAPI's TestClient.

Run with ``cd backend && python -m unittest discover -s tests -v``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


class FakeLedger:
    """The subset of ``ledger.Ledger`` the endpoints use, backed by a dict."""

    def __init__(self) -> None:
        self.rows_by_actor: dict[str, dict[str, dict]] = {}

    def configured(self) -> bool:
        return True

    async def connect(self) -> None:  # pragma: no cover - lifespan is not run here
        pass

    async def close(self) -> None:  # pragma: no cover
        pass

    async def add(self, actor: str, account: str, rows: list[dict], source: str) -> dict:
        import workbook
        from decimal import Decimal

        store = self.rows_by_actor.setdefault(actor, {})
        added = 0
        for row in rows:
            fp = workbook.fingerprint(row["date"], row.get("description", ""), Decimal(row["amount"]))
            if fp in store:
                continue
            store[fp] = {
                "date": row["date"],
                "description": row.get("description", ""),
                "amount": row["amount"],
                "currency": row.get("currency") or "SGD",
                "account": account,
                "source": source,
            }
            added += 1
        return {"added": added, "skipped": len(rows) - added}

    async def rows(self, actor: str, currency=None, account=None, limit=2000) -> list[dict]:
        found = list(self.rows_by_actor.get(actor, {}).values())
        if currency:
            found = [r for r in found if r["currency"] == currency.upper()[:3]]
        if account:
            found = [r for r in found if r["account"] == account]
        found.sort(key=lambda r: r["date"])
        return found[-limit:]

    async def recent(self, actor: str, limit: int = 50) -> list[dict]:
        return list(reversed(await self.rows(actor, limit=limit)))

    async def clear(self, actor: str) -> int:
        return len(self.rows_by_actor.pop(actor, {}))


def csv_history() -> str:
    """Six months: rent, salary, a subscription whose payee text varies, and groceries."""
    lines = ["Date,Description,Amount,Balance"]
    for month in range(6, 12):
        lines.append(f"01/{month:02d}/2026,RENT PAYMENT,-2000.00,0")
        if month < 11:
            lines.append(f"25/{month:02d}/2026,ACME PTE LTD SALARY,5000.00,0")
        lines.append(f"21/{month:02d}/2026,NETFLIX.COM #00{month:02d} SG,-17.98,0")
        for day in (5, 12, 19, 26):
            lines.append(f"{day:02d}/{month:02d}/2026,KOPITIAM,-8.50,0")
    return "\n".join(lines)


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLedger()
        self._real = main.book
        main.book = self.fake
        self.client = TestClient(main.app)

    def tearDown(self):
        main.book = self._real

    def as_user(self, email: str | None):
        return {"x-forwarded-email": email} if email else {}


class ImportTests(ApiTestCase):
    def test_preview_parses_but_stores_nothing(self):
        response = self.client.post(
            "/api/ledger/import",
            data={"text": csv_history(), "account": "UOB One", "commit": "false"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["committed"])
        self.assertEqual(body["parsed"], 41)
        self.assertEqual(body["added"], 41)  # would all be added
        self.assertEqual(body["skipped"], 0)
        self.assertEqual(self.fake.rows_by_actor, {})

    def test_preview_after_commit_reports_nothing_new(self):
        self.client.post(
            "/api/ledger/import",
            data={"text": csv_history(), "account": "UOB One", "commit": "true"},
        )
        body = self.client.post(
            "/api/ledger/import",
            data={"text": csv_history(), "account": "UOB One", "commit": "false"},
        ).json()
        self.assertEqual(body["added"], 0)
        self.assertEqual(body["skipped"], 41)

    def test_commit_stores_rows_and_reimport_is_idempotent(self):
        first = self.client.post(
            "/api/ledger/import",
            data={"text": csv_history(), "account": "UOB One", "commit": "true"},
        )
        self.assertEqual(first.json()["added"], 41)
        second = self.client.post(
            "/api/ledger/import",
            data={"text": csv_history(), "account": "UOB One", "commit": "true"},
        )
        self.assertEqual(second.json()["added"], 0)
        self.assertEqual(second.json()["skipped"], 41)

    def test_bad_rows_are_reported_not_silently_dropped(self):
        text = "Date,Description,Amount\n25/06/2026,COFFEE,-4.50\nnot-a-date,THING,-1.00\n"
        body = self.client.post(
            "/api/ledger/import", data={"text": text, "commit": "false"}
        ).json()
        self.assertEqual(body["parsed"], 1)
        self.assertEqual(len(body["errors"]), 1)
        self.assertIn("Line 3", body["errors"][0])

    def test_missing_date_column_is_a_clear_422(self):
        response = self.client.post(
            "/api/ledger/import", data={"text": "Thing,Amount\nA,1.00\n", "commit": "false"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("date column", response.json()["detail"])

    def test_import_requires_something_to_import(self):
        response = self.client.post("/api/ledger/import", data={"commit": "false"})
        self.assertEqual(response.status_code, 400)

    def test_rows_are_scoped_per_user(self):
        self.client.post(
            "/api/ledger/import",
            headers=self.as_user("a@example.com"),
            data={"text": csv_history(), "account": "UOB One", "commit": "true"},
        )
        mine = self.client.get("/api/ledger", headers=self.as_user("b@example.com")).json()
        self.assertEqual(mine["total"], 0)
        theirs = self.client.get("/api/ledger", headers=self.as_user("a@example.com")).json()
        self.assertEqual(theirs["total"], 41)


class InsightsEndpointTests(ApiTestCase):
    def seed(self, email: str = "a@example.com"):
        self.client.post(
            "/api/ledger/import",
            headers=self.as_user(email),
            data={"text": csv_history(), "account": "UOB One", "commit": "true"},
        )

    def test_no_history_is_not_ready_with_a_reason(self):
        body = self.client.get("/api/insights").json()
        self.assertFalse(body["ready"])
        self.assertIn("Import a CSV", body["reason"])
        self.assertIsNone(body["forecast"])

    def test_detects_the_subscription_and_the_rent(self):
        self.seed()
        body = self.client.get(
            "/api/insights", headers=self.as_user("a@example.com"), params={"balance": "4200"}
        ).json()
        self.assertTrue(body["ready"])
        titles = {item["title"] for item in body["recurring"]}
        self.assertIn("Netflix", titles)
        self.assertIn("Rent", titles)
        netflix = next(i for i in body["recurring"] if i["title"] == "Netflix")
        self.assertEqual(netflix["amount"], "-17.98")
        self.assertEqual(netflix["cadence"], "monthly")
        self.assertNotIn("_signed", netflix)

    def test_forecast_is_returned_with_confidence_and_points(self):
        self.seed()
        body = self.client.get(
            "/api/insights", headers=self.as_user("a@example.com"), params={"balance": "4200"}
        ).json()
        forecast = body["forecast"]
        self.assertEqual(len(forecast["points"]), forecast["horizon_days"] + 1)
        self.assertIn(forecast["confidence"], {"low", "medium", "high"})
        self.assertTrue(forecast["confidence_reasons"])
        self.assertTrue(forecast["assumptions"])
        self.assertEqual(forecast["start_balance"], "4200.00")

    def test_another_user_sees_no_history(self):
        self.seed()
        body = self.client.get("/api/insights", headers=self.as_user("b@example.com")).json()
        self.assertFalse(body["ready"])

    def test_unreadable_balance_is_a_400(self):
        self.seed()
        response = self.client.get(
            "/api/insights", headers=self.as_user("a@example.com"), params={"balance": "lots"}
        )
        self.assertEqual(response.status_code, 400)

    def test_delete_clears_only_my_rows(self):
        self.seed("a@example.com")
        self.seed("b@example.com")
        result = self.client.delete("/api/ledger", headers=self.as_user("a@example.com")).json()
        self.assertEqual(result["deleted"], 41)
        mine = self.client.get("/api/insights", headers=self.as_user("a@example.com")).json()
        self.assertFalse(mine["ready"])
        theirs = self.client.get("/api/insights", headers=self.as_user("b@example.com")).json()
        self.assertTrue(theirs["ready"])


class AffordabilityEndpointTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.client.post(
            "/api/ledger/import",
            headers=self.as_user("a@example.com"),
            data={"text": csv_history(), "account": "UOB One", "commit": "true"},
        )

    def ask(self, **body):
        return self.client.post(
            "/api/affordability", headers=self.as_user("a@example.com"), json=body
        )

    def test_comfortable_purchase_is_a_yes(self):
        response = self.ask(amount="200", date="2026-11-20", balance="4200")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["verdict"], "yes")
        self.assertIn("low", response.json())

    def test_missing_balance_is_rejected(self):
        response = self.ask(amount="100", date="2026-11-20")
        self.assertEqual(response.status_code, 400)
        self.assertIn("balance", response.json()["detail"].lower())

    def test_zero_or_negative_amount_is_rejected(self):
        self.assertEqual(self.ask(amount="0", balance="100").status_code, 400)
        self.assertEqual(self.ask(amount="-50", balance="100").status_code, 400)

    def test_past_date_is_rejected_with_a_reason(self):
        response = self.ask(amount="100", date="2026-01-01", balance="100")
        self.assertEqual(response.status_code, 400)
        self.assertIn("future", response.json()["detail"])

    def test_far_future_date_is_rejected(self):
        response = self.ask(amount="100", date="2030-01-01", balance="100")
        self.assertEqual(response.status_code, 400)
        self.assertIn("horizon", response.json()["detail"])

    def test_huge_purchase_against_a_small_balance_is_a_no(self):
        response = self.ask(amount="9000", date="2026-11-18", balance="500")
        self.assertEqual(response.json()["verdict"], "no")
        self.assertIn("below zero", response.json()["reason"])


class ConfigTests(ApiTestCase):
    def test_config_reports_that_history_is_stored(self):
        body = self.client.get("/api/config").json()
        self.assertTrue(body["history_enabled"])


if __name__ == "__main__":
    unittest.main()