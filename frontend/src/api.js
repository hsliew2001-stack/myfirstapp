// Same-origin API calls. The ingress routes /api to the backend; Vite's dev server
// proxies the same paths to :8000 locally, so nothing absolute is ever needed here.

// Gateway errors arrive as a bare status with no JSON body, so a stack trace of numbers
// is all the user would otherwise see. Say what it means and what to do instead.
const GATEWAY = {
  502: "The app is restarting or unreachable. Wait a moment and try again.",
  503: "The app is temporarily unavailable. Try again shortly.",
  504:
    "Reading that file took longer than the server allows, so the request was cut off. " +
    "A scanned or very long PDF is the usual cause — try a statement with fewer pages, " +
    "upload a page as a screenshot, or import the transactions as a CSV instead.",
};

async function fail(response) {
  let detail = GATEWAY[response.status] || `Request failed (${response.status}).`;
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") detail = body.detail;
    else if (Array.isArray(body?.detail)) {
      detail = body.detail.map((d) => d.msg ?? String(d)).join("; ");
    }
  } catch {
    // Non-JSON error body (typically an ingress 502/504) — keep the status-based message.
  }
  return new Error(detail);
}

async function json(response) {
  if (!response.ok) throw await fail(response);
  return response.json();
}

export const api = {
  config() {
    return fetch("/api/config").then((r) => (r.ok ? r.json() : fail(r)));
  },

  // ---- insights: recurring bills and the forward forecast -------------------

  insights({ balance, currency = "SGD", horizonDays = 45 } = {}) {
    const params = new URLSearchParams({ currency, horizon_days: String(horizonDays) });
    // Omit a blank balance rather than sending "" — the difference between "none given"
    // and "zero" changes what the forecast means.
    if (balance !== undefined && balance !== null && String(balance).trim() !== "") {
      params.set("balance", String(balance).trim());
    }
    return fetch(`/api/insights?${params}`).then(json);
  },

  affordability({ amount, date, balance, cushion, currency = "SGD", horizonDays = 45 }) {
    return fetch("/api/affordability", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        amount,
        date: date || null,
        balance: balance !== "" && balance != null ? String(balance) : null,
        cushion: cushion !== "" && cushion != null ? String(cushion) : "0",
        currency,
        horizon_days: horizonDays,
      }),
    }).then(json);
  },

  ledger(limit = 100) {
    return fetch(`/api/ledger?limit=${limit}`).then(json);
  },

  importCsv({ file, text, account, currency = "SGD", commit = false }) {
    const form = new FormData();
    if (file) form.append("file", file);
    else form.append("text", text || "");
    form.append("account", account || "Unassigned");
    form.append("currency", currency);
    form.append("commit", commit ? "true" : "false");
    return fetch("/api/ledger/import", { method: "POST", body: form }).then(json);
  },

  addRows({ account, currency = "SGD", rows }) {
    return fetch("/api/ledger/rows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account, currency, rows }),
    }).then(json);
  },

  clearLedger() {
    return fetch("/api/ledger", { method: "DELETE" }).then(json);
  },

  async analyze({ screenshot, workbook, label }) {
    const form = new FormData();
    form.append("screenshot", screenshot);
    form.append("workbook_file", workbook);
    form.append("label", label);
    const response = await fetch("/api/analyze", { method: "POST", body: form });
    if (!response.ok) throw await fail(response);
    return response.json();
  },

  async apply({ workbook, label, sheet, cell, rows, compact }) {
    const form = new FormData();
    form.append("workbook_file", workbook);
    form.append("label", label);
    form.append("sheet", sheet);
    form.append("cell", cell);
    form.append("compact", compact ? "true" : "false");
    form.append(
      "rows_json",
      JSON.stringify(
        rows.map((row) => ({
          date: row.date || null,
          description: row.description || "",
          amount: row.amount,
          currency: row.currency || null,
        }))
      )
    );

    const response = await fetch("/api/apply", { method: "POST", body: form });
    if (!response.ok) throw await fail(response);

    const disposition = response.headers.get("Content-Disposition") || "";
    const match = /filename="?([^"]+)"?/.exec(disposition);
    return {
      blob: await response.blob(),
      headers: {
        filename: match?.[1] || "workbook-updated.xlsx",
        cell: response.headers.get("X-Cell") || "",
        before: response.headers.get("X-Formula-Before") || "",
        after: response.headers.get("X-Formula-After") || "",
        delta: response.headers.get("X-Delta") || "",
      },
    };
  },
};

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Give the browser a moment to start the download before freeing the URL.
  setTimeout(() => URL.revokeObjectURL(url), 30_000);
}