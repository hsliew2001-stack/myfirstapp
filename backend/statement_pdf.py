"""Read a bank statement PDF into transactions.

**Deterministic first, vision only as a fallback.** A statement PDF that carries a text
layer is a table of exact strings; running it through a vision model would turn exact
digits into guesses and cost money for the privilege. So the primary path parses the text
layer directly, and the vision model is used only for a *scanned* PDF (one with no text),
where there is nothing else to read.

The format this is built for is the common Singapore current-account export:

    Date,Description,Withdrawal_SGD,Deposit_SGD,Balance_SGD
    2026-11-12,Inward DR - GIRO ... DINERS CLUB (S) PL,172.32,0.00,1127.68

Three things make it reliable rather than merely plausible:

* **Description wrapping.** Statements wrap a long payee over several lines. A line that
  does not start with a date continues the previous row's description, which is how
  "PAYNOW-FAST OTHR qsb-sqr-sg- ... Qashier-XI AN Noodl" stays one row on the page.
* **Sign comes from the column, not the wording.** A `Withdrawal` amount is negative and a
  `Deposit` amount is positive. Nothing is inferred from "Inward"/"Credit"/"DR" in the
  text, because those words describe the rail, not the direction of the money.
* **The running balance is checked.** Statements print a balance after every row, so the
  rows must satisfy `previous_balance + amount == balance`. If they don't, the parse is
  wrong or a row was missed — and the user is told, rather than being handed a plausible
  list that quietly omits a transaction.
"""

from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation

import pdfplumber

#: Statement pages handed to the vision model when there is no text layer. Each page is a
#: model call, so this bounds both the bill and the wait.
#: Each page costs a model call, and the whole request must finish inside the ingress
#: timeout (~60s). Four pages read concurrently is the most that reliably fits; a longer
#: statement is read in chunks rather than failing as a bare 504.
MAX_VISION_PAGES = 4
VISION_RENDER_SCALE = 2.0
#: The vision model's per-image limit (same as a screenshot upload), kept in sync with
#: extract.MAX_IMAGE_BYTES so a rendered page is never rejected after the fact.
MAX_VISION_BYTES = 5 * 1024 * 1024

_DATE = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})"
_DATE_AT_START = re.compile(rf"^\s*({_DATE})\b")
_AMOUNT = r"(?:\d{1,3}(?:,\d{3})*|\d+)\.\d{2}"
# An amount token, optionally signed or bracketed, optionally tagged CR/DR.
_AMOUNT_TOKEN = re.compile(rf"^\(?-?{_AMOUNT}\)?(?:\s*(?:CR|DR))?$", re.I)
_ANY_NUMBER = re.compile(r"\(?-?\d[\d,]*\.\d{2}\)?")

_HEADER_HINTS = ("withdrawal", "deposit", "balance", "description")
_NOISE = re.compile(
    r"^(page\s+\d+\s+of\s+\d+|continued|\d+/\d+)$", re.I
)

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%d %b %Y", "%d %B %Y")


class StatementError(Exception):
    """A problem the user can act on — surfaced as a 422.

    ``sample`` carries the first few lines the reader actually saw. Without it a failure
    like "no dated rows" is unfalsifiable from the outside: the same message appears for
    an empty PDF, a scanned PDF, and a table whose shape is simply unrecognised.
    """

    def __init__(self, message: str, sample: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.sample = sample
        self.detail = detail


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _parse_date_text(text: str) -> date | None:
    cleaned = " ".join(text.split())
    for fmt in _DATE_FORMATS:
        try:
            from datetime import datetime

            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(text: str) -> Decimal | None:
    raw = text.replace(",", "").replace("(", "").replace(")", "").strip()
    raw = re.sub(r"(?i)\s*(cr|dr)$", "", raw).strip()
    negative = text.strip().startswith("(")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return -abs(value) if negative else value


def _looks_like_header(line: str) -> bool:
    lowered = line.lower()
    hits = sum(1 for hint in _HEADER_HINTS if hint in lowered)
    return hits >= 2


def _layout_from_header(line: str) -> dict | None:
    """Work out which money columns the statement prints, from its header line.

    This matters because two numeric columns are ambiguous: they may be
    withdrawal+deposit (no running balance) or amount+balance. Guessing wrong would
    silently reinterpret every row, so the header — which the statement always prints —
    decides instead.
    """
    lowered = line.lower()
    has_withdrawal = "withdrawal" in lowered or "debit" in lowered
    has_deposit = "deposit" in lowered or "credit" in lowered
    has_balance = "balance" in lowered
    # A header naming both a debit-ish and a credit-ish column (plus optionally a balance)
    # is the withdrawal/deposit layout; otherwise rows are amount+balance.
    split = has_withdrawal and has_deposit
    columns = (2 if split else 1) + (1 if has_balance else 0)
    if columns < 2:
        return None
    return {"split": split, "balance": has_balance, "columns": columns}


def _money_tokens(line: str) -> list[str]:
    """The trailing money columns on a line, with any CR/DR tag re-joined to its amount.

    A tag can be split off by whitespace ("5000.00 CR"), so re-join it first: otherwise
    the tag looks like a column of its own and the amount never reaches the row.
    """
    tokens = _tokens_of(line)
    rejoined: list[str] = []
    for token in tokens:
        if (
            re.fullmatch(r"(?i)(CR|DR)", token)
            and rejoined
            and _AMOUNT_TOKEN.match(rejoined[-1])
        ):
            rejoined[-1] = f"{rejoined[-1]} {token}"
        else:
            rejoined.append(token)
    money: list[str] = []
    while rejoined and _AMOUNT_TOKEN.match(rejoined[-1]) and len(money) < 3:
        money.insert(0, rejoined.pop())
    return money


def _tokens_of(line: str) -> list[str]:
    # Comma-separated rows show up when a statement is exported as CSV but pasted through
    # the PDF path, so treat commas as column separators here too.
    if line.count(",") >= 2:
        return " ".join(part for part in re.split(r"\s*,\s*", line) if part).split()
    return line.split()


def _strip_money(line: str) -> str:
    """Return a line with its trailing money columns removed (the description text)."""
    tokens = _tokens_of(line)
    rejoined: list[str] = []
    for token in tokens:
        if (
            re.fullmatch(r"(?i)(CR|DR)", token)
            and rejoined
            and _AMOUNT_TOKEN.match(rejoined[-1])
        ):
            rejoined[-1] = f"{rejoined[-1]} {token}"
        else:
            rejoined.append(token)
    while rejoined and _AMOUNT_TOKEN.match(rejoined[-1]):
        rejoined.pop()
    return " ".join(rejoined).strip()


def _finish_block(block: dict, layout: dict | None) -> dict | None:
    """Turn one date-block into a signed transaction, or None if it isn't one."""
    when = _parse_date_text(block["date_text"])
    if when is None:
        return None

    # Find the money: the line in the block that carries the money columns. Everything up
    # to and including that line describes this transaction; anything after it belongs to
    # this row only if it is text (a wrapped tail), which the block loop already folded in.
    money_index = None
    for index, line in enumerate(block["lines"]):
        if len(_money_tokens(line)) >= 2:
            money_index = index
    if money_index is None:
        block["money_line"] = ""
        block["description"] = " ".join(block["lines"]).strip()
        return None

    money_line = block["lines"][money_index]
    description_parts = [
        _strip_money(line) if index == money_index else line
        for index, line in enumerate(block["lines"])
    ]
    block["money_line"] = money_line
    block["description"] = " ".join(part for part in description_parts if part).strip()

    if not money_line:
        return None

    money = _money_tokens(money_line)
    values = [_parse_amount(value) for value in money]
    if any(value is None for value in values):
        return None

    withdrawal = deposit = Decimal("0")
    balance = None
    if layout is None:
        # No header seen: fall back to shape. Three columns is unambiguous; two is read
        # as amount+balance, which is rarer but self-consistent.
        if len(values) >= 3:
            withdrawal, deposit, balance = values[:3]
        elif len(values) == 2:
            first, balance = values
            if re.search(r"(?i)\bCR\b", money[0]):
                deposit = abs(first)
            else:
                withdrawal = abs(first)
        else:
            return None
    elif layout["split"]:
        if len(values) < 2:
            return None
        withdrawal, deposit = values[0], values[1]
        if layout["balance"] and len(values) >= 3:
            balance = values[2]
    else:
        amount = values[0]
        if len(values) > 1:
            balance = values[1]
        if re.search(r"(?i)\bCR\b", money[0]):
            deposit = abs(amount)
        else:
            withdrawal = abs(amount)

    withdrawal = abs(withdrawal or Decimal("0"))
    deposit = abs(deposit or Decimal("0"))
    if withdrawal == 0 and deposit == 0:
        return None

    amount = deposit - withdrawal
    return {
        "date": when.isoformat(),
        "description": block["description"],
        "amount": format(amount.quantize(Decimal("0.01")), "f"),
        "currency": "SGD",
        "balance": None if balance is None else format(balance.quantize(Decimal("0.01")), "f"),
    }


_LOOSE_DATE = re.compile(
    r"(?<!\d)(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9})(?!\d)"
)
# Any decimal amount, with or without thousands separators. The integer part must allow
# four or more digits: a balance like 13280.42 is common and an earlier \d{1,3} bound made
# those amounts invisible, which silently dropped rows rather than failing loudly.
_DECIMAL = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+\.\d{2}(?![\d])|(?<![\d.,])\d+\.\d{2}(?![\d])")


def parse_lenient(text: str) -> dict:
    """Last-resort parse: find a date and the amounts wherever they sit on a line.

    The strict parser asks the line to *look like* a statement row — date at the start,
    money at the end. Real statements from some banks put the description first, split
    columns across lines the extractor joined oddly, or use a date format the strict
    reader does not expect. Rather than fail outright, this scans each line for any date
    and any money tokens and reads them positionally by value.

    It is deliberately permissive, so its output is never trusted silently: the caller
    reports that a lenient read was used, and the running-balance check still runs — a
    wrong row almost always breaks the balance chain, which is what makes this safe to
    offer at all.
    """
    rows: list[dict] = []
    for line in text.splitlines():
        if not line.strip() or _looks_like_header(line):
            continue
        date_match = _LOOSE_DATE.search(line)
        if date_match is None:
            continue
        when = _parse_date_text(date_match.group(1))
        if when is None:
            continue

        amounts = [(m.start(), m.group(0)) for m in _DECIMAL.finditer(line)]
        # Ignore numbers that are part of the date itself ("2026-11-12").
        amounts = [(pos, raw) for pos, raw in amounts if not (date_match.start() <= pos < date_match.end())]
        if len(amounts) < 2:
            continue

        values = [_parse_amount(raw) for _, raw in amounts]
        if any(v is None for v in values):
            continue

        # Withdrawal / Deposit / Balance, in printed order. A row with only two numbers
        # is read as amount + balance.
        if len(values) >= 3:
            withdrawal, deposit, balance = values[0], values[1], values[2]
        else:
            withdrawal, deposit, balance = values[0], Decimal("0"), values[1]

        withdrawal = abs(withdrawal or Decimal("0"))
        deposit = abs(deposit or Decimal("0"))
        if withdrawal == 0 and deposit == 0:
            continue

        # The description is the text with the date and the money removed. Blank the spans
        # by position rather than replacing the first matching substring: "5000.00" also
        # contains "500", so a string replace leaves fragments behind.
        chars = list(line)
        for (start, raw) in amounts:
            for index in range(start, start + len(raw)):
                chars[index] = " "
        for index in range(date_match.start(), date_match.end()):
            chars[index] = " "
        desc = re.sub(r"\s{2,}", " ", "".join(chars)).strip(" ,;|-")

        rows.append(
            {
                "date": when.isoformat(),
                "description": desc,
                "amount": format((deposit - withdrawal).quantize(Decimal("0.01")), "f"),
                "currency": "SGD",
                "balance": None if balance is None else format(balance.quantize(Decimal("0.01")), "f"),
            }
        )

    if not rows:
        sample_lines = [line for line in text.splitlines() if line.strip()][:8]
        raise StatementError(
            "No transaction rows could be read from that PDF's text.",
            sample="\n".join(sample_lines),
            detail=(
                f"{len(text.splitlines())} text line(s) were found, and none carried a "
                "readable date together with at least two amounts."
            ),
        )

    balance_check = verify_running_balance(rows)
    warnings = []
    if balance_check["checked"] and not balance_check["ok"]:
        warnings.append(balance_check["message"])
    return {"rows": rows, "warnings": warnings, "balance_check": balance_check}


def parse_statement_text(text: str) -> dict:
    """Parse the text layer of a statement into signed transactions.

    Returns ``{"rows", "warnings", "balance_check"}``. Rows come out **newest first**,
    in the order the statement prints them, because that order is what makes the running
    balance chain verifiable.
    """
    rows: list[dict] = []
    warnings: list[str] = []
    skipped: list[str] = []
    layout: dict | None = None

    # A transaction is a *block*, not a line: it begins wherever a date appears and its
    # money columns may sit on any later line of the block. That is how real statements
    # print a long payee — date on the first line, amounts on the last. Treating a line
    # as a transaction is what drops rows (or the whole statement) on a wrapped layout.
    block: dict | None = None

    def close_block() -> None:
        nonlocal block
        if block is None:
            return
        parsed = _finish_block(block, layout)
        if parsed is not None:
            rows.append(parsed)
        elif block["money_line"] or block["description"]:
            skipped.append(block["first_line"])
        block = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _NOISE.match(line):
            continue
        if _looks_like_header(line):
            layout = _layout_from_header(line) or layout
            continue

        match = _DATE_AT_START.match(line)
        if match is None:
            # Continuation of the block in progress: more description, and possibly the
            # money columns that the wrapped description pushed onto this line.
            if block is not None:
                block["lines"].append(line)
            elif _ANY_NUMBER.search(line):
                skipped.append(line)
            continue

        # A new dated line ends the previous transaction. The block stays open until the
        # next date so that a description wrapped *after* the amounts is still captured —
        # statements put the money on the first line or the last, not always the same one.
        close_block()
        block = {
            "date_text": match.group(1),
            "lines": [line[match.end() :].strip()],
            "first_line": line,
        }

    close_block()

    if not rows:
        # Include what was actually read. A statement that "has no dated rows" according
        # to the reader is a bug report; showing the first lines makes it a diagnosis.
        sample_lines = [line for line in text.splitlines() if line.strip()][:8]
        raise StatementError(
            "No transaction rows could be read from that PDF's text.",
            sample="\n".join(sample_lines),
            detail=(
                f"{len(text.splitlines())} text line(s) were found, and none carried both a "
                "date and at least two amount columns."
            ),
        )

    if skipped:
        warnings.append(
            f"{len(skipped)} line(s) looked like transactions but could not be read and were "
            "left out — check the statement against the rows below."
        )

    balance_check = verify_running_balance(rows)
    if balance_check["checked"] and not balance_check["ok"]:
        warnings.append(balance_check["message"])

    return {"rows": rows, "warnings": warnings, "balance_check": balance_check}


def verify_running_balance(rows: list[dict]) -> dict:
    """Check that each row's printed balance follows from the one before it.

    Statements print the balance *after* each transaction and list rows newest first, so
    walking backwards through the list, each row's balance must equal the next row's
    balance plus its own signed amount. A mismatch means a row was missed or misread —
    exactly the failure a vision model makes silently.
    """
    with_balance = [row for row in rows if row.get("balance") not in (None, "")]
    if len(with_balance) < 2:
        return {"checked": False, "ok": True, "mismatches": [], "message": ""}

    expected_order = all(
        with_balance[i]["date"] >= with_balance[i + 1]["date"]
        for i in range(len(with_balance) - 1)
    )

    mismatches = 0
    for i in range(len(with_balance) - 1):
        current, previous = with_balance[i], with_balance[i + 1]
        amount = Decimal(current["amount"])
        balance = Decimal(current["balance"])
        prior = Decimal(previous["balance"])
        # Newest-first: the row above happened after the one below.
        if expected_order:
            ok = prior + amount == balance
        else:
            ok = balance + Decimal(previous["amount"]) == prior
        if not ok:
            mismatches += 1

    if mismatches == 0:
        return {"checked": True, "ok": True, "mismatches": 0, "message": ""}
    return {
        "checked": True,
        "ok": False,
        "mismatches": mismatches,
        "message": (
            f"The printed balances do not add up on {mismatches} row(s): a transaction in "
            "this statement was probably not read. Compare the rows below against the PDF "
            "before applying them."
        ),
    }


# ------------------------------------------------------------------------ vision path


def render_pages(data: bytes, limit: int = MAX_VISION_PAGES) -> list[bytes]:
    """Render PDF pages to PNG, for a statement with no text layer (a scan)."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - depends on deployment image
        raise StatementError(
            "This PDF has no text layer (it looks like a scan) and page rendering is "
            "unavailable in this build, so it cannot be read. Upload it as an image instead."
        ) from exc

    out: list[bytes] = []
    try:
        document = pdfium.PdfDocument(data)
        for index in range(min(len(document), limit)):
            bitmap = document[index].render(scale=VISION_RENDER_SCALE)
            image = bitmap.to_pil()
            png = io.BytesIO()
            image.save(png, format="PNG")
            payload = png.getvalue()
            # A big or very dense page can render past the model's image limit. Step the
            # scale down rather than fail: a slightly softer scan still reads, and the
            # rows are reviewed before anything is applied.
            scale = VISION_RENDER_SCALE
            while len(payload) > MAX_VISION_BYTES and scale > 0.8:
                scale -= 0.4
                shrunk = io.BytesIO()
                image.resize(
                    (max(1, int(image.width * scale / VISION_RENDER_SCALE)),
                     max(1, int(image.height * scale / VISION_RENDER_SCALE)))
                ).save(shrunk, format="PNG")
                payload = shrunk.getvalue()
            if len(payload) > MAX_VISION_BYTES:
                raise StatementError(
                    "A page of that PDF is too large to read even after shrinking it. "
                    "Upload that page as a screenshot instead."
                )
            out.append(payload)
    except StatementError:
        raise
    except Exception as exc:  # noqa: BLE001 - malformed or encrypted PDF
        raise StatementError(f"That PDF could not be rendered: {exc}") from exc
    return out


def page_count(data: bytes) -> int:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return len(pdf.pages)
    except Exception:  # noqa: BLE001 - not worth failing over
        return 0


def extract_text(data: bytes) -> str:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as exc:  # noqa: BLE001 - reports as unreadable to the user
        raise StatementError(f"That PDF could not be opened: {exc}") from exc


# ------------------------------------------------------------------- positional path


def extract_positional_text(data: bytes) -> str:
    """Rebuild statement lines from word coordinates instead of the PDF's line breaks.

    Some PDF generators emit text so that every *word* is its own line, or break a line
    at the column boundary rather than at a sensible place. Trusting line breaks then
    produces a document where no line holds two money columns, and the reader finds
    nothing — which is exactly how a perfectly readable statement gets rejected.

    Words carry their own coordinates, so grouping by vertical position reconstructs the
    visual lines the way a human sees them. This is the primary extraction path; the
    plain ``extract_text`` remains as a fallback.
    """
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            out: list[str] = []
            for page in pdf.pages:
                words = page.extract_words(
                    use_text_flow=False, keep_blank_chars=False, extra_attrs=["size"]
                )
                if not words:
                    continue
                # Group into visual lines: words whose vertical centre is within a
                # tolerance of the first word's. A percentage of the page height adapts
                # to different font sizes and page sizes.
                tolerance = max(2.0, float(page.height) * 0.006)
                words.sort(key=lambda w: (round(float(w["top"]), 1), float(w["x0"])))
                lines: list[list[dict]] = []
                for word in words:
                    top = float(word["top"])
                    placed = False
                    for line in lines:
                        if abs(top - float(line[0]["top"])) <= tolerance:
                            line.append(word)
                            placed = True
                            break
                    if not placed:
                        lines.append([word])
                lines.sort(key=lambda line: float(line[0]["top"]))
                for line in lines:
                    line.sort(key=lambda w: float(w["x0"]))
                    out.append(_join_words(line))
            return "\n".join(out)
    except Exception as exc:  # noqa: BLE001 - reports as unreadable to the user
        raise StatementError(f"That PDF could not be opened: {exc}") from exc


def _join_words(words: list[dict]) -> str:
    """Join a visual line's words, keeping column gaps as spaces.

    A gap much wider than a normal space is a column boundary in the original table.
    Two spaces there is enough to keep the text readable without inventing separators
    that the amount-token regex would not match.
    """
    parts: list[str] = []
    previous_end = None
    # A normal word space is about a quarter of the glyph height; a column gap is much
    # wider. Threshold on that ratio, not on absolute points, so the split works for any
    # font size. Getting this too large silently glues words together.
    heights = [float(w.get("height") or 8.0) for w in words]
    unit = sorted(heights)[len(heights) // 2] if heights else 8.0
    column_gap = unit * 1.2
    for word in words:
        if previous_end is not None:
            gap = float(word["x0"]) - previous_end
            parts.append("  " if gap > column_gap else " ")
        parts.append(word["text"])
        previous_end = float(word["x1"])
    return "".join(parts)