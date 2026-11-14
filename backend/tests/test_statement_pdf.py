"""Tests for the statement-PDF reader.

The fixtures here are text, not PDF bytes: the reader's whole job is turning extracted
statement text into signed transactions, and testing it at that boundary keeps the tests
fast and free of a PDF generator. A real PDF is exercised end-to-end in test_api.py.

Run with ``cd backend && python -m unittest discover -s tests -v``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import statement_pdf  # noqa: E402

HEADER = "Date Description Withdrawal_SGD Deposit_SGD Balance_SGD"

# Two withdrawals and one deposit, with the running balance consistent at each step.
SIMPLE = f"""UOB One Account — Statement
Account no: 012-345-678-9      Period: 01 Nov 2026 - 12 Nov 2026
{HEADER}
2026-11-12 Inward DR - GIRO 5304863730514943 CCRD 824295185025 DINERS CLUB (S) PL 172.32 0.00 1127.68
2026-11-11 Inward Credit-FAST Transfer OTHR Other LIEW HON SENG 0.00 203.73 1300.00
2026-11-10 NETS Debit-Consumer YONG TAU FO13362232 xxxxxx4342 9.90 0.00 1096.27
"""


class ParsingTests(unittest.TestCase):
    def test_withdrawal_column_produces_a_negative_amount(self):
        rows = statement_pdf.parse_statement_text(SIMPLE)["rows"]
        self.assertEqual(rows[0]["amount"], "-172.32")
        self.assertEqual(rows[0]["date"], "2026-11-12")

    def test_deposit_column_produces_a_positive_amount(self):
        rows = statement_pdf.parse_statement_text(SIMPLE)["rows"]
        self.assertEqual(rows[1]["amount"], "203.73")

    def test_rows_keep_the_statement_order(self):
        rows = statement_pdf.parse_statement_text(SIMPLE)["rows"]
        self.assertEqual([row["date"] for row in rows], ["2026-11-12", "2026-11-11", "2026-11-10"])

    def test_header_and_account_lines_are_not_transactions(self):
        rows = statement_pdf.parse_statement_text(SIMPLE)["rows"]
        self.assertEqual(len(rows), 3)

    def test_trailing_balance_is_carried_for_verification(self):
        rows = statement_pdf.parse_statement_text(SIMPLE)["rows"]
        self.assertEqual(rows[0]["balance"], "1127.68")

    def test_wrapped_description_joins_the_previous_row(self):
        text = (
            f"{HEADER}\n"
            "2026-11-12 PAYNOW-FAST OTHR qsb-sqr-sg- 38250221340549 6.80 0.00 1331.41\n"
            "PIB2611122586676639 Qashier-XI AN Noodl\n"
        )
        rows = statement_pdf.parse_statement_text(text)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertIn("Qashier-XI AN Noodl", rows[0]["description"])
        self.assertEqual(rows[0]["amount"], "-6.80")

    def test_comma_separated_rows_parse_too(self):
        # A statement exported as CSV but pasted through the PDF reader.
        text = (
            "2026-11-12,Inward DR - GIRO 5425503005970230 CCRD 503005970230 LI YUAN,31.41,0.00,1300.00\n"
            "2026-11-11,Inward Credit-FAST Transfer OTHR Other LIEW HON SENG,0.00,203.73,1331.41\n"
        )
        rows = statement_pdf.parse_statement_text(text)["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["amount"], "-31.41")
        self.assertEqual(rows[0]["description"],
                         "Inward DR - GIRO 5425503005970230 CCRD 503005970230 LI YUAN")

    def test_thousands_separator_is_handled(self):
        text = (
            f"{HEADER}\n"
            "2026-11-03 Funds Trf - FAST OTHR Transfer PIB2611032464283377 1,501.39 0.00 1307.61\n"
            "2026-11-03 Funds Transfer Transfer YUAN LI 0.00 1,501.39 2809.00\n"
        )
        rows = statement_pdf.parse_statement_text(text)["rows"]
        self.assertEqual(rows[0]["amount"], "-1501.39")
        self.assertEqual(rows[1]["amount"], "1501.39")

    def test_cr_tag_marks_a_two_column_row_as_a_deposit(self):
        text = (
            "Date Description Amount Balance\n"
            "2026-11-11 Salary 5000.00 CR 5000.00\n"
        )
        rows = statement_pdf.parse_statement_text(text)["rows"]
        self.assertEqual(rows[0]["amount"], "5000.00")

    def test_text_with_no_transactions_raises_a_clear_error(self):
        with self.assertRaises(statement_pdf.StatementError) as ctx:
            statement_pdf.parse_statement_text("This PDF is just a cover letter.\n")
        self.assertIn("No transaction rows could be read", str(ctx.exception))
        # The error must carry what was seen, or it cannot be acted on.
        self.assertIn("cover letter", ctx.exception.sample)

    def test_zero_on_both_sides_is_dropped(self):
        text = f"{HEADER}\n2026-11-01 Nothing happened 0.00 0.00 100.00\n"
        with self.assertRaises(statement_pdf.StatementError):
            statement_pdf.parse_statement_text(text)


class WrappedLayoutTests(unittest.TestCase):
    """A transaction is a block, not a line.

    Real statements wrap a long payee, and the money columns land on whichever line the
    wrap pushes them to. An earlier version treated each line as a row and failed on the
    real thing while passing on tidy test data — these cases lock that shut.
    """

    MONEY_ON_LAST_LINE = (
        "Date Description Withdrawal_SGD Deposit_SGD Balance_SGD\n"
        "2026-11-12 Inward DR - GIRO 5304863730514943\n"
        "CCRD 824295185025 DINERS CLUB (S) PL 172.32 0.00 1127.68\n"
        "2026-11-11 Inward Credit-FAST Transfer OTHR Other\n"
        "LIEW HON SENG 0.00 203.73 1300.00\n"
    )
    MONEY_ON_FIRST_LINE = (
        f"{HEADER}\n"
        "2026-11-12 PAYNOW-FAST OTHR qsb-sqr-sg- 38250221340549 6.80 0.00 1331.41\n"
        "PIB2611122586676639 Qashier-XI AN Noodl\n"
    )

    def test_money_on_the_last_line_still_produces_rows(self):
        rows = statement_pdf.parse_statement_text(self.MONEY_ON_LAST_LINE)["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["amount"], "-172.32")
        self.assertEqual(rows[1]["amount"], "203.73")

    def test_wrapped_description_before_the_money_is_joined(self):
        rows = statement_pdf.parse_statement_text(self.MONEY_ON_LAST_LINE)["rows"]
        self.assertIn("DINERS CLUB (S) PL", rows[0]["description"])
        self.assertIn("Inward DR - GIRO", rows[0]["description"])

    def test_wrapped_description_after_the_money_is_joined(self):
        rows = statement_pdf.parse_statement_text(self.MONEY_ON_FIRST_LINE)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertIn("Qashier-XI AN Noodl", rows[0]["description"])
        self.assertEqual(rows[0]["amount"], "-6.80")

    def test_balances_still_chain_across_a_wrap(self):
        # 1300.00 = 1127.68 + 172.32 (newest first), so a correct block parse verifies.
        result = statement_pdf.parse_statement_text(self.MONEY_ON_LAST_LINE)
        self.assertTrue(result["balance_check"]["checked"])
        self.assertTrue(result["balance_check"]["ok"], result["balance_check"])

    def test_a_block_with_no_money_is_not_a_transaction(self):
        text = (
            f"{HEADER}\n"
            "2026-11-01\n"
            "Opening balance brought forward\n"
            "2026-11-02 COFFEE 4.50 0.00 95.50\n"
        )
        rows = statement_pdf.parse_statement_text(text)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-11-02")

    def test_amounts_are_never_leaked_into_the_description(self):
        rows = statement_pdf.parse_statement_text(self.MONEY_ON_LAST_LINE)["rows"]
        self.assertNotIn("172.32", rows[0]["description"])


class LenientParseTests(unittest.TestCase):
    """The permissive reader, for layouts the strict one rejects.

    Its rows are only offered when nothing stricter worked, and the balance check still
    runs — but the regex it relies on must not silently miss amounts.
    """

    def test_four_digit_amounts_are_visible(self):
        # A \\d{1,3} bound made 13280.42 invisible and silently dropped the row.
        for value in ("1300.00", "13280.42", "5808.25", "99999.99"):
            with self.subTest(value=value):
                found = [m.group(0) for m in statement_pdf._DECIMAL.finditer(value)]
                self.assertEqual(found, [value])

    def test_thousands_separators_are_visible(self):
        found = [m.group(0) for m in statement_pdf._DECIMAL.finditer("1,300.00 and 12,499.33")]
        self.assertEqual(found, ["1,300.00", "12,499.33"])

    def test_description_preceding_the_date_is_read(self):
        text = (
            "Inward DR - GIRO DINERS CLUB 2026-11-12 172.32 0.00 1127.68\n"
            "LIEW HON SENG 11 Nov 2026 0.00 203.73 1300.00\n"
        )
        rows = statement_pdf.parse_lenient(text)["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["amount"], "-172.32")
        self.assertEqual(rows[1]["amount"], "203.73")

    def test_amounts_are_removed_cleanly_from_the_description(self):
        # "5000.00" contains "500": a string-replace leaves a fragment behind.
        text = "BIG TRANSFER 2026-11-10 5000.00 0.00 13280.42\n"
        rows = statement_pdf.parse_lenient(text)["rows"]
        self.assertEqual(rows[0]["description"], "BIG TRANSFER")

    def test_lenient_parse_failing_still_raises_with_a_sample(self):
        with self.assertRaises(statement_pdf.StatementError) as ctx:
            statement_pdf.parse_lenient("Just a cover letter, nothing else.\n")
        self.assertIn("cover letter", ctx.exception.sample)


class BalanceCheckTests(unittest.TestCase):
    def test_consistent_balances_pass(self):
        result = statement_pdf.parse_statement_text(SIMPLE)
        self.assertTrue(result["balance_check"]["checked"])
        self.assertTrue(result["balance_check"]["ok"])

    def test_a_dropped_row_is_detected(self):
        # Remove the middle row: the remaining rows no longer chain.
        lines = SIMPLE.strip().splitlines()
        broken = "\n".join(lines[:4] + lines[5:])
        result = statement_pdf.parse_statement_text(broken)
        self.assertFalse(result["balance_check"]["ok"])
        self.assertGreaterEqual(result["balance_check"]["mismatches"], 1)
        self.assertTrue(any("do not add up" in w for w in result["warnings"]))

    def test_a_misread_amount_is_detected(self):
        text = SIMPLE.replace("172.32 0.00 1127.68", "127.32 0.00 1127.68")
        result = statement_pdf.parse_statement_text(text)
        self.assertFalse(result["balance_check"]["ok"])

    def test_no_balance_column_means_the_check_is_skipped_not_failed(self):
        text = (
            "Date Description Withdrawal Deposit\n"
            "2026-11-01 COFFEE 4.50 0.00\n"
            "2026-11-02 COFFEE 4.50 0.00\n"
        )
        result = statement_pdf.parse_statement_text(text)
        self.assertFalse(result["balance_check"]["checked"])
        self.assertTrue(result["balance_check"]["ok"])


class PdfDetectionTests(unittest.TestCase):
    def test_magic_bytes_identify_a_pdf(self):
        self.assertTrue(statement_pdf.is_pdf(b"%PDF-1.7\n..."))
        self.assertFalse(statement_pdf.is_pdf(b"\x89PNG\r\n\x1a\n"))
        self.assertFalse(statement_pdf.is_pdf(b"\xff\xd8\xff\xe0"))

    def test_a_corrupt_pdf_raises_statement_error_not_a_crash(self):
        with self.assertRaises(statement_pdf.StatementError):
            statement_pdf.extract_text(b"%PDF-1.7 definitely not a real pdf")


if __name__ == "__main__":
    unittest.main()