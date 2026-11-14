"""Transaction-to-spreadsheet app: read a bank screenshot, append it to an Excel balance.

The workbook never leaves your control: every call takes the .xlsx, edits exactly one
formula, and hands the file back. Nothing is stored server-side except a hash-based audit
trail (used to warn when a screenshot overlaps one you already entered).

  POST /api/analyze   screenshot + workbook + account label -> rows + proposed target cell
  POST /api/apply     the same, plus the confirmed choices -> the patched .xlsx download
  GET  /api/history   recent runs, most recent first

Insights (recurring bills + a forward forecast; see docs/PRD-recurring-forecast.md):

  GET    /api/insights       recurring series, monthly summary, balance projection
  POST   /api/affordability  "can I afford X on date D" -> verdict + projection
  POST   /api/ledger/import  upload a transactions CSV (preview or commit)
  POST   /api/ledger/rows    add rows by hand
  GET    /api/ledger         the stored transactions (newest first)
  DELETE /api/ledger         erase this user's stored transactions
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

import extract
import history
import insights
import ledger
import statement_pdf
import workbook

MAX_WORKBOOK_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_CSV_BYTES = 5 * 1024 * 1024
# Statements are bigger than screenshots: a multi-page PDF with a text layer is parsed
# locally, so the limit here is about upload size, not model tokens.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
# A scanned PDF is rendered page by page at ~2x; each page must stay under the vision
# model's own image limit, which the render path enforces.
MAX_PDF_PAGES_VISION = statement_pdf.MAX_VISION_PAGES

store = history.Database()
book = ledger.Ledger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.connect()
    await book.connect()
    yield
    await book.close()
    await store.close()


# ------------------------------------------------------------------------ models


class Tx(BaseModel):
    date: str | None = None
    description: str
    amount: str
    currency: str | None = None
    fingerprint: str | None = None


class RowIn(BaseModel):
    date: str | None = None
    description: str = ""
    amount: str
    currency: str | None = None


class Candidate(BaseModel):
    sheet: str
    cell: str
    label_cell: str
    label: str
    formula: str | None = None
    raw_value: str | None = None


class BalanceCheck(BaseModel):
    """Whether the statement's own running balance agrees with the rows read."""

    checked: bool
    ok: bool
    mismatches: int = 0
    message: str = ""


class AnalyzeResult(BaseModel):
    account_hint: str | None = None
    transactions: list[Tx]
    candidates: list[Candidate]
    warnings: list[str] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list)
    total: str
    # "pdf-text" | "pdf-vision" | "image" — lets the UI say how the rows were read.
    source: str = "image"
    balance_check: BalanceCheck | None = None


class HistoryEntry(BaseModel):
    created_at: str
    actor: str
    action: str
    sheet: str | None = None
    cell: str | None = None
    count: int
    detail: str | None = None


class HistoryResult(BaseModel):
    entries: list[HistoryEntry]


class Health(BaseModel):
    status: str


class Me(BaseModel):
    email: str | None = None
    user: str | None = None


class Config(BaseModel):
    model: str
    provider: str
    has_api_key: bool
    # Surfaced so the UI can be honest about whether history is being stored at all.
    history_enabled: bool


class RecurringItem(BaseModel):
    key: str
    title: str
    direction: str
    cadence: str
    interval_days: int
    amount: str
    occurrences: int
    last_seen: str
    next_expected: str
    days_until: int
    status: str
    missed: bool
    confidence: str
    currency: str


class MoneySummary(BaseModel):
    monthly_income: str
    monthly_spend: str
    monthly_net: str
    daily_burn: str
    months: int


class Point(BaseModel):
    date: str
    balance: str


class LowPoint(BaseModel):
    date: str
    balance: str


class ForecastOut(BaseModel):
    start: str
    end: str
    horizon_days: int
    currency: str
    start_balance: str | None = None
    low: LowPoint
    end_balance: str
    points: list[Point]
    assumptions: list[str]
    confidence: str | None = None
    confidence_reasons: list[str] = Field(default_factory=list)


class Coverage(BaseModel):
    first: str
    last: str
    days: int
    transactions: int


class BalanceInfo(BaseModel):
    supplied: bool
    amount: str | None = None


class InsightsResult(BaseModel):
    ready: bool
    currency: str
    reason: str | None = None
    coverage: Coverage | None = None
    balance: BalanceInfo | None = None
    recurring: list[RecurringItem] = Field(default_factory=list)
    summary: MoneySummary
    forecast: ForecastOut | None = None
    warnings: list[str] = Field(default_factory=list)


class AffordabilityRequest(BaseModel):
    amount: str
    date: str | None = None
    balance: str | None = None
    currency: str = "SGD"
    cushion: str = "0"
    horizon_days: int = insights.DEFAULT_HORIZON_DAYS


class AffordabilityResult(BaseModel):
    verdict: str
    reason: str
    amount: str
    date: str
    cushion: str
    currency: str
    low: LowPoint
    end_balance: str
    start_balance: str | None = None
    forecast: ForecastOut


class LedgerRow(BaseModel):
    date: str
    description: str = ""
    amount: str
    currency: str | None = None
    account: str | None = None
    source: str | None = None


class LedgerList(BaseModel):
    rows: list[LedgerRow]
    total: int


class LedgerAddRequest(BaseModel):
    account: str = "Unassigned"
    currency: str = "SGD"
    rows: list[RowIn]


class ImportResult(BaseModel):
    committed: bool
    parsed: int
    added: int
    skipped: int
    errors: list[str] = Field(default_factory=list)
    columns: dict[str, str | None] = Field(default_factory=dict)
    rows_preview: list[RowIn] = Field(default_factory=list)


class ClearResult(BaseModel):
    deleted: int


# ----------------------------------------------------------------------- helpers


def _actor(request: Request) -> str:
    """The signed-in user, from the platform's unspoofable header.

    ``x-forwarded-email`` is injected by the ingress when SSO is enabled and is absent
    otherwise; writes from an anonymous session land in one shared bucket, which is also
    what local development sees.
    """
    return ledger.Ledger.actor(request.headers.get("x-forwarded-email"))


def _decimal(value: str | None, what: str, *, required: bool = False) -> Decimal | None:
    if value is None or not str(value).strip():
        if required:
            raise HTTPException(400, f"{what} is required.")
        return None
    try:
        parsed = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation as exc:
        raise HTTPException(400, f"{what} is not a number: {value!r}") from exc
    if not parsed.is_finite():
        raise HTTPException(400, f"{what} is not a finite number.")
    return parsed


def _as_date(value: str | None) -> date:
    if not value:
        return date.today()
    parsed = insights.parse_day(value)
    if parsed is None:
        raise HTTPException(400, f"That date could not be read: {value!r} (use YYYY-MM-DD).")
    return parsed


def _statement_detail(exc: statement_pdf.StatementError) -> str:
    """A 422 message that says what the reader actually saw.

    A bare "no rows found" is impossible to act on from outside: it is the same message
    for an empty file, a scan, and a table whose column shape the reader did not expect.
    Appending the first lines it read turns the error into a diagnosis.
    """
    parts = [exc.message]
    if exc.detail:
        parts.append(exc.detail)
    if exc.sample:
        clipped = exc.sample if len(exc.sample) <= 600 else exc.sample[:600] + "…"
        parts.append("The first lines the app read were:\n" + clipped)
    return "\n\n".join(parts)


def _extract_statement(data: bytes, content_type: str | None) -> tuple[dict, list[str]]:
    """Read an image *or* a PDF into transactions, choosing the most exact path.

    A PDF with a text layer is parsed deterministically — exact digits, no model call.
    Only a PDF with no text (a scan) is rendered and sent to the vision model, page by
    page. Returns the extraction plus any warnings the caller should surface.
    """
    if data[:5] == b"%PDF-":
        # Rebuild lines from word coordinates first: some PDFs put every word on its own
        # line in the text layer, which makes the line-based extraction useless even
        # though the page looks like a table. Fall back to plain text only if the
        # positional pass yields nothing.
        text = statement_pdf.extract_positional_text(data)
        if not text.strip():
            text = statement_pdf.extract_text(data)
        text_failure = None
        if text.strip():
            try:
                parsed = statement_pdf.parse_statement_text(text)
            except statement_pdf.StatementError as exc:
                # The strict reader wants a date at the start of a line and the money at
                # the end. Before falling back to the (slower, costlier) model, try a
                # permissive pass: find a date and amounts anywhere on the line. Its rows
                # are only trusted if the running balance agrees, so a misread cannot slip
                # through silently.
                try:
                    parsed = statement_pdf.parse_lenient(text)
                except statement_pdf.StatementError:
                    parsed = None
                if parsed is None or not parsed["rows"]:
                    # Keep the whole exception, not str(exc): the message alone discards
                    # the sample of what was read, which is what makes this diagnosable.
                    text_failure = exc
                else:
                    parsed["warnings"] = ["Read with a permissive parser. Check the rows "
                                          "against the PDF before applying."] + list(parsed["warnings"])
                    return (
                        {
                            "account_hint": None,
                            "transactions": [
                                {
                                    "date": row["date"],
                                    "description": row["description"],
                                    "amount": Decimal(row["amount"]),
                                    "currency": row["currency"],
                                }
                                for row in parsed["rows"]
                            ],
                            "truncated": False,
                            "balance_check": parsed["balance_check"],
                            "source": "pdf-lenient",
                        },
                        list(parsed["warnings"]),
                    )
            else:
                rows = parsed["rows"]
                return (
                    {
                        "account_hint": None,
                        "transactions": [
                            {
                                "date": row["date"],
                                "description": row["description"],
                                # parse_statement_text already hands back Decimal amounts.
                                "amount": Decimal(row["amount"]),
                                "currency": row["currency"],
                            }
                            for row in rows
                        ],
                        "truncated": False,
                        "balance_check": parsed["balance_check"],
                        "source": "pdf-text",
                    },
                    list(parsed["warnings"]),
                )

        # No usable text layer (a scan, or a layout the reader could not follow). Render
        # the pages and let the model read them.
        pages = statement_pdf.render_pages(data)
        if not pages:
            raise statement_pdf.StatementError(
                "That PDF has no text and no renderable pages, so it could not be read."
            )
        merged: dict = {"account_hint": None, "transactions": [], "truncated": False,
                        "source": "pdf-vision"}
        if text_failure:
            warnings = [
                "This PDF's text is in a layout the app's table reader could not follow, so "
                "it was read with the vision model instead. Check the rows carefully."
            ]
        else:
            warnings = [
                f"This PDF has no text layer (it looks like a scan), so it was read with the "
                f"vision model from {len(pages)} page(s). Check the rows against the PDF."
            ]
        # Read the pages concurrently. Sequentially, eight pages at up to 120s each can
        # outlast the ingress timeout, which surfaces to the user as a bare 504.
        with ThreadPoolExecutor(max_workers=min(4, len(pages))) as pool:
            futures = [pool.submit(extract.extract, page, "image/png") for page in pages]
            results = []
            for future in futures:
                try:
                    results.append(future.result())
                except extract.ExtractionError:
                    # If the fallback itself cannot run (no API key, provider down) and we
                    # only got here because the text layout was unrecognised, re-raise the
                    # layout error intact. It already carries the sample of what was read,
                    # which is what makes the failure diagnosable; wrapping it in a new
                    # error would throw that away.
                    if text_failure is not None:
                        raise text_failure from None
                    raise
        for page_result in results:
            merged["transactions"].extend(page_result["transactions"])
            merged["account_hint"] = merged["account_hint"] or page_result.get("account_hint")
            merged["truncated"] = merged["truncated"] or page_result.get("truncated", False)
        if statement_pdf.page_count(data) > len(pages):
            warnings.append(
                f"Only the first {len(pages)} pages were read. Split the statement and "
                "upload the rest."
            )
        if not merged["transactions"] and text_failure is not None:
            # Both readers came back empty. The text reader's error carries the sample of
            # what it saw, which is the only actionable evidence here — surface it rather
            # than the caller reporting a bare "no transactions found".
            raise text_failure
        return merged, warnings

    result = extract.extract(data, content_type)
    result["source"] = "image"
    result["balance_check"] = {"checked": False, "ok": True, "mismatches": 0, "message": ""}
    return result, []


def _read_upload(file: UploadFile, limit: int, what: str) -> bytes:
    data = file.file.read(limit + 1)
    if not data:
        raise HTTPException(400, f"The {what} you attached was empty.")
    if len(data) > limit:
        raise HTTPException(413, f"That {what} is larger than {limit // (1024 * 1024)} MB.")
    return data


def _to_amounts(rows: list[RowIn]) -> list[Decimal]:
    amounts: list[Decimal] = []
    for index, row in enumerate(rows, start=1):
        try:
            amounts.append(workbook.parse_amount(row.amount))
        except ValueError as exc:
            raise HTTPException(400, f"Row {index}: {exc}") from exc
    return amounts


def _safe_filename(name: str) -> str:
    base = os.path.basename(name or "workbook.xlsx")
    stem, _, ext = base.rpartition(".")
    if not stem:
        stem, ext = base, "xlsx"
    suffix = "xlsm" if ext.lower() == "xlsm" else "xlsx"
    # ASCII-only: HTTP headers are latin-1, so a CJK filename would break the download.
    cleaned = (
        "".join(c for c in stem if c.isascii() and (c.isalnum() or c in " -_().")).strip()
        or "workbook"
    )
    return f"{cleaned}-updated.{suffix}"


def _sheet_response(data: bytes, filename: str) -> Response:
    macro = filename.lower().endswith(".xlsm")
    return Response(
        content=data,
        media_type=(
            "application/vnd.ms-excel.sheet.macroEnabled.12"
            if macro
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{_safe_filename(filename)}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


def _resolve_and_patch(
    workbook_bytes: bytes,
    label: str,
    sheet: str,
    cell: str,
    amounts: list[Decimal],
    compact: bool,
) -> tuple[bytes, dict]:
    """Resolve the target cell, build the new formula, and patch the workbook."""
    if not (workbook_bytes.startswith(b"PK\x03\x04") or workbook_bytes.startswith(b"PK\x05\x06")):
        raise HTTPException(
            400,
            "That workbook isn't an .xlsx or .xlsm file. (Old .xls files must be saved as "
            ".xlsx in Excel first.)",
        )
    try:
        if sheet and cell:
            before = workbook.read_cell(workbook_bytes, sheet, cell)
        else:
            if not label.strip():
                raise HTTPException(400, "Name the account to update, or pick a cell explicitly.")
            matches = workbook.find_candidates(workbook_bytes, label)
            if not matches:
                raise HTTPException(
                    404,
                    f'No row in that workbook contains "{label}". Check the account name, '
                    "or pick the balance cell yourself.",
                )
            distinct = {(m["sheet"], m["cell"]) for m in matches}
            if len(distinct) > 1:
                options = ", ".join(f'{m["sheet"]}!{m["cell"]}' for m in matches)
                raise HTTPException(
                    409,
                    f'"{label}" appears in more than one row ({options}). Choose the cell to update.',
                )
            sheet, cell = next(iter(distinct))
            before = workbook.read_cell(workbook_bytes, sheet, cell)

        # A plain-number cell (4200) is just as valid a base as a formula (=1310.25);
        # build_formula folds either into one expression.
        base = before["formula"] if before["formula"] is not None else before["raw_value"]
        formula, estimate = workbook.build_formula(base, amounts, compact)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - openpyxl/zipfile raise a variety of errors
        raise HTTPException(400, f"Could not read that workbook: {exc}") from exc

    try:
        patched = workbook.patch_cell(workbook_bytes, sheet, cell, formula)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return patched, {
        "sheet": sheet,
        "cell": cell,
        "before": before["formula"] or before["raw_value"],
        "after": formula,
        "estimate": str(estimate) if estimate is not None else None,
        "delta": workbook.format_amount(sum(amounts, Decimal("0"))),
        "count": len(amounts),
        "label": label,
    }


def _parse_rows(raw: str) -> list[RowIn]:
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "The edited row list was not valid JSON.") from exc
    if not isinstance(parsed, list):
        raise HTTPException(400, "The edited row list should be a JSON array.")
    try:
        return [RowIn.model_validate(item) for item in parsed]
    except Exception as exc:  # noqa: BLE001 - pydantic reports field-level detail here
        raise HTTPException(400, f"The edited rows could not be read: {exc}") from exc


app = FastAPI(
    title="Statement to Spreadsheet",
    description=(
        "Reads a bank transaction screenshot with OpenRouter vision models and appends the signed "
        "amounts onto an account-balance formula in an Excel workbook."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ------------------------------------------------------------------------ routes


@app.get("/health", response_model=Health)
def health():
    return {"status": "ok"}


@app.get("/api/me", response_model=Me)
def me(request: Request):
    return {
        "email": request.headers.get("x-forwarded-email"),
        "user": request.headers.get("x-forwarded-user"),
    }


@app.get("/api/config", response_model=Config)
def config():
    return {
        "model": extract.MODEL,
        "provider": "openrouter",
        "has_api_key": extract.has_api_key(),
        # Whether *transaction history* can be stored at all. The UI uses this to decide
        # whether to offer the Insights tab or explain that it is unavailable here.
        "history_enabled": book.configured(),
    }


@app.post("/api/analyze", response_model=AnalyzeResult)
async def analyze(
    request: Request,
    screenshot: Annotated[UploadFile, File()],
    workbook_file: Annotated[UploadFile, File()],
    label: Annotated[str, Form()] = "UOB One",
):
    """Extract transactions and propose which cell to append to. Writes nothing.

    The uploaded file may be an image or a statement PDF; both arrive as ``screenshot``
    so existing clients (and the form field name) keep working.
    """
    image_bytes = _read_upload(screenshot, MAX_UPLOAD_BYTES, "statement")
    workbook_bytes = _read_upload(workbook_file, MAX_WORKBOOK_BYTES, "workbook")

    try:
        # Blocking (pdfplumber + model calls). Run it in a worker thread so the event
        # loop keeps serving /health and other requests while a statement is read; on the
        # loop it would freeze the whole app for the duration of the model call.
        result, source_warnings = await run_in_threadpool(
            _extract_statement, image_bytes, screenshot.content_type
        )
    except extract.ExtractionError as exc:
        raise HTTPException(422, str(exc)) from exc
    except statement_pdf.StatementError as exc:
        raise HTTPException(422, _statement_detail(exc)) from exc

    try:
        candidates = workbook.find_candidates(workbook_bytes, label)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises a variety of errors
        raise HTTPException(400, f"Could not read that workbook: {exc}") from exc

    # Warnings from the reader itself come first: a missed row matters more than a
    # mismatched account name.
    warnings: list[str] = list(source_warnings)
    if result["account_hint"] and label.strip():
        # Compare on letters-and-digits only: the model may drop a space ("UOBOne
        # Account"), which shouldn't read as a different account.
        hint = workbook.compact_label(result["account_hint"])
        needle = workbook.compact_label(label)
        if needle not in hint and hint not in needle:
            warnings.append(
                f'The screenshot names "{result["account_hint"]}", which does not match '
                f'"{label}". Confirm you are updating the right account.'
            )
    if not candidates:
        warnings.append(
            f'No row in the workbook contains "{label}", so there is no cell to append to yet.'
        )
    if not result["transactions"]:
        warnings.append("No transactions were found in that file.")
    if result.get("truncated"):
        warnings.append(
            "The model hit its output limit, so this list may be incomplete. Check the "
            "screenshot against the rows below, or split it into smaller images."
        )

    duplicates: list[str] = []
    known = await store.fingerprints()
    transactions: list[Tx] = []
    total = Decimal("0")
    for row in result["transactions"]:
        print_ = workbook.fingerprint(row["date"], row["description"], row["amount"])
        total += row["amount"]
        if print_ in known:
            duplicates.append(
                f'{row["date"] or "undated"} · {row["description"]} · '
                f"{workbook.format_amount(row['amount'])}"
            )
        transactions.append(
            Tx(
                date=row["date"],
                description=row["description"],
                amount=workbook.format_amount(row["amount"]),
                currency=row["currency"],
                fingerprint=print_,
            )
        )

    await store.log(
        actor=request.headers.get("x-forwarded-email"),
        action="analyze",
        sheet=candidates[0]["sheet"] if candidates else None,
        cell=candidates[0]["cell"] if len(candidates) == 1 else None,
        count=len(transactions),
        detail=label,
    )

    return AnalyzeResult(
        account_hint=result["account_hint"],
        transactions=transactions,
        candidates=[Candidate(**c) for c in candidates],
        warnings=warnings,
        duplicates=duplicates,
        total=workbook.format_amount(total),
        source=result.get("source", "image"),
        balance_check=result.get("balance_check"),
    )


@app.post("/api/apply")
async def apply(
    request: Request,
    screenshot: Annotated[UploadFile | None, File()] = None,
    workbook_file: Annotated[UploadFile | None, File()] = None,
    label: Annotated[str, Form()] = "UOB One",
    sheet: Annotated[str, Form()] = "",
    cell: Annotated[str, Form()] = "",
    rows_json: Annotated[str, Form()] = "",
    compact: Annotated[bool, Form()] = False,
):
    """Append the rows to the target cell and return the patched workbook."""
    if workbook_file is None:
        raise HTTPException(400, "Attach the workbook you want updated.")
    workbook_bytes = _read_upload(workbook_file, MAX_WORKBOOK_BYTES, "workbook")

    rows = _parse_rows(rows_json)
    if not rows:
        # No reviewed rows supplied: extract fresh from the upload (image or PDF).
        if screenshot is None:
            raise HTTPException(400, "There are no transactions to apply.")
        image_bytes = _read_upload(screenshot, MAX_UPLOAD_BYTES, "statement")
        try:
            extracted, _ = await run_in_threadpool(
                _extract_statement, image_bytes, screenshot.content_type
            )
        except extract.ExtractionError as exc:
            raise HTTPException(422, str(exc)) from exc
        except statement_pdf.StatementError as exc:
            raise HTTPException(422, _statement_detail(exc)) from exc
        rows = [
            RowIn(
                date=row["date"],
                description=row["description"],
                amount=workbook.format_amount(row["amount"]),
                currency=row["currency"],
            )
            for row in extracted["transactions"]
        ]
        if not rows:
            raise HTTPException(400, "There are no transactions to apply.")

    amounts = _to_amounts(rows)
    patched, info = _resolve_and_patch(
        workbook_bytes, label, sheet, cell, amounts, compact
    )

    await store.log(
        actor=request.headers.get("x-forwarded-email"),
        action="apply",
        sheet=info["sheet"],
        cell=info["cell"],
        count=info["count"],
        detail=f'{info["delta"]}',
    )
    for row, amount in zip(rows, amounts):
        await store.remember(
            workbook.fingerprint(row.date, row.description, amount), row.date, amount
        )

    # Also record the transactions themselves, so the recurring/forecast features have
    # something to work from. Best-effort: a storage problem must not lose the workbook
    # the user just asked for. Rows without a date cannot be placed on a timeline, so the
    # ledger skips them (they still went into the formula, which needs only the amount).
    await book.add(
        _actor(request),
        info.get("label") or label,
        [
            {
                "date": row.date,
                "description": row.description,
                "amount": workbook.format_amount(amount),
                "currency": row.currency,
            }
            for row, amount in zip(rows, amounts)
            if insights.parse_day(row.date) is not None
        ],
        source="screenshot",
    )

    response = _sheet_response(patched, workbook_file.filename or "workbook.xlsx")
    # The browser reads these back so it can show what changed without a second request.
    # Headers are latin-1: a sheet name or formula with non-ASCII text would raise, so
    # such values are dropped rather than failing the download itself.
    for name, value in (
        ("X-Cell", f'{info["sheet"]}!{info["cell"]}'),
        ("X-Formula-Before", info["before"] or ""),
        ("X-Formula-After", info["after"]),
        ("X-Delta", info["delta"]),
    ):
        try:
            value.encode("latin-1")
        except UnicodeEncodeError:
            continue
        response.headers[name] = value
    response.headers["Access-Control-Expose-Headers"] = (
        "X-Cell, X-Formula-Before, X-Formula-After, X-Delta, Content-Disposition"
    )
    return response


@app.get("/api/history", response_model=HistoryResult)
async def get_history(limit: int = 50):
    return HistoryResult(entries=[HistoryEntry(**e) for e in await store.recent(limit)])


# ----------------------------------------------------------------------- insights


async def _history_rows(
    actor: str, currency: str, account: str | None = None, limit: int = ledger.MAX_ROWS
) -> list[dict]:
    return await book.rows(actor, currency=currency, account=account, limit=limit)


@app.get("/api/insights", response_model=InsightsResult)
async def get_insights(
    request: Request,
    balance: str | None = None,
    currency: str = "SGD",
    account: str | None = None,
    horizon_days: int = insights.DEFAULT_HORIZON_DAYS,
    as_of: str | None = None,
):
    """Recurring bills and a forward balance projection from stored transactions.

    Shaped so the UI can render an honest empty state: ``ready: false`` carries the
    reason and everything else is optional (see the PRD, F2.6).
    """
    rows = await _history_rows(_actor(request), currency, account)
    result = insights.insights(
        rows,
        _decimal(balance, "The balance"),
        currency=currency.upper()[:3],
        as_of=_as_date(as_of) if as_of else None,
        horizon_days=horizon_days,
    )
    return result


@app.post("/api/affordability", response_model=AffordabilityResult)
async def post_affordability(request: Request, body: AffordabilityRequest):
    """"Can I afford this?" — the forecast re-run with the purchase inserted."""
    amount = _decimal(body.amount, "The amount", required=True)
    if amount is None or amount <= 0:
        raise HTTPException(400, "Enter an amount greater than zero.")
    rows = await _history_rows(_actor(request), body.currency)
    try:
        result = insights.affordability(
            rows,
            insights.detect_recurring(rows),
            _decimal(body.balance, "Your current balance", required=True),
            amount,
            _as_date(body.date),
            cushion=_decimal(body.cushion, "The cushion") or Decimal("0"),
            horizon_days=body.horizon_days,
            currency=body.currency.upper()[:3],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


# ------------------------------------------------------------------------- ledger


@app.post("/api/pdf/inspect")
async def inspect_pdf(
    request: Request,
    file: Annotated[UploadFile, File()],
    lines: int = 40,
):
    """Report what the statement reader sees in a PDF. Nothing is stored.

    Exists because a failed parse is otherwise indistinguishable from a scanned PDF or an
    empty file: the user gets the same message either way. This returns the extracted
    text, the row count, and the balance check, so a layout that does not parse can be
    diagnosed without sending the statement anywhere.
    """
    data = _read_upload(file, MAX_UPLOAD_BYTES, "PDF")
    if not statement_pdf.is_pdf(data):
        raise HTTPException(400, "That is not a PDF.")

    report: dict = {"bytes": len(data), "pages": statement_pdf.page_count(data)}
    try:
        positional = statement_pdf.extract_positional_text(data)
    except statement_pdf.StatementError as exc:
        report["positional_error"] = str(exc)
        positional = ""
    try:
        plain = statement_pdf.extract_text(data)
    except statement_pdf.StatementError as exc:
        report["text_error"] = str(exc)
        plain = ""

    report["text_chars"] = len(plain.strip())
    report["head"] = "\n".join(
        [line for line in (positional or plain).splitlines() if line.strip()][: max(1, min(lines, 200))]
    )

    for label, text in (("positional", positional), ("plain", plain)):
        if not text.strip():
            report[f"{label}_rows"] = 0
            continue
        try:
            parsed = statement_pdf.parse_statement_text(text)
        except statement_pdf.StatementError as exc:
            report[f"{label}_rows"] = 0
            report[f"{label}_error"] = exc.message
            report[f"{label}_detail"] = exc.detail
        else:
            report[f"{label}_rows"] = len(parsed["rows"])
            report[f"{label}_balance_ok"] = parsed["balance_check"]["ok"]

    return report


@app.post("/api/ledger/import", response_model=ImportResult)
async def import_csv(
    request: Request,
    file: Annotated[UploadFile | None, File()] = None,
    text: Annotated[str, Form()] = "",
    account: Annotated[str, Form()] = "Unassigned",
    currency: Annotated[str, Form()] = "SGD",
    commit: Annotated[bool, Form()] = False,
):
    """Parse a transactions CSV *or statement PDF*; store it only when ``commit`` is true.

    Preview-then-commit is deliberate: the column guesses and the per-line errors are
    what the user needs to see *before* the numbers land in their history.

    A PDF goes through the statement reader (exact text parse, or vision for a scan) and
    then the same preview/commit path as a CSV, so history can be bulk-loaded from the
    file the bank actually gives you.
    """
    uploaded = file is not None and bool(file.filename)
    payload = _read_upload(file, MAX_UPLOAD_BYTES, "file") if uploaded else b""

    if uploaded and statement_pdf.is_pdf(payload):
        try:
            extracted, reader_warnings = await run_in_threadpool(
                _extract_statement, payload, file.content_type
            )
        except statement_pdf.StatementError as exc:
            raise HTTPException(422, _statement_detail(exc)) from exc
        except extract.ExtractionError as exc:
            raise HTTPException(422, str(exc)) from exc
        # A PDF's date order is the statement's, so hand it to the import pipeline with
        # the clock icon: rows already stored are skipped, the rest are added.
        parsed = {
            "rows": [
                {
                    "date": row["date"],
                    "description": row["description"],
                    "amount": workbook.format_amount(row["amount"]),
                    "currency": row["currency"],
                }
                for row in extracted["transactions"]
            ],
            "errors": list(reader_warnings),
            "columns": {"source": "statement PDF"},
        }
        if not parsed["rows"]:
            return ImportResult(
                committed=False,
                parsed=0,
                added=0,
                skipped=0,
                errors=parsed["errors"] or ["No transactions were found in that PDF."],
                columns={},
            )
    elif uploaded or text.strip():
        raw = payload.decode("utf-8-sig", errors="replace") if uploaded else text
        try:
            parsed = ledger.parse_csv(raw, default_currency=currency)
        except ledger.ImportError_ as exc:
            raise HTTPException(422, str(exc)) from exc
    else:
        raise HTTPException(400, "Attach a CSV or PDF file, or paste the CSV contents.")

    if commit:
        result = await book.add(_actor(request), account, parsed["rows"], source="import")
        added, skipped = result["added"], result["skipped"]
    else:
        # Preview: nothing is written, so "added" would be a lie. Report how many rows
        # are understood and not already stored — what committing would actually add.
        actor = _actor(request)
        known = {
            workbook.fingerprint(row["date"], row["description"], Decimal(row["amount"]))
            for row in await book.rows(actor)
        }
        fresh = [
            row
            for row in parsed["rows"]
            if workbook.fingerprint(row["date"], row["description"], Decimal(row["amount"]))
            not in known
        ]
        added = len(fresh)
        skipped = len(parsed["rows"]) - added

    return ImportResult(
        committed=bool(commit),
        parsed=len(parsed["rows"]),
        added=added,
        skipped=skipped,
        errors=parsed["errors"],
        columns=parsed["columns"],
        rows_preview=[RowIn(**row) for row in parsed["rows"][:50]],
    )


@app.post("/api/ledger/rows", response_model=ImportResult)
async def add_rows(request: Request, body: LedgerAddRequest):
    """Add transactions by hand — for the odd cash purchase, or history you typed in."""
    rows = []
    for index, row in enumerate(body.rows, start=1):
        day = insights.parse_day(row.date)
        if day is None:
            raise HTTPException(400, f"Row {index}: unreadable date {row.date!r}.")
        amount = _decimal(row.amount, f"Row {index} amount", required=True)
        if amount is None or amount == 0:
            raise HTTPException(400, f"Row {index}: enter a non-zero amount.")
        rows.append(
            {
                "date": day.isoformat(),
                "description": row.description,
                "amount": workbook.format_amount(amount),
                "currency": (row.currency or body.currency),
            }
        )
    if not rows:
        raise HTTPException(400, "There are no rows to add.")
    result = await book.add(_actor(request), body.account, rows, source="manual")
    return ImportResult(
        committed=True,
        parsed=len(rows),
        added=result["added"],
        skipped=result["skipped"],
        columns={},
        rows_preview=[RowIn(**row) for row in rows[:50]],
    )


@app.get("/api/ledger", response_model=LedgerList)
async def list_ledger(request: Request, limit: int = 100, currency: str | None = None):
    rows = await book.recent(_actor(request), limit=limit)
    if currency:
        rows = [r for r in rows if r["currency"] == currency.upper()[:3]]
    return LedgerList(rows=[LedgerRow(**row) for row in rows], total=len(rows))


@app.delete("/api/ledger", response_model=ClearResult)
async def clear_ledger(request: Request):
    """Erase this user's stored transactions. Screenshots and workbooks were never stored."""
    deleted = await book.clear(_actor(request))
    return ClearResult(deleted=deleted)


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})