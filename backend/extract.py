"""Turn a bank-statement screenshot into structured transactions with a vision model.

Talks to **OpenRouter** (https://openrouter.ai), which exposes an OpenAI-compatible
`/chat/completions` endpoint and routes to many providers behind one key. We call it over
plain HTTP rather than the OpenAI SDK so a provider quirk stays debuggable: the request and
response are both visible in the code below.

Two things make the extraction reliable:

* the image goes in as a base64 `image_url` data URI, which every vision provider behind
  OpenRouter accepts;
* we ask for JSON via `response_format` and, if the chosen model supports it, a strict
  `json_schema`. When a model doesn't support schema-constrained output we fall back to
  `{"type": "json_object"}` and then to lenient parsing, so the app still works — it just
  trusts the prompt more.
"""

from __future__ import annotations

import base64
import json
import os
import re
from decimal import Decimal, InvalidOperation

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3.5-super-vl:free")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
REQUEST_TIMEOUT = 120.0

# Vision needs a raster format; check magic bytes rather than trusting the filename.
_MEDIA_TYPES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}

SYSTEM_PROMPT = """You read bank transaction screenshots from Singapore banks (UOB, DBS, \
OCBC, POSB, HSBC, StanChart, Wise, Revolut and similar) and return them as structured data \
for bookkeeping.

Extract every transaction row you can see. For each one:

- `date`: ISO `YYYY-MM-DD`, using the date shown for that row (or the statement date if a \
section header supplies it). If you truly cannot tell, use null.
- `description`: the merchant or payee text, kept as printed. You may tidy wrapping and \
collapse repeated whitespace, but keep the identifying words (transfer references, card \
digits, payee names).
- `amount`: a signed number as a STRING. Money leaving the account (debit, payment, GIRO \
deduction, card spend, transfer out, ATM withdrawal) is NEGATIVE. Money coming in (credit, \
deposit, salary, incoming FAST/PayNow transfer, interest) is POSITIVE. Signs in the \
screenshot take priority: if the bank prints "-172.32" or "172.32 DR", the amount is \
-172.32; if it prints "+203.73" or "203.73 CR", the amount is +203.73. Never guess a sign \
from a description alone when an explicit sign or DR/CR marker is shown.
- `currency`: the ISO code if visible (e.g. "SGD"), else null.

Rules:
- Read the digits exactly. Amounts are usually two decimal places; do not invent rounding.
- Ignore non-transaction chrome: account balances, available credit, headers, "show more" \
links, running totals, and running-balance columns. A row that only displays a balance is \
not a transaction.
- If a row shows both a debit and a credit column, use whichever is filled in and sign it.
- If the screenshot is not a statement of transactions at all, return an empty list.

Do not merge rows, and do not add transactions that are not visible."""

# Strict-mode JSON schema. Every field is a plain type with an empty string meaning
# "unknown" — nullable unions (`["string", "null"]`) are legal JSON Schema but several
# provider backends behind OpenRouter reject them, and an empty string is converted back
# to None below. Maximum compatibility for something we don't control.
_SCHEMA = {
    "type": "object",
    "properties": {
        "account_hint": {
            "type": "string",
            "description": "Account name/number shown in the screenshot, or empty string if none.",
        },
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "ISO YYYY-MM-DD, or empty."},
                    "description": {"type": "string"},
                    "amount": {"type": "string", "description": "Signed decimal as a string."},
                    "currency": {"type": "string", "description": "ISO code, or empty."},
                },
                "required": ["date", "description", "amount", "currency"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["account_hint", "transactions"],
    "additionalProperties": False,
}


def _blank_to_none(value: object) -> str | None:
    """Map the schema's empty-string 'unknown' back to None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None

USER_PROMPT = "Extract every transaction from this screenshot."


class ExtractionError(Exception):
    """A failure the user can act on — surfaced as a 422, never a 500."""


def sniff_media_type(data: bytes, declared: str | None = None) -> str:
    for magic, media_type in _MEDIA_TYPES.items():
        if data.startswith(magic):
            return media_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ExtractionError(
        "That file is not a PNG, JPEG, GIF or WebP image. Screenshot the transactions and "
        "upload the image itself."
    )


def has_api_key() -> bool:
    """Whether a usable-looking OpenRouter key is configured. Cheap — for /api/config."""
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    return bool(key) and key.startswith("sk-or-")


def _api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise ExtractionError(
            "No OPENROUTER_API_KEY is configured for this app. Add it under the app's "
            "settings (Environment variables / Secrets) and try again."
        )
    if not key.startswith("sk-or-"):
        raise ExtractionError(
            "OPENROUTER_API_KEY does not look like an OpenRouter key (they start with "
            "'sk-or-'). Check the value in the app's settings."
        )
    return key


def _headers(key: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    # Optional attribution headers OpenRouter uses for its leaderboard. Only sent when
    # configured, so we don't publish a URL the user never chose.
    referer = os.getenv("OPENROUTER_SITE_URL", "").strip()
    title = os.getenv("OPENROUTER_APP_NAME", "").strip()
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    return headers


def _body(model: str, data_uri: str, structured: bool) -> dict:
    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": USER_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 4000,
    }
    if structured:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "transactions", "strict": True, "schema": _SCHEMA},
        }
    else:
        # Widely supported fallback: valid JSON, shape implied by the prompt.
        body["response_format"] = {"type": "json_object"}
    return body


class _UpstreamError(ExtractionError):
    """An error the upstream provider returned; carries the status so we can retry."""

    def __init__(self, message: str, status: int, detail: str) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


def _post(body: dict, key: str) -> dict:
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(OPENROUTER_URL, headers=_headers(key), json=body)
    except httpx.TimeoutException as exc:
        raise ExtractionError(
            "OpenRouter did not respond in time. Try again, or use a smaller screenshot."
        ) from exc
    except httpx.HTTPError as exc:
        raise ExtractionError("Could not reach OpenRouter. Check the app's network access.") from exc

    if response.status_code == 401:
        raise ExtractionError(
            "OpenRouter rejected the configured key. Check OPENROUTER_API_KEY in the app's "
            "settings."
        )
    if response.status_code == 402:
        raise ExtractionError(
            "The OpenRouter account has insufficient credit for this model. Top up, or "
            "switch OPENROUTER_MODEL to a free variant (slug ending in ':free')."
        )
    if response.status_code == 429:
        raise ExtractionError(
            "OpenRouter is rate-limiting this app right now. Free models are shared and "
            "often busy — wait a moment and retry."
        )
    if response.status_code >= 400:
        # Surface the upstream message: "Provider returned error" alone is useless for
        # working out whether it's the schema, the image, or the model being offline.
        detail = ""
        parsed = None
        try:
            parsed = response.json()
        except Exception:  # noqa: BLE001 - an HTML/proxy error page is fine to ignore
            parsed = None
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or "")
                meta = err.get("metadata") or {}
                raw = meta.get("raw") if isinstance(meta, dict) else None
                if raw:
                    detail = f"{detail} ({str(raw)[:300]})"
                provider = meta.get("provider_name") if isinstance(meta, dict) else None
                if provider:
                    detail = f"{detail} [provider: {provider}]"
            elif isinstance(err, str):
                detail = err
        hint = ""
        lowered = detail.lower()
        if "image" in lowered or "modal" in lowered or "vision" in lowered:
            hint = (
                f" The model '{body.get('model')}' may not accept images — pick a "
                "vision-capable model."
            )
        elif "no endpoints" in lowered or "unavailable" in lowered:
            hint = (
                " That model is offline at the provider right now. Try again, or set "
                "OPENROUTER_MODEL to another vision model."
            )
        raise _UpstreamError(
            f"OpenRouter returned {response.status_code}"
            + (f": {detail}" if detail else ".")
            + hint,
            response.status_code,
            detail,
        )

    try:
        return response.json()
    except ValueError as exc:
        raise ExtractionError("OpenRouter returned a response that was not JSON.") from exc


def _message_text(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise ExtractionError("OpenRouter returned no choices — the model may be unavailable.")
    message = choices[0].get("message") or {}
    content = message.get("content")
    # Some providers return a list of content parts even for text-only replies.
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not content:
        raise ExtractionError("The model returned an empty response for that image.")
    return str(content)


def _parse_json(text: str) -> dict:
    """Parse the model's reply, tolerating a fenced code block or surrounding prose."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ExtractionError("Could not read the model's response as JSON.")


def _to_decimal(raw: str, index: int) -> Decimal:
    cleaned = (
        str(raw)
        .replace(",", "")
        .replace("$", "")
        .replace("S$", "")
        .replace("−", "-")
        .replace("–", "-")
        .strip()
    )
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ExtractionError(
            f"Row {index} came back with an unreadable amount ({raw!r}). Re-take the "
            "screenshot or enter the row by hand."
        ) from exc


def extract(image_bytes: bytes, media_type: str | None = None) -> dict:
    """Return {"account_hint": str|None, "transactions": [parsed rows]}."""
    if not image_bytes:
        raise ExtractionError("The uploaded image is empty.")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ExtractionError("That image is larger than 5 MB — please use a smaller screenshot.")

    media = sniff_media_type(image_bytes, media_type)
    data_uri = f"data:{media};base64,{base64.standard_b64encode(image_bytes).decode('ascii')}"
    key = _api_key()

    # Ask for a strict schema first. Any rejection of the request shape — 400, 404, or the
# 422 some providers return for unsupported parameters — falls back once to a plain
# json_object, which nearly everything supports. Auth/credit/limit errors (401/402/429)
# are raised as plain ExtractionError and must not be retried.
    try:
        payload = _post(_body(MODEL, data_uri, structured=True), key)
    except _UpstreamError as exc:
        structured_rejected = exc.status in (400, 404, 422) and any(
            token in exc.detail.lower()
            for token in ("response_format", "json_schema", "schema", "structured")
        )
        if not structured_rejected:
            raise
        payload = _post(_body(MODEL, data_uri, structured=False), key)

    text = _message_text(payload)
    data = _parse_json(text)

    rows = []
    for index, item in enumerate(data.get("transactions") or [], start=1):
        if not isinstance(item, dict):
            continue
        raw_amount = str(item.get("amount") or "").strip()
        if not raw_amount:
            continue
        rows.append(
            {
                "date": _blank_to_none(item.get("date")),
                "description": str(item.get("description") or "").strip(),
                "amount": _to_decimal(raw_amount, index),
                "currency": _blank_to_none(item.get("currency")),
            }
        )

    finish = ((payload.get("choices") or [{}])[0] or {}).get("finish_reason")
    truncated = finish == "length" and bool(rows)

    return {
        "account_hint": data.get("account_hint"),
        "transactions": rows,
        "truncated": truncated,
    }