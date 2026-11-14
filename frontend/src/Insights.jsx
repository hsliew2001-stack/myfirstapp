import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import BalanceChart from "./BalanceChart.jsx";
import { Amount, Banner, Card, Empty } from "./components.jsx";

const CADENCE_LABEL = {
  weekly: "Weekly",
  fortnightly: "Fortnightly",
  monthly: "Monthly",
  quarterly: "Quarterly",
  annual: "Yearly",
};

const VERDICT = {
  yes: { icon: "✓", label: "Yes", tone: "text-emerald-800 bg-emerald-50 border-emerald-200" },
  tight: { icon: "!", label: "Tight", tone: "text-amber-900 bg-amber-50 border-amber-200" },
  no: { icon: "×", label: "No", tone: "text-red-900 bg-red-50 border-red-200" },
};

export default function Insights({ config }) {
  const [balance, setBalance] = useState("");
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setBusy("insights");
    setError(null);
    try {
      setData(await api.insights({ balance }));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  }, [balance]);

  useEffect(() => {
    load();
    // Only on mount: re-reading on every keystroke of the balance would fire a request
    // per character. The form below submits explicitly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="space-y-6">
      {error && <Banner tone="error" title="That didn't work" body={error} />}

      {config && config.history_enabled === false && (
        <Banner
          tone="warning"
          title="History storage is off in this environment"
          body="No database is configured, so imported transactions are not kept and these views stay empty. On the deployed app they persist."
        />
      )}

      <Card>
        <SectionHeading
          title="Starting balance"
          hint="What is in the account today. The forecast runs from here."
        />
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <label className="block">
            <span className="text-xs font-medium text-slate-600">Balance (SGD)</span>
            <input
              value={balance}
              onChange={(e) => setBalance(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && load()}
              inputMode="decimal"
              placeholder="4200.00"
              className="mt-1 w-40 rounded-md border border-slate-300 px-3 py-2 text-sm tabular-nums shadow-sm focus:border-slate-900 focus:outline-none focus:ring-1 focus:ring-slate-900"
            />
          </label>
          <button
            type="button"
            onClick={load}
            disabled={busy !== null}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-slate-800 disabled:bg-slate-300"
          >
            {busy === "insights" ? "Working…" : "Project my balance"}
          </button>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          Leave it blank to see the expected <em>change</em> instead of an absolute balance.
        </p>
      </Card>

      {data && !data.ready && (
        <Card>
          <Empty title="Nothing to project yet" body={data.reason} />
        </Card>
      )}

      {data?.ready && (
        <>
          <RecurringCard data={data} />
          <ForecastCard data={data} />
          <AffordabilityCard balance={balance} horizonDays={data.forecast?.horizon_days} />
          <LedgerCard onChanged={load} />
        </>
      )}

      {/* Always available: with no history this is the only thing on the page that can
          fill it, so hiding it behind `ready` would be a dead end. */}
      <ImportPanel onImported={load} />
    </div>
  );
}

function SectionHeading({ title, hint }) {
  return (
    <div>
      <h2 className="text-base font-semibold text-slate-900">{title}</h2>
      {hint && <p className="mt-0.5 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

function RecurringCard({ data }) {
  const bills = data.recurring.filter((item) => item.direction === "debit");
  const income = data.recurring.filter((item) => item.direction === "credit");
  const missed = data.recurring.filter((item) => item.missed);

  return (
    <Card>
      <SectionHeading
        title="Recurring patterns"
        hint={`${data.coverage.transactions} transactions from ${data.coverage.first} to ${data.coverage.last} (${data.coverage.days} days).`}
      />

      {data.warnings.map((warning, index) => (
        <div key={index} className="mt-3">
          <Banner tone="warning" body={warning} />
        </div>
      ))}

      {data.recurring.length === 0 ? (
        <p className="mt-4 text-sm text-slate-600">
          No repeating pattern yet. A bill needs at least two charges roughly a schedule
          apart — import more history and this fills in.
        </p>
      ) : (
        <div className="mt-4 space-y-5">
          {missed.length > 0 && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
              <div className="font-medium">
                Expected but not seen ({missed.length})
              </div>
              <p className="mt-1 opacity-90">
                These usually charge by now. The forecast still counts them as coming.
              </p>
            </div>
          )}
          {bills.length > 0 && <SeriesTable title="Money out" items={bills} />}
          {income.length > 0 && <SeriesTable title="Money in" items={income} />}
        </div>
      )}

      <div className="mt-5 grid gap-3 border-t border-slate-200 pt-4 sm:grid-cols-3">
        <Stat label="Monthly income" value={data.summary.monthly_income} tone="positive" />
        <Stat label="Monthly spending" value={data.summary.monthly_spend} tone="negative" />
        <Stat label="Monthly net" value={data.summary.monthly_net} tone="auto" />
      </div>
    </Card>
  );
}

function Stat({ label, value, tone }) {
  const n = Number.parseFloat(value);
  const signed = tone === "auto" ? n : Math.abs(n);
  return (
    <div className="rounded-lg bg-slate-50 px-3 py-2">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-0.5 text-lg font-semibold text-slate-900">
        {tone === "auto" ? <Amount value={signed} /> : `S$${Math.abs(signed).toLocaleString("en-SG", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
      </div>
    </div>
  );
}

function SeriesTable({ title, items }) {
  return (
    <div>
      <div className="text-sm font-medium text-slate-700">{title}</div>
      <div className="mt-2 overflow-hidden rounded-lg border border-slate-200">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-3 py-2 font-medium">What</th>
              <th className="px-3 py-2 font-medium">How often</th>
              <th className="px-3 py-2 text-right font-medium">Typical</th>
              <th className="px-3 py-2 font-medium">Next</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {items.map((item) => (
              <tr key={item.key} className="align-top">
                <td className="px-3 py-2">
                  <div className="text-slate-900">{item.title}</div>
                  <div className="mt-0.5 text-xs text-slate-500">
                    {item.occurrences}× since {item.last_seen}
                    {item.confidence === "low" && " · only seen twice"}
                  </div>
                </td>
                <td className="px-3 py-2 text-slate-700">
                  {CADENCE_LABEL[item.cadence] ?? item.cadence}
                </td>
                <td className="px-3 py-2 text-right">
                  <Amount value={item.amount} />
                </td>
                <td className="px-3 py-2">
                  <DueBadge item={item} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function DueBadge({ item }) {
  // Status is never colour alone: each carries a word.
  const map = {
    overdue: "border-amber-200 bg-amber-50 text-amber-900",
    due: "border-slate-300 bg-slate-100 text-slate-800",
    upcoming: "border-slate-200 bg-white text-slate-600",
  };
  const text =
    item.days_until < 0
      ? `${Math.abs(item.days_until)}d late`
      : item.days_until === 0
      ? "Today"
      : `in ${item.days_until}d`;
  return (
    <div>
      <span className={`inline-block rounded border px-1.5 py-0.5 text-xs ${map[item.status]}`}>
        {text}
      </span>
      <div className="mt-1 text-xs text-slate-500">{item.next_expected}</div>
    </div>
  );
}

function ForecastCard({ data }) {
  const forecast = data.forecast;
  if (!forecast) return null;
  const low = Number(forecast.low.balance);
  const negative = low < 0;
  const noBalance = !forecast.start_balance;

  return (
    <Card>
      <SectionHeading
        title="Forward projection"
        hint={
          noBalance
            ? "No starting balance given, so this is the expected change, not your balance."
            : `${forecast.horizon_days} days from ${forecast.start}.`
        }
      />

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <div className={`rounded-lg border px-4 py-3 ${negative ? "border-red-200 bg-red-50" : "border-slate-200 bg-slate-50"}`}>
          <div className="text-xs text-slate-600">
            {negative ? "Shortfall" : noBalance ? "Lowest point (change)" : "Lowest balance"}
          </div>
          <div className="mt-0.5 text-2xl font-semibold text-slate-900">
            S${Math.abs(low).toLocaleString("en-SG", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
          <div className="text-xs text-slate-500">on {forecast.low.date}</div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3">
          <div className="text-xs text-slate-600">At the end</div>
          <div className="mt-0.5 text-2xl font-semibold text-slate-900">
            <Amount value={forecast.end_balance} />
          </div>
          <div className="text-xs text-slate-500">on {forecast.end}</div>
        </div>
      </div>

      <div className="mt-5">
        <BalanceChart forecast={forecast} />
      </div>

      <div className="mt-4 rounded-lg bg-slate-50 px-4 py-3">
        <div className="flex items-center gap-2 text-sm">
          <span className="text-slate-600">Confidence:</span>
          <span className="font-medium text-slate-900">{forecast.confidence}</span>
        </div>
        <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-slate-600">
          {forecast.confidence_reasons.map((reason, index) => (
            <li key={index}>{reason}</li>
          ))}
        </ul>
        <div className="mt-2 text-xs font-medium text-slate-600">What this assumes</div>
        <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-slate-600">
          {forecast.assumptions.map((assumption, index) => (
            <li key={index}>{assumption}</li>
          ))}
        </ul>
      </div>
    </Card>
  );
}

function AffordabilityCard({ balance, horizonDays = 45 }) {
  const today = new Date().toISOString().slice(0, 10);
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState(today);
  const [cushion, setCushion] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const ask = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(
        await api.affordability({ amount, date, balance, cushion, horizonDays })
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const verdict = result ? VERDICT[result.verdict] : null;

  // No balance means no verdict: the projection would start at zero, so every answer
  // would be "below zero". Ask for the balance instead of answering badly.
  const needsBalance = !balance || !String(balance).trim();

  return (
    <Card>
      <SectionHeading
        title="Can I afford this?"
        hint="Checked against the projection, not just today's balance."
      />
      {needsBalance && (
        <div className="mt-3">
          <Banner
            tone="warning"
            body="Enter your current balance above first — whether you can afford something depends on what is already in the account."
          />
        </div>
      )}
      <div className="mt-4 flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="text-xs font-medium text-slate-600">Amount</span>
          <input
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && amount && ask()}
            inputMode="decimal"
            placeholder="900.00"
            className="mt-1 w-32 rounded-md border border-slate-300 px-3 py-2 text-sm tabular-nums shadow-sm focus:border-slate-900 focus:outline-none focus:ring-1 focus:ring-slate-900"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-slate-600">On</span>
          <input
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            className="mt-1 rounded-md border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-900 focus:outline-none focus:ring-1 focus:ring-slate-900"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-slate-600">Keep spare (optional)</span>
          <input
            value={cushion}
            onChange={(e) => setCushion(e.target.value)}
            inputMode="decimal"
            placeholder="0"
            className="mt-1 w-32 rounded-md border border-slate-300 px-3 py-2 text-sm tabular-nums shadow-sm focus:border-slate-900 focus:outline-none focus:ring-1 focus:ring-slate-900"
          />
        </label>
        <button
          type="button"
          onClick={ask}
          disabled={!amount || busy || needsBalance}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {busy ? "Checking…" : "Check"}
        </button>
      </div>

      {error && (
        <div className="mt-3">
          <Banner tone="error" body={error} />
        </div>
      )}

      {result && verdict && (
        <div className={`mt-4 rounded-lg border px-4 py-3 ${verdict.tone}`}>
          <div className="flex items-center gap-2 text-base font-semibold">
            <span aria-hidden="true" className="flex size-5 items-center justify-center rounded-full bg-white/70 text-sm">
              {verdict.icon}
            </span>
            {verdict.label}
          </div>
          <p className="mt-1 text-sm opacity-90">{result.reason}</p>
        </div>
      )}
    </Card>
  );
}

function ImportPanel({ onImported }) {
  const inputRef = useRef(null);
  const [file, setFile] = useState(null);
  const [text, setText] = useState("");
  const [account, setAccount] = useState("UOB One");
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const run = async (commit) => {
    setBusy(commit ? "commit" : "preview");
    setError(null);
    try {
      const result = await api.importCsv({ file, text, account, commit });
      setPreview(result);
      if (commit) {
        setFile(null);
        setText("");
        if (inputRef.current) inputRef.current.value = "";
        onImported?.();
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Card>
      <SectionHeading
        title="Import past transactions"
        hint="A CSV export or a statement PDF from your bank. Nothing is stored until you confirm."
      />
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="text-xs font-medium text-slate-600">CSV or PDF file</span>
          <input
            ref={inputRef}
            type="file"
            accept=".csv,text/csv,.pdf,application/pdf"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="mt-1 block w-full text-sm text-slate-600 file:mr-3 file:rounded-md file:border-0 file:bg-slate-900 file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-white"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-slate-600">Account label</span>
          <input
            value={account}
            onChange={(e) => setAccount(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-900 focus:outline-none focus:ring-1 focus:ring-slate-900"
          />
        </label>
      </div>

      <details className="mt-3">
        <summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-700">
          Or paste the CSV contents
        </summary>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={5}
          placeholder={"Date,Description,Amount\n21/10/2026,NETFLIX.COM,-17.98"}
          className="mt-2 w-full rounded-md border border-slate-300 px-3 py-2 font-mono text-xs shadow-sm focus:border-slate-900 focus:outline-none focus:ring-1 focus:ring-slate-900"
        />
      </details>

      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          onClick={() => run(false)}
          disabled={(!file && !text.trim()) || busy !== null}
          className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 shadow-sm hover:border-slate-400 disabled:opacity-40"
        >
          {busy === "preview" ? "Reading…" : "Preview"}
        </button>
        <button
          type="button"
          onClick={() => run(true)}
          disabled={(!file && !text.trim()) || busy !== null}
          className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-800 disabled:bg-slate-300"
        >
          {busy === "commit" ? "Importing…" : "Import"}
        </button>
      </div>

      {error && (
        <div className="mt-3">
          <Banner tone="error" body={error} />
        </div>
      )}

      {preview && (
        <div className="mt-4 space-y-3">
          <Banner
            tone={preview.committed ? "info" : "info"}
            title={preview.committed ? `Imported ${preview.added} transactions` : "Preview — nothing stored yet"}
            body={`${preview.parsed} rows understood · ${preview.added} new · ${preview.skipped} already stored.`}
          />
          {preview.errors.length > 0 && (
            <Banner
              tone="warning"
              title={`${preview.errors.length} row(s) could not be read`}
              body={
                <ul className="list-disc space-y-0.5 pl-4">
                  {preview.errors.slice(0, 6).map((line, index) => (
                    <li key={index}>{line}</li>
                  ))}
                  {preview.errors.length > 6 && <li>…and {preview.errors.length - 6} more.</li>}
                </ul>
              }
            />
          )}
          {preview.rows_preview.length > 0 && (
            <div className="max-h-48 overflow-auto rounded-md border border-slate-200">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-slate-50 text-left text-slate-500">
                  <tr>
                    <th className="px-3 py-1.5 font-medium">Date</th>
                    <th className="px-3 py-1.5 font-medium">Description</th>
                    <th className="px-3 py-1.5 text-right font-medium">Amount</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {preview.rows_preview.map((row, index) => (
                    <tr key={index}>
                      <td className="whitespace-nowrap px-3 py-1 text-slate-600">{row.date}</td>
                      <td className="px-3 py-1 text-slate-800">{row.description}</td>
                      <td className="px-3 py-1 text-right tabular-nums">
                        <Amount value={row.amount} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function LedgerCard({ onChanged }) {
  const [rows, setRows] = useState([]);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const data = await api.ledger(100);
      setRows(data.rows);
    } catch {
      setRows([]);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const clear = async () => {
    setBusy(true);
    try {
      await api.clearLedger();
      setConfirming(false);
      await load();
      onChanged?.();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <SectionHeading
        title="Stored transactions"
        hint="What the recurring list and forecast are computed from. Your screenshots and workbooks are never stored."
      />

      <div className="mt-3">
        {busy && rows.length === 0 ? (
          <p className="text-sm text-slate-500">Loading…</p>
        ) : rows.length === 0 ? (
          <p className="text-sm text-slate-500">Nothing stored yet.</p>
        ) : (
          <div className="max-h-64 overflow-auto rounded-lg border border-slate-200">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-3 py-2 font-medium">Date</th>
                  <th className="px-3 py-2 font-medium">Description</th>
                  <th className="px-3 py-2 font-medium">Account</th>
                  <th className="px-3 py-2 text-right font-medium">Amount</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {rows.map((row, index) => (
                  <tr key={`${row.date}-${index}`}>
                    <td className="whitespace-nowrap px-3 py-1.5 text-slate-600">{row.date}</td>
                    <td className="px-3 py-1.5 text-slate-800">{row.description}</td>
                    <td className="px-3 py-1.5 text-xs text-slate-500">{row.account}</td>
                    <td className="px-3 py-1.5 text-right">
                      <Amount value={row.amount} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="mt-4 flex items-center gap-3 border-t border-slate-200 pt-3">
        {confirming ? (
          <>
            <span className="text-sm text-slate-700">
              Delete all {rows.length} stored transactions? This cannot be undone.
            </span>
            <button
              type="button"
              onClick={clear}
              disabled={busy}
              className="rounded-md border border-red-300 bg-red-50 px-3 py-1.5 text-xs font-medium text-red-800 hover:bg-red-100"
            >
              Yes, delete
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="text-xs text-slate-600 hover:text-slate-900"
            >
              Cancel
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            disabled={rows.length === 0}
            className="text-xs font-medium text-slate-500 hover:text-red-700 disabled:opacity-40"
          >
            Delete my stored transactions
          </button>
        )}
      </div>
    </Card>
  );
}