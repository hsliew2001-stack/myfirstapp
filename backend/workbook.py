"""Read and safely patch an .xlsx / .xlsm workbook.

**Why the two halves use different tools.**

Reading uses openpyxl. *Writing* does not: an openpyxl round-trip drops charts, images,
pivot tables and drawings, which would silently mangle a real budget workbook. Instead
``patch_cell`` edits exactly one ``<c>`` element inside the worksheet XML and copies every
other zip part through byte-for-byte — so charts, conditional formatting, VBA, defined
names and the rest all survive untouched.

The formula work is deliberately conservative: we only ever *append* signed literals
(``=1310.25`` -> ``=1310.25+203.73-172.32``) or, when asked, fold the whole history into
one total. Anything we don't understand raises a clear error rather than guessing.
"""

from __future__ import annotations

import ast
import io
import operator
import re
import zipfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape, unescape

from openpyxl import load_workbook

# Excel rejects formulas longer than 8192 characters; leave a little headroom.
MAX_FORMULA_LEN = 8000
MAX_EVAL_CHARS = 2000  # beyond this we skip the cosmetic cached value entirely
MAX_ROW_SCAN = 12  # cells to the right of a label we'll consider as the balance

_TWO_DP = Decimal("0.01")

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


# --------------------------------------------------------------------------- helpers


def normalize_label(text: object) -> str:
    """Lowercase and collapse whitespace, keeping word boundaries visible.

    Used for matching rows in the sheet ("UOB One Account" contains "uob one"), so the
    spaces matter. Two labels that differ only by punctuation are *not* equal here.
    """
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def compact_label(text: object) -> str:
    """Letters and digits only — for comparing text the model may have mangled.

    Vision models routinely drop or insert spaces around a word boundary ("UOBOne
    Account" for "UOB One Account"), which makes a whitespace-sensitive comparison
    report a mismatch the user would call a false alarm. Comparing the squashed form
    answers the question actually being asked: do these name the same account?
    """
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def parse_amount(value: object) -> Decimal:
    """Parse a money amount out of a string/number, accepting "$1,234.56" forms."""
    if value is None:
        raise ValueError("Empty amount")
    raw = str(value).strip()
    if not raw:
        raise ValueError("Empty amount")
    raw = raw.replace(",", "").replace("$", "").replace("S$", "")
    raw = raw.replace("−", "-").replace("–", "-").strip()
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Not a number: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"Not a finite number: {value!r}")
    if abs(amount) >= Decimal("1e12"):
        raise ValueError(f"Amount out of range: {value!r}")
    return _round_money(amount)


def _round_money(amount: Decimal) -> Decimal:
    exp = amount.as_tuple().exponent
    if isinstance(exp, int) and exp < -2:
        return amount.quantize(_TWO_DP, rounding=ROUND_HALF_UP)
    return amount


def format_amount(amount: Decimal) -> str:
    """Render an amount for a formula: no exponent, no separators, no plus sign."""
    return format(_round_money(amount), "f")


def fingerprint(date: str | None, description: str, amount: Decimal) -> str:
    """Stable identity for a transaction, used to spot a re-uploaded screenshot."""
    import hashlib

    bag = "|".join(
        [
            (date or "").strip(),
            normalize_label(description),
            format_amount(amount),
        ]
    )
    return hashlib.sha256(bag.encode("utf-8")).hexdigest()


def eval_arithmetic(body: str) -> Decimal | None:
    """Evaluate a plain ``+ - * /`` expression. Returns None for anything else.

    Used only for display/estimation and for the cached value we write into the cell.
    Supported formulas (SUM, cell refs, ...) return None and are left to Excel.

    Long expressions are declined rather than evaluated: CPython's parser recurses on
    depth, so a 500-term sums would blow the stack. Excel recalculates on open anyway,
    so declining only costs us a cosmetic cached value.
    """
    if len(body) > MAX_EVAL_CHARS:
        return None
    try:
        node = ast.parse(body, mode="eval").body
    except (SyntaxError, RecursionError, MemoryError, ValueError):
        return None

    def walk(n: ast.AST) -> Decimal:
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return Decimal(str(n.value))
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](walk(n.left), walk(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -walk(n.operand)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.UAdd):
            return walk(n.operand)
        raise ValueError("unsupported")

    try:
        result = walk(node)
    except (ValueError, ZeroDivisionError, InvalidOperation, ArithmeticError, RecursionError):
        return None
    if not result.is_finite():
        return None
    return result.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------------ discovery


def _is_formula(value: object) -> bool:
    return isinstance(value, str) and value.lstrip().startswith("=")


def _is_numeric(value: object) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def find_candidates(data: bytes, label: str) -> list[dict]:
    """Find cells whose row carries ``label`` and whose row holds a balance-ish value.

    Returns one candidate per matching label, nearest-balance-first within the row
    (the first formula or number to the right of the label).
    """
    needle = normalize_label(label)
    if not needle:
        return []

    workbook = load_workbook(io.BytesIO(data), data_only=False)
    out: list[dict] = []
    for sheet in workbook.worksheets:
        if sheet.sheet_state != "visible":
            continue
        for row in sheet.iter_rows():
            for cell in row:
                value = cell.value
                if not isinstance(value, str) or needle not in normalize_label(value):
                    continue
                balance = None
                for other in row[cell.column : cell.column + MAX_ROW_SCAN]:
                    if other.column <= cell.column:
                        continue
                    if _is_formula(other.value) or _is_numeric(other.value):
                        balance = other
                        break
                if balance is None:
                    continue
                out.append(
                    {
                        "sheet": sheet.title,
                        "label_cell": cell.coordinate,
                        "label": str(value).strip(),
                        "cell": balance.coordinate,
                        "formula": balance.value if _is_formula(balance.value) else None,
                        "raw_value": None if _is_formula(balance.value) else str(balance.value),
                    }
                )
    return out


def read_cell(data: bytes, sheet_name: str, coord: str) -> dict:
    """Describe one cell: its formula (if any) and its cached/displayed value."""
    coordinate = _clean_coord(coord)
    formula_wb = load_workbook(io.BytesIO(data), data_only=False)
    if sheet_name not in formula_wb.sheetnames:
        raise ValueError(f'No sheet named "{sheet_name}" in this workbook.')
    raw = formula_wb[sheet_name][coordinate].value
    # A second pass with data_only=True gets Excel's cached result for formula cells.
    cached = None
    if _is_formula(raw):
        values_wb = load_workbook(io.BytesIO(data), data_only=True)
        cached = values_wb[sheet_name][coordinate].value
    return {
        "sheet": sheet_name,
        "cell": coordinate,
        "formula": raw if _is_formula(raw) else None,
        "raw_value": None if _is_formula(raw) else (None if raw is None else str(raw)),
        "cached_value": cached if isinstance(cached, (int, float)) else None,
    }


def _clean_coord(coord: str) -> str:
    coordinate = (coord or "").strip().replace("$", "").upper()
    if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", coordinate):
        raise ValueError(f"Invalid cell reference: {coord!r}")
    return coordinate


# ------------------------------------------------------------------------- formula


def build_formula(
    old_formula: str | None,
    amounts: list[Decimal],
    compact: bool = False,
) -> tuple[str, Decimal | None]:
    """Append signed amounts to a balance cell.

    ``=1310.25`` + [203.73, -172.32] -> ``=1310.25+203.73-172.32``

    Returns (formula, estimated_new_value). The estimate is None when the existing
    formula is something we can't evaluate (a SUM over other cells, say).
    """
    if not amounts:
        raise ValueError("There are no transactions to append.")

    base = (old_formula or "").strip()
    if base.startswith("="):
        body = base[1:].strip()
    elif base == "":
        body = ""
    else:
        try:
            Decimal(base.replace(",", "").replace("$", ""))
        except InvalidOperation as exc:
            raise ValueError(
                "The target cell holds text, not a number or a formula — pick the cell that "
                "holds the account balance."
            ) from exc
        body = base.replace(",", "").replace("$", "")

    if body and body[-1] in "+-*/^(":
        raise ValueError(
            "The target formula ends with an operator, so appending to it would produce "
            "nonsense. Repair that cell first."
        )

    if compact and body:
        current = eval_arithmetic(body)
        if current is None:
            raise ValueError(
                "This cell's formula is too complex to fold into a single number. "
                "Turn off 'compact' to append the transactions instead."
            )
        total = current
        for amount in amounts:
            total += amount
        return "=" + format_amount(total), total

    terms = "".join(
        ("+" if amount >= 0 else "-") + format_amount(abs(amount)) for amount in amounts
    )

    if not body:
        # Nothing to append to: start the formula from the terms themselves.
        formula = "=" + (terms.lstrip("+") or "0")
    else:
        formula = "=" + body + terms

    if len(formula) > MAX_FORMULA_LEN:
        raise ValueError(
            f"The appended formula would be {len(formula)} characters, past Excel's "
            f"{MAX_FORMULA_LEN}-character limit. Tick 'compact' to fold the running "
            "history into a single total instead."
        )

    return formula, eval_arithmetic(formula[1:])


# -------------------------------------------------------------------------- patching


def _attr(attrs: str, name: str) -> str | None:
    match = re.search(rf'(?:^|\s){re.escape(name)}="([^"]*)"', attrs)
    return unescape(match.group(1)) if match else None


def _col_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index


def _sheet_part(zf: zipfile.ZipFile, sheet_name: str) -> str:
    workbook_xml = zf.read("xl/workbook.xml").decode("utf-8")
    rel_id = None
    for tag in re.findall(r"<sheet\b[^>]*>", workbook_xml):
        if unescape(_attr(tag, "name") or "") == sheet_name:
            rel_id = _attr(tag, "r:id")
            break
    if rel_id is None:
        raise ValueError(f'No sheet named "{sheet_name}" in this workbook.')

    rels_xml = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    target = None
    for tag in re.findall(r"<Relationship\b[^>]*>", rels_xml):
        if _attr(tag, "Id") == rel_id:
            target = _attr(tag, "Target")
            break
    if not target:
        raise ValueError("Could not resolve the worksheet part for that sheet.")

    target = target.lstrip("/")
    if not target.startswith("xl/"):
        target = "xl/" + target
    if target not in zf.namelist():
        raise ValueError("Worksheet part missing from the workbook archive.")
    return target


def _force_recalc(workbook_xml: str) -> str:
    """Ask Excel to recalculate every formula when the file is opened."""
    existing = re.search(r"<calcPr\b[^>]*/>", workbook_xml)
    if existing:
        tag = existing.group(0)
        if "fullCalcOnLoad=" in tag:
            new_tag = re.sub(r'fullCalcOnLoad="[^"]*"', 'fullCalcOnLoad="1"', tag)
        else:
            new_tag = tag[:-2].rstrip() + ' fullCalcOnLoad="1"/>'
        return workbook_xml[: existing.start()] + new_tag + workbook_xml[existing.end() :]

    calc = '<calcPr calcId="191029" fullCalcOnLoad="1"/>'
    for anchor in ("</definedNames>", "</sheets>"):
        at = workbook_xml.find(anchor)
        if at != -1:
            at += len(anchor)
            return workbook_xml[:at] + calc + workbook_xml[at:]
    return workbook_xml.replace("</workbook>", calc + "</workbook>")


def _patch_sheet_xml(xml: str, coord: str, formula: str, cached: Decimal | None) -> str:
    col_letters = re.match(r"([A-Z]+)", coord).group(1)
    col_index = _col_index(col_letters)
    row_number = int(coord[len(col_letters) :])

    # An empty sheet has <sheetData/>, which can't be searched into.
    empty = re.search(r"<sheetData\b[^>]*?/>", xml)
    if empty:
        xml = xml[: empty.start()] + "<sheetData/>" + xml[empty.end() :]

    block = re.search(r"(<sheetData\b[^>]*>)(.*?)</sheetData>", xml, re.S)
    if not block:
        raise ValueError("Unsupported worksheet layout (no <sheetData> element).")
    head, inner = block.group(1), block.group(2)

    value_xml = f"<v>{format(cached, 'f')}</v>" if cached is not None else ""
    cell_xml = f'<c r="{coord}"{{style}}><f>{escape(formula[1:], quote=False)}</f>{value_xml}</c>'

    row_re = re.compile(r"<row\b(?P<attrs>[^>]*?)(?:/>|>(?P<inner>.*?)</row>)", re.S)
    rows = list(row_re.finditer(inner))

    def with_style(attrs: str) -> str:
        style = _attr(attrs, "s")
        return cell_xml.format(style=f' s="{style}"' if style else "")

    # 1. The row, and the cell inside it, already exist.
    for row_match in rows:
        if _attr(row_match.group("attrs"), "r") != str(row_number):
            continue
        row_inner = row_match.group("inner")
        if row_inner is None:  # <row .../>
            row_inner = ""
            rebuilt = _insert_cell("", coord, col_index, with_style(""))
            replacement = f'<row{row_match.group("attrs")}>{rebuilt}</row>'
            inner = inner[: row_match.start()] + replacement + inner[row_match.end() :]
            break

        cell_re = re.compile(r"<c\b(?P<attrs>[^>]*?)(?:/>|>(?P<inner>.*?)</c>)", re.S)
        replaced = False
        for cell_match in cell_re.finditer(row_inner):
            if (_attr(cell_match.group("attrs"), "r") or "").upper() != coord:
                continue
            cell_inner = cell_match.group("inner") or ""
            if re.search(r'<f\b[^>]*\bt="(?:shared|array)"', cell_inner):
                raise ValueError(
                    f"Cell {coord} is part of a shared or array formula. Replacing it would "
                    "break the other cells that depend on it — choose a standalone balance cell."
                )
            row_inner = (
                row_inner[: cell_match.start()]
                + with_style(cell_match.group("attrs"))
                + row_inner[cell_match.end() :]
            )
            replaced = True
            break
        if not replaced:
            row_inner = _insert_cell(row_inner, coord, col_index, with_style(""))

        replacement = f'<row{row_match.group("attrs")}>{row_inner}</row>'
        inner = inner[: row_match.start()] + replacement + inner[row_match.end() :]
        break
    else:
        # 2. No row: create one in the right place.
        new_row = f'<row r="{row_number}">{with_style("")}</row>'
        inserted = False
        for row_match in rows:
            existing = _attr(row_match.group("attrs"), "r") or "0"
            if existing.isdigit() and int(existing) > row_number:
                inner = inner[: row_match.start()] + new_row + inner[row_match.start() :]
                inserted = True
                break
        if not inserted:
            inner = inner + new_row

    return xml[: block.start()] + head + inner + "</sheetData>" + xml[block.end() :]


def _insert_cell(row_inner: str, coord: str, col_index: int, cell_xml: str) -> str:
    """Insert a cell into a row, keeping columns in ascending order."""
    cell_re = re.compile(r"<c\b(?P<attrs>[^>]*?)(?:/>|>.*?</c>)", re.S)
    for cell_match in cell_re.finditer(row_inner):
        ref = (_attr(cell_match.group("attrs"), "r") or "").upper()
        letters = re.match(r"([A-Z]+)", ref)
        if letters and _col_index(letters.group(1)) > col_index:
            return row_inner[: cell_match.start()] + cell_xml + row_inner[cell_match.start() :]
    return row_inner + cell_xml


def patch_cell(data: bytes, sheet_name: str, coord: str, formula: str) -> bytes:
    """Write ``formula`` into one cell and return the re-zipped workbook."""
    coordinate = _clean_coord(coord)
    if not formula.startswith("="):
        raise ValueError("Internal error: refusing to write a non-formula value.")

    with zipfile.ZipFile(io.BytesIO(data)) as source:
        if "xl/workbook.xml" not in source.namelist():
            raise ValueError("That file does not look like a .xlsx or .xlsm workbook.")

        sheet_part = _sheet_part(source, sheet_name)
        new_sheet_xml = _patch_sheet_xml(
            source.read(sheet_part).decode("utf-8"),
            coordinate,
            formula,
            eval_arithmetic(formula[1:]),
        )
        new_workbook_xml = _force_recalc(source.read("xl/workbook.xml").decode("utf-8"))

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                if item.filename == sheet_part:
                    payload = new_sheet_xml.encode("utf-8")
                elif item.filename == "xl/workbook.xml":
                    payload = new_workbook_xml.encode("utf-8")
                else:
                    payload = source.read(item.filename)
                # Copying the ZipInfo keeps each part's original compression/date.
                target.writestr(item, payload)

    result = buffer.getvalue()

    # Verify: if openpyxl can't read it back with our formula in place, don't hand it out.
    try:
        check = load_workbook(io.BytesIO(result), data_only=False)
        written = check[sheet_name][coordinate].value
    except Exception as exc:  # noqa: BLE001 - any parse failure means we mangle nothing
        raise ValueError(f"The patched workbook failed validation: {exc}") from exc
    if written != formula:
        raise ValueError("The patched workbook failed validation: cell value did not stick.")
    return result