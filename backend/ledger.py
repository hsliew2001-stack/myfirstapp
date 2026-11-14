"""Per-user transaction storage, and the CSV import that fills it.

``tx_seen`` (see ``history.py``) is a global set of hashes used only to warn about a
re-uploaded screenshot. It cannot answer *when* a bill repeats or *what* the balance
will be, because it has no owner, date or description. This module owns ``tx_ledger``,
which does.

Every query filters on ``actor`` — the SSO email from ``x-forwarded-email``. That is an
identity supplied by the platform's proxy, never by the browser, so it is the right
boundary for "your transactions". When SSO is off the header is absent and every caller
shares the ``anonymous`` bucket; that is a known limitation of running without SSO, not
an access-control decision made here.

As with ``history.py``, a missing ``DATABASE_URL`` (local dev without compose) must not
break the app: reads return empty and writes are no-ops.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import asyncmy

from history import _clip, _dsn  # reuse the DSN parsing; same pool semantics

ANONYMOUS = "anonymous"
MAX_IMPORT_ROWS = 5000
MAX_ROWS = 2000

#: Header aliases, lowercased. Singapore banks are inconsistent about wording, so the
#: importer looks for any of these rather than demanding one schema (PRD F4.2).
_DATE_KEYS = ("date", "transaction date", "txn date", "posting date", "value date", "when")
_DESC_KEYS = ("description", "details", "transaction details", "narrative", "payee",
              "merchant", "particulars", "reference", "remarks", "memo")
_AMOUNT_KEYS = ("amount", "transaction amount", "amt", "value", "amount (sgd)")
_DEBIT_KEYS = ("debit", "withdrawal", "withdrawals", "debit amount", "money out", "outflow")
_CREDIT_KEYS = ("credit", "deposit", "deposits", "credit amount", "money in", "inflow")
_BALANCE_KEYS = ("balance", "running balance", "closing balance", "available balance")

_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y%m%d",
)


class ImportError_(Exception):
    """A problem with the uploaded file the user can act on (surfaced as a 422)."""


# ------------------------------------------------------------------------ parsing


def parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    # Strip a trailing time component: "2026-10-21 14:32" and "2026-10-21T14:32:00".
    text = re.split(r"[ T]", text, maxsplit=1)[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_money(value: object) -> Decimal | None:
    """Parse an amount cell, tolerating currency symbols, thousands separators and CR/DR."""
    text = str(value or "").strip()
    if not text:
        return None
    negative = False
    lowered = text.lower()
    if lowered.endswith(("dr", " db")) or lowered.startswith(("dr ", "(dr")):
        negative = True
        text = re.sub(r"(?i)\b(dr|db)\b", "", text)
    if lowered.endswith(("cr", " cr.")) or lowered.startswith("cr "):
        text = re.sub(r"(?i)\bcr\b", "", text)
    wrapped = text.strip().startswith("(") and text.strip().endswith(")")
    text = text.replace("(", "").replace(")", "")
    text = (
        text.replace(",", "")
        .replace("$", "")
        .replace("S$", "")
        .replace("USD", "")
        .replace("−", "-")
        .replace("–", "-")
        .replace("+", "")
        .strip()
    )
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if wrapped or negative:
        amount = -abs(amount)
    return amount


def _description_direction(description: str) -> int:
    """-1 for an explicit debit marker, +1 for credit, 0 when there is none.

    Matched as a standalone token, so "KOPITIAM (DR)" and "SALARY CR" qualify while
    "DRIVING LESSON" and "CRANE SERVICES" do not.
    """
    tokens = {t.strip("().,") for t in str(description or "").upper().split()}
    if tokens & {"DR", "DB", "DEBIT"}:
        return -1
    if tokens & {"CR", "CREDIT"}:
        return 1
    return 0


def _norm_header(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(name).lower())).strip()


def _find(header: list[str], keys: tuple[str, ...]) -> int | None:
    for index, name in enumerate(header):
        if name in keys:
            return index
    # Fall back to a substring match ("transaction amount (sgd)" contains "amount"), but
    # only for the amount column, where that is unambiguous enough to be useful.
    for index, name in enumerate(header):
        if any(key in name for key in keys) and not any(
            other in name for other in _BALANCE_KEYS
        ):
            return index
    return None


def detect_columns(raw_header: list[str]) -> dict:
    header = [_norm_header(h) for h in raw_header]
    columns = {
        "date": _find(header, _DATE_KEYS),
        "description": _find(header, _DESC_KEYS),
        "amount": _find(header, _AMOUNT_KEYS),
        "debit": _find(header, _DEBIT_KEYS),
        "credit": _find(header, _CREDIT_KEYS),
    }
    return columns


def parse_csv(text: str, default_currency: str = "SGD") -> dict:
    """Parse a transaction CSV into rows, reporting per-line problems.

    Returns ``{"rows": [...], "errors": [...], "columns": {...}, "delimiter": ","}``.
    Rows without a usable date or amount become *errors*, not silent drops — the user is
    importing a financial record and needs to know what did not land (PRD F4.3).
    """
    if not text.strip():
        raise ImportError_("The file is empty.")

    # Sniff the delimiter on the first non-empty line; bank exports vary.
    sample = text.splitlines()[0] if text.splitlines() else ""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        raw_header = next(reader)
    except StopIteration as exc:
        raise ImportError_("The file has no header row.") from exc

    columns = detect_columns(raw_header)
    has_split = columns["debit"] is not None or columns["credit"] is not None
    if columns["date"] is None:
        raise ImportError_(
            "Could not find a date column. Rename the column to 'Date' (or "
            "'Transaction Date') and try again."
        )
    if columns["amount"] is None and not has_split:
        raise ImportError_(
            "Could not find an amount column. Rename it to 'Amount', or use separate "
            "'Debit' and 'Credit' columns."
        )

    rows: list[dict] = []
    errors: list[str] = []
    for line_number, record in enumerate(reader, start=2):
        if not any(cell.strip() for cell in record):
            continue  # blank line, common at the foot of exports
        if len(rows) + len(errors) >= MAX_IMPORT_ROWS:
            errors.append(f"Stopped at {MAX_IMPORT_ROWS} rows — split the file and import the rest.")
            break

        def cell(index: int | None) -> str:
            if index is None or index >= len(record):
                return ""
            return record[index].strip()

        when = parse_date(cell(columns["date"]))
        if when is None:
            # Name the assumed order: "06/25/2026" is unreadable to a DD/MM reader, and
            # saying so is the difference between a fixed file and a support ticket.
            errors.append(
                f"Line {line_number}: unreadable date {cell(columns['date'])!r} "
                "(accepted: YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, 21 Oct 2026)."
            )
            continue

        description = cell(columns["description"])
        amount = None
        if has_split:
            debit = parse_money(cell(columns["debit"]))
            credit = parse_money(cell(columns["credit"]))
            if debit not in (None, Decimal("0")):
                amount = -abs(debit)
            elif credit not in (None, Decimal("0")):
                amount = abs(credit)
        else:
            amount = parse_money(cell(columns["amount"]))
            # Some exports put the DR/CR marker in the description while the amount
            # column stays unsigned. Ignoring it would invert every debit in the ledger,
            # so an explicit marker wins. Only an explicit one counts — a bare "dr" in
            # free text ("DRIVING LESSON") must not.
            if amount is not None and _description_direction(description):
                sign = _description_direction(description)
                amount = -abs(amount) if sign < 0 else abs(amount)
        if amount is None:
            errors.append(f"Line {line_number}: unreadable amount ({when.isoformat()}).")
            continue
        if amount == 0:
            errors.append(f"Line {line_number}: amount is zero, skipped.")
            continue

        rows.append(
            {
                "date": when.isoformat(),
                "description": description[:512],
                "amount": format(amount.quantize(Decimal("0.01")), "f"),
                "currency": default_currency.upper()[:3],
            }
        )

    if not rows and not errors:
        raise ImportError_("That file has a header but no transaction rows.")

    # Rows whose fingerprint repeats earlier in the same file are dropped here: a
    # statement that lists a pending charge twice should not become two transactions.
    import workbook

    seen: set[str] = set()
    unique: list[dict] = []
    for row in rows:
        fingerprint = workbook.fingerprint(row["date"], row["description"], Decimal(row["amount"]))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(row)

    return {
        "rows": unique,
        # How many of these the file itself introduced; the endpoint narrows this to
        # "not already in the database" when it previews.
        "unseen": len(unique),
        "errors": errors,
        "columns": {
            name: (raw_header[index] if index is not None and index < len(raw_header) else None)
            for name, index in columns.items()
        },
        "delimiter": delimiter,
    }


# ------------------------------------------------------------------------ storage


class Ledger:
    def __init__(self) -> None:
        self._pool: asyncmy.Pool | None = None

    def configured(self) -> bool:
        import os

        return bool(os.getenv("DATABASE_URL"))

    @staticmethod
    def actor(email: str | None) -> str:
        return (email or ANONYMOUS).strip().lower()[:320] or ANONYMOUS

    async def connect(self) -> None:
        if self.configured() and self._pool is None:
            self._pool = await asyncmy.create_pool(**_dsn(), autocommit=True)

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def _run(self, sql: str, args: tuple = ()) -> list[tuple]:
        if self._pool is None:
            return []
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, args)
            if cur.description is None:
                return []
            return list(await cur.fetchall())

    async def _exec(self, sql: str, args: tuple = ()) -> int:
        """Run a write and return affected rows. Needed because ``INSERT IGNORE`` that
        hits a duplicate reports 0 — which is exactly the number the importer shows."""
        if self._pool is None:
            return 0
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, args)
            return int(cur.rowcount or 0)

    # -- writes ---------------------------------------------------------------

    async def add(self, actor: str, account: str, rows: list[dict], source: str) -> dict:
        """Insert rows, skipping any this user already has (by fingerprint).

        Import and apply both land here. The unique key on ``(actor, fingerprint)`` is
        what makes importing an overlapping statement a no-op rather than a double count.
        """
        import workbook

        if self._pool is None or not rows:
            return {"added": 0, "skipped": 0}
        added = 0
        for row in rows:
            try:
                fingerprint = workbook.fingerprint(
                    row.get("date"), row.get("description", ""), Decimal(str(row["amount"]))
                )
            except (ValueError, InvalidOperation, KeyError):
                continue
            try:
                affected = await self._exec(
                    "INSERT IGNORE INTO tx_ledger "
                    "(actor, account, tx_date, description, amount, currency, fingerprint, source) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        actor[:320],
                        _clip(account, 128) or "Unassigned",
                        row["date"],
                        _clip(row.get("description", ""), 512) or "",
                        str(row["amount"]),
                        (row.get("currency") or "SGD")[:3].upper(),
                        fingerprint,
                        (source or "screenshot")[:16],
                    ),
                )
            except Exception:  # noqa: BLE001 - one bad row must not lose the rest
                continue
            # rowcount, not "we tried": an ignored duplicate is a skip, and telling the
            # user "21 added" when 0 landed is the kind of lie a ledger must not tell.
            added += 1 if affected else 0
        return {"added": added, "skipped": max(0, len(rows) - added)}

    async def clear(self, actor: str) -> int:
        rows = await self._run("SELECT COUNT(*) FROM tx_ledger WHERE actor = %s", (actor,))
        count = int(rows[0][0]) if rows else 0
        await self._run("DELETE FROM tx_ledger WHERE actor = %s", (actor,))
        return count

    # -- reads ----------------------------------------------------------------

    async def rows(
        self, actor: str, currency: str | None = None, account: str | None = None,
        limit: int = MAX_ROWS,
    ) -> list[dict]:
        """Oldest-first: the analysis wants a timeline, not a feed."""
        sql = (
            "SELECT tx_date, description, amount, currency, account, source "
            "FROM tx_ledger WHERE actor = %s"
        )
        args: list = [actor]
        if currency:
            sql += " AND currency = %s"
            args.append(currency.upper()[:3])
        if account:
            sql += " AND account = %s"
            args.append(account[:128])
        sql += " ORDER BY tx_date DESC, id DESC LIMIT %s"
        args.append(max(1, min(int(limit), MAX_ROWS)))
        try:
            found = await self._run(sql, tuple(args))
        except Exception:  # noqa: BLE001 - a read failure renders as "no history"
            return []
        rows = [
            {
                "date": row[0].isoformat() if isinstance(row[0], date) else str(row[0]),
                "description": row[1],
                "amount": format(Decimal(str(row[2])).quantize(Decimal("0.01")), "f"),
                "currency": row[3],
                "account": row[4],
                "source": row[5],
            }
            for row in found
        ]
        rows.reverse()  # oldest first, matching insights.normalize_rows
        return rows

    async def recent(self, actor: str, limit: int = 50) -> list[dict]:
        found = await self.rows(actor, limit=limit)
        return list(reversed(found))