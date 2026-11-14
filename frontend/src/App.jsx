import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, downloadBlob } from "./api.js";
import Insights from "./Insights.jsx";
import {
  Amount,
  Banner,
  Card,
  CellPicker,
  Empty,
  RowEditor,
  StepNumber,
} from "./components.jsx";

const DEFAULT_LABEL = "UOB One";

const TABS = [
  { id: "reconcile", label: "Reconcile" },
  { id: "insights", label: "Insights" },
];

export default function App() {
  const [tab, setTab] = useState("reconcile");
  const [screenshot, setScreenshot] = useState(null);
  const [workbook, setWorkbook] = useState(null);
  const [label, setLabel] = useState(DEFAULT_LABEL);
  const [compact, setCompact] = useState(false);

  const [analysis, setAnalysis] = useState(null);
  const [rows, setRows] = useState([]);
  const [target, setTarget] = useState(null);
  const [busy, setBusy] = useState(null); // "analyze" | "apply" | null
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [config, setConfig] = useState(null);

  useEffect(() => {
    api.config().then(setConfig).catch(() => setConfig(null));
  }, []);

  const reset = () => {
    setAnalysis(null);
    setRows([]);
    setTarget(null);
    setResult(null);
    setError(null);
  };

  const onPick = (setter) => (file) => {
    setter(file);
    reset();
  };

  const total = useMemo(
    () =>
      rows.reduce((sum, row) => sum + (Number.parseFloat(row.amount) || 0), 0),
    [rows]
  );

  const analyze = async () => {
    setBusy("analyze");
    setError(null);
    setResult(null);
    try {
      const data = await api.analyze({ screenshot, workbook, label });
      setAnalysis(data);
      setRows(data.transactions);
      setTarget(data.candidates.length === 1 ? data.candidates[0] : null);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  const applyRows = async () => {
    setBusy("apply");
    setError(null);
    try {
      const { blob, headers } = await api.apply({
        workbook,
        label,
        sheet: target?.sheet ?? "",
        cell: target?.cell ?? "",
        rows,
        compact,
      });
      downloadBlob(blob, headers.filename);
      setResult({
        cell: `${target.sheet}!${target.cell}`,
        before: headers.before,
        after: headers.after,
        delta: headers.delta,
        count: rows.length,
      });
      setAnalysis(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  const readyToAnalyze = Boolean(screenshot && workbook && label.trim());
  const readyToApply =
    Boolean(workbook && rows.length) && Boolean(target?.sheet && target?.cell);

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto max-w-5xl px-6 py-5">
          <h1 className="text-xl font-semibold tracking-tight text-slate-900">
            Statement → Spreadsheet
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            Append a bank screenshot's transactions to your budget's balance formula.
          </p>
          <nav className="mt-4 flex gap-1 border-b border-slate-200" aria-label="Sections">
            {TABS.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setTab(item.id)}
                aria-current={tab === item.id ? "page" : undefined}
                className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition ${
                  tab === item.id
                    ? "border-slate-900 text-slate-900"
                    : "border-transparent text-slate-500 hover:text-slate-800"
                }`}
              >
                {item.label}
              </button>
            ))}
          </nav>
        </div>
      </header>

      {tab === "insights" ? (
        <main className="mx-auto max-w-5xl space-y-6 px-6 py-8">
          <Insights config={config} />
          <footer className="pb-4 text-center text-xs text-slate-400">
            Recurring bills and the projection are computed from stored transactions only.
            Clear them any time below.
          </footer>
        </main>
      ) : (
      <main className="mx-auto max-w-5xl space-y-6 px-6 py-8">
        {error && <Banner tone="error" title="That didn't work" body={error} />}
        {config && !config.has_api_key && (
          <Banner
            tone="warning"
            title="No OpenRouter API key configured"
            body="Add OPENROUTER_API_KEY under this app's environment variables before reading screenshots. You can create one at openrouter.ai/keys."
          />
        )}

        <Card>
          <StepNumber n={1} title="Your files" />
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <FileField
              label="Statement or screenshot"
              hint="A transactions PDF, or a PNG / JPEG / GIF / WebP screenshot"
              accept="image/*,.pdf,application/pdf"
              file={screenshot}
              preview
              onChange={onPick(setScreenshot)}
            />
            <FileField
              label="Excel workbook"
              hint=".xlsx or .xlsm — edited in place, everything else preserved"
              accept=".xlsx,.xlsm"
              file={workbook}
              onChange={onPick(setWorkbook)}
            />
          </div>
          <div className="mt-4">
            <label
              htmlFor="label"
              className="block text-sm font-medium text-slate-700"
            >
              Account row label
            </label>
            <p className="mt-0.5 text-xs text-slate-500">
              The app finds the row whose text contains this and appends to the balance
              cell in it.
            </p>
            <input
              id="label"
              value={label}
              onChange={(e) => {
                setLabel(e.target.value);
                reset();
              }}
              className="mt-2 w-full max-w-sm rounded-md border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-900 focus:outline-none focus:ring-1 focus:ring-slate-900"
              placeholder={DEFAULT_LABEL}
            />
          </div>
          <div className="mt-5">
            <button
              type="button"
              onClick={analyze}
              disabled={!readyToAnalyze || busy !== null}
              className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {busy === "analyze" ? "Reading statement…" : "Read transactions"}
            </button>
          </div>
        </Card>

        {analysis && (
          <Card>
            <StepNumber n={2} title="Review the transactions" />
            <SourceNote analysis={analysis} />
            {analysis.warnings.length > 0 && (
              <div className="mt-3 space-y-2">
                {analysis.warnings.map((w, i) => (
                  <Banner key={i} tone="warning" body={w} />
                ))}
              </div>
            )}
            {analysis.duplicates.length > 0 && (
              <div className="mt-3">
                <Banner
                  tone="warning"
                  title="Possible duplicates"
                  body={`These look like transactions you have already applied: ${analysis.duplicates.join(
                    "; "
                  )}`}
                />
              </div>
            )}
            {analysis.account_hint && (
              <p className="mt-3 text-xs text-slate-500">
                Screenshot refers to:{" "}
                <span className="font-medium text-slate-700">
                  {analysis.account_hint}
                </span>
              </p>
            )}

            {analysis.candidates.length > 0 && (
              <div className="mt-4">
                <CellPicker
                  candidates={analysis.candidates}
                  selected={target}
                  onSelect={setTarget}
                />
              </div>
            )}

            <div className="mt-5">
              <RowEditor rows={rows} onChange={setRows} />
            </div>

            <form
              className="mt-4 flex items-center gap-2 text-sm text-slate-600"
              onSubmit={(e) => e.preventDefault()}
            >
              <input
                id="compact"
                type="checkbox"
                checked={compact}
                onChange={(e) => setCompact(e.target.checked)}
                className="size-4 rounded border-slate-300"
              />
              <label htmlFor="compact">
                Fold into a single total{" "}
                <span className="text-xs text-slate-500">
                  (=3183.56 instead of appending each amount; helps once the formula gets long)
                </span>
              </label>
            </form>

            <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 pt-4">
              <div className="text-sm text-slate-600">
                {rows.length} transaction{rows.length === 1 ? "" : "s"} ·{" "}
                <Amount value={total} className="font-semibold" />
              </div>
              <div className="flex items-center gap-3">
                <PreviewNote target={target} rows={rows} compact={compact} />
                <button
                  type="button"
                  onClick={applyRows}
                  disabled={!readyToApply || busy !== null}
                  className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-300"
                >
                  {busy === "apply" ? "Writing workbook…" : "Apply & download"}
                </button>
              </div>
            </div>
          </Card>
        )}

        {result && (
          <Card>
            <StepNumber n={3} title="Done — your workbook is downloaded" />
            <p className="mt-2 text-sm text-slate-600">
              <span className="font-medium text-slate-900">{result.cell}</span> now reads{" "}
              <code className="break-all rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-800">
                {result.after.length > 160
                  ? `${result.after.slice(0, 120)}…${result.after.slice(-30)}`
                  : result.after}
              </code>
            </p>
            <p className="mt-2 text-xs text-slate-500">
              {result.count} transaction{result.count === 1 ? "" : "s"} applied (
              {result.delta}). Open the file in Excel to see the recalculated balance.
            </p>
          </Card>
        )}

        {!analysis && !result && (
          <Card>
            <Empty />
          </Card>
        )}

        <footer className="pb-4 text-center text-xs text-slate-400">
          The workbook never leaves this request. Every apply edits one cell, then your file
          downloads.
        </footer>
      </main>
      )}
    </div>
  );
}

// How the rows were read, and whether the statement's own arithmetic agrees with them.
// For a text PDF this is a positive check ("the balances add up"), which is the strongest
// reassurance the app can give — stronger than any vision-model confidence wording.
function SourceNote({ analysis }) {
  const check = analysis.balance_check;
  const source = analysis.source;

  if (check?.checked) {
    return (
      <div className="mt-3">
        {check.ok ? (
          <Banner
            tone="info"
            body="Read from the PDF's text, and every printed balance in the statement adds up — nothing appears to have been missed."
          />
        ) : (
          <Banner tone="warning" title="The statement's balances don't add up" body={check.message} />
        )}
      </div>
    );
  }

  if (source === "pdf-vision") {
    return (
      <div className="mt-3">
        <Banner
          tone="warning"
          body="This PDF is a scan with no text layer, so it was read with the vision model. Check the rows against the PDF before applying."
        />
      </div>
    );
  }

  return null;
}

function PreviewNote({ target, rows, compact }) {
  if (!target) {
    return (
      <span className="text-xs text-amber-700">
        No balance cell found for that account label — rename the label above to match a row
        in your sheet.
      </span>
    );
  }

  const amountCount = rows.length;
  const base = target.formula ?? target.raw_value ?? "";
  let preview;

  if (compact) {
    preview = "= <folded total>";
  } else if (!base) {
    preview = "= <sum of the amounts below>";
  } else {
    const terms = rows
      .map((row) => {
        const n = Number.parseFloat(row.amount);
        if (!Number.isFinite(n) || n === 0) return "";
        return (n >= 0 ? "+" : "-") + Math.abs(n).toFixed(2);
      })
      .join("");
    const full = `${base}${terms}`;
    preview = full.length > 70 ? `${full.slice(0, 50)}…${full.slice(-16)}` : full;
  }

  return (
    <span className="max-w-xs truncate text-xs text-slate-500" title={preview}>
      {amountCount > 0 ? (
        <>
          Will write <code className="text-slate-700">{preview}</code>
        </>
      ) : (
        "Add at least one row"
      )}
    </span>
  );
}

function FileField({ label, hint, accept, file, onChange, preview }) {
  const inputRef = useRef(null);
  const [url, setUrl] = useState(null);

  const isPdf =
    Boolean(file) &&
    (file.type === "application/pdf" || /\.pdf$/i.test(file.name || ""));

  useEffect(() => {
    if (!preview || !file || !file.type?.startsWith("image/")) {
      setUrl(null);
      return;
    }
    const objectUrl = URL.createObjectURL(file);
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [file, preview]);

  const handleDrop = useCallback(
    (e) => {
      e.preventDefault();
      const dropped = e.dataTransfer.files?.[0];
      if (dropped) onChange(dropped);
    },
    [onChange]
  );

  return (
    <div
      onDragOver={(e) => e.preventDefault()}
      onDrop={handleDrop}
      className="flex h-full flex-col rounded-lg border border-dashed border-slate-300 bg-slate-50/60 p-3"
    >
      <div className="text-sm font-medium text-slate-700">{label}</div>
      <div className="mt-0.5 text-xs text-slate-500">{hint}</div>

      {url && (
        <img
          src={url}
          alt="Screenshot preview"
          className="mt-3 max-h-40 w-full rounded border border-slate-200 object-contain"
        />
      )}

      {isPdf && (
        <div className="mt-3 rounded border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600">
          PDF — will be read from its text, so the amounts come through exactly.
        </div>
      )}

      <div className="mt-auto pt-3">
        <input
          ref={inputRef}
          type="file"
          accept={accept}
          className="hidden"
          onChange={(e) => e.target.files?.[0] && onChange(e.target.files[0])}
        />
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:border-slate-400"
          >
            Choose file
          </button>
          <span className="truncate text-xs text-slate-600">
            {file ? file.name : "or drop it here"}
          </span>
        </div>
      </div>
    </div>
  );
}