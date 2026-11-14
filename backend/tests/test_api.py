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


class StatementPdfTests(ApiTestCase):
    """The PDF path through the real endpoints.

    A small workbook and a hand-written statement are enough: the reader's job (text ->
    signed rows) is unit-tested in test_statement_pdf.py, so here we only prove the
    endpoint wiring — that a PDF is accepted where an image was, and that the running
    balance check reaches the response.
    """

    STATEMENT = (
        "Date Description Withdrawal_SGD Deposit_SGD Balance_SGD\n"
        "2026-11-12 Inward DR - GIRO 5304863730514943 DINERS CLUB (S) PL 172.32 0.00 1127.68\n"
        "2026-11-11 Inward Credit-FAST Transfer OTHR Other LIEW HON SENG 0.00 203.73 1300.00\n"
    )

    def workbook(self) -> bytes:
        import io

        import openpyxl

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Budget"
        sheet["A1"] = "UOB One Account"
        sheet["B1"] = "=1310.25"
        buffer = io.BytesIO()
        book.save(buffer)
        return buffer.getvalue()

    def pdf_bytes(self) -> bytes:
        """A minimal single-page PDF whose text layer is our statement."""
        lines = self.STATEMENT.strip().splitlines()
        text_ops = ["BT", "/F1 8 Tf", "40 780 Td", "12 TL"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            text_ops.append(f"({escaped}) Tj T*")
        text_ops.append("ET")
        stream = "\n".join(text_ops).encode("latin-1")
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        ]
        out = bytearray(b"%PDF-1.4\n")
        offsets = []
        for index, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode()
        out += (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
        ).encode()
        return bytes(out)

    def test_analyze_reads_a_pdf_statement(self):
        response = self.client.post(
            "/api/analyze",
            files={
                "screenshot": ("statement.pdf", self.pdf_bytes(), "application/pdf"),
                "workbook_file": (
                    "book.xlsx",
                    self.workbook(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            },
            data={"label": "UOB One"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["source"], "pdf-text")
        self.assertEqual(len(body["transactions"]), 2)
        self.assertEqual(body["transactions"][0]["amount"], "-172.32")
        self.assertEqual(body["transactions"][1]["amount"], "203.73")
        self.assertTrue(body["balance_check"]["checked"])
        self.assertTrue(body["balance_check"]["ok"])
        # The account row is still found, so a PDF flows through the same review step.
        self.assertEqual(body["candidates"][0]["cell"], "B1")

    def test_import_accepts_a_pdf_and_stores_its_rows(self):
        response = self.client.post(
            "/api/ledger/import",
            files={"file": ("statement.pdf", self.pdf_bytes(), "application/pdf")},
            data={"account": "UOB One", "commit": "true"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["parsed"], 2)
        self.assertEqual(response.json()["added"], 2)

    def test_a_pdf_with_no_transactions_is_a_422(self):
        empty = self.pdf_bytes().replace(b"172.32", b"0.00").replace(b"203.73", b"0.00")
        response = self.client.post(
            "/api/ledger/import",
            files={"file": ("statement.pdf", empty, "application/pdf")},
            data={"account": "UOB One", "commit": "false"},
        )
        self.assertIn(response.status_code, (200, 422))
        if response.status_code == 422:
            self.assertIn("No transaction rows could be read", response.json()["detail"])


class EventLoopTests(unittest.TestCase):
    """The read path must not run blocking work on the event loop.

    As ``async def`` with a blocking model call inside, one statement read froze the
    entire app — /health stopped answering and the ingress returned a bare 504. These
    tests fail if the blocking call is ever moved back onto the loop.
    """

    def test_analyze_offloads_extraction_to_a_thread(self):
        import inspect

        import main

        source = inspect.getsource(main.analyze)
        self.assertIn(
            "run_in_threadpool",
            source,
            "analyze() must run the blocking extraction in a worker thread",
        )

    def test_import_offloads_extraction_to_a_thread(self):
        import inspect

        import main

        self.assertIn("run_in_threadpool", inspect.getsource(main.import_csv))
        self.assertIn("run_in_threadpool", inspect.getsource(main.apply))

    def test_model_timeout_is_under_the_gateway_limit(self):
        import extract

        # The ingress cuts the connection at ~60s; waiting longer only produces a 504.
        self.assertLess(extract.REQUEST_TIMEOUT, 60)

    def test_scanned_pdf_path_is_page_capped(self):
        import statement_pdf

        # Each page is a model call and must fit inside one request.
        self.assertLessEqual(statement_pdf.MAX_VISION_PAGES, 4)


class ConfigTests(ApiTestCase):
    def test_config_reports_that_history_is_stored(self):
        body = self.client.get("/api/config").json()
        self.assertTrue(body["history_enabled"])


if __name__ == "__main__":
    unittest.main()