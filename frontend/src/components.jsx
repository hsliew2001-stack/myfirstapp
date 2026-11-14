import { useState } from "react";

export function Card({ children }) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      {children}
    </section>
  );
}

export function StepNumber({ n, title }) {
  return (
    <div className="flex items-center gap-3">
      <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-slate-900 text-xs font-semibold text-white">
        {n}
      </span>
      <h2 className="text-base font-semibold text-slate-900">{title}</h2>
    </div>
  );
}

const TONES = {
  error: "border-red-200 bg-red-50 text-red-900",
  warning: "border-amber-200 bg-amber-50 text-amber-900",
  info: "border-slate-200 bg-slate-50 text-slate-700",
};

export function Banner({ tone = "info", title, body }) {
  return (
    <div className={`rounded-lg border px-4 py-3 text-sm ${TONES[tone]}`}>
      {title && <div className="font-medium">{title}</div>}
      <div className={title ? "mt-1 opacity-90" : ""}>{body}</div>
    </div>
  );
}

export function Amount({ value, className = "" }) {
  const n = typeof value === "number" ? value : Number.parseFloat(value);
  if (!Number.isFinite(n)) return <span className={className}>{String(value)}</span>;
  const formatted = n.toLocaleString("en-SG", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return (
    <span
      className={`tabular-nums ${
        n < 0 ? "text-red-700" : n > 0 ? "text-emerald-700" : "text-slate-600"
      } ${className}`}
    >
      {n > 0 ? "+" : ""}
      {formatted}
    </span>
  );
}

export function Empty({
  title = "Nothing loaded yet",
  body = "Add a screenshot and your workbook, then read the transactions. You will see every row and the exact formula change before anything is written.",
}) {
  return (
    <div className="py-8 text-center">
      <p className="text-sm font-medium text-slate-700">{title}</p>
      <p className="mx-auto mt-1 max-w-md text-sm text-slate-500">{body}</p>
    </div>
  );
}

export function CellPicker({ candidates, selected, onSelect }) {
  const chosen = (c) => selected?.sheet === c.sheet && selected?.cell === c.cell;

  return (
    <div>
      <div className="text-sm font-medium text-slate-700">Where to append</div>
      <p className="mt-0.5 text-xs text-slate-500">
        The balance cell found in that row. Confirm it holds the account balance before
        applying.
      </p>
      <div className="mt-2 space-y-2">
        {candidates.map((c) => (
          <label
            key={`${c.sheet}!${c.cell}`}
            className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition ${
              chosen(c)
                ? "border-slate-900 bg-slate-50 ring-1 ring-slate-900"
                : "border-slate-200 hover:border-slate-300"
            }`}
          >
            <input
              type="radio"
              name="target-cell"
              checked={chosen(c)}
              onChange={() => onSelect(c)}
              className="mt-1"
            />
            <div className="min-w-0">
              <div className="text-sm text-slate-900">
                <span className="font-medium">{c.label}</span>
                <span className="ml-2 font-mono text-xs text-slate-500">
                  {c.sheet}!{c.cell}
                </span>
              </div>
              <div className="mt-1 truncate font-mono text-xs text-slate-600">
                {c.formula ?? c.raw_value ?? "(empty cell)"}
              </div>
            </div>
          </label>
        ))}
      </div>
    </div>
  );
}

export function RowEditor({ rows, onChange }) {
  const [draft, setDraft] = useState({ date: "", description: "", amount: "" });

  const update = (index, patch) =>
    onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));

  const remove = (index) => onChange(rows.filter((_, i) => i !== index));

  const add = () => {
    const amount = draft.amount.trim();
    if (!amount) return;
    onChange([
      ...rows,
      {
        date: draft.date.trim() || null,
        description: draft.description.trim(),
        amount,
        currency: null,
      },
    ]);
    setDraft({ date: "", description: "", amount: "" });
  };

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <div className="text-sm font-medium text-slate-700">Transactions</div>
        <div className="text-xs text-slate-500">
          Debits are negative, credits positive — edit anything the screenshot misread.
        </div>
      </div>

      <div className="mt-2 overflow-hidden rounded-lg border border-slate-200">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="w-28 px-3 py-2 font-medium">Date</th>
              <th className="px-3 py-2 font-medium">Description</th>
              <th className="w-28 px-3 py-2 text-right font-medium">Amount</th>
              <th className="w-10 px-2 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {rows.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-6 text-center text-slate-500">
                  No transactions found. Add rows below, or try a clearer screenshot.
                </td>
              </tr>
            )}
            {rows.map((row, index) => (
              <tr key={index} className="align-top">
                <td className="px-3 py-2">
                  <input
                    value={row.date ?? ""}
                    onChange={(e) => update(index, { date: e.target.value || null })}
                    placeholder="2026-11-12"
                    className="w-full rounded border border-transparent px-1 py-0.5 text-xs text-slate-700 hover:border-slate-200 focus:border-slate-400 focus:outline-none"
                  />
                </td>
                <td className="px-3 py-2">
                  <input
                    value={row.description}
                    onChange={(e) => update(index, { description: e.target.value })}
                    className="w-full rounded border border-transparent px-1 py-0.5 text-slate-800 hover:border-slate-200 focus:border-slate-400 focus:outline-none"
                  />
                </td>
                <td className="px-3 py-2 text-right">
                  <input
                    value={row.amount}
                    onChange={(e) => update(index, { amount: e.target.value })}
                    className="w-24 rounded border border-transparent px-1 py-0.5 text-right tabular-nums hover:border-slate-200 focus:border-slate-400 focus:outline-none"
                  />
                </td>
                <td className="px-2 py-2 text-right">
                  <button
                    type="button"
                    onClick={() => remove(index)}
                    aria-label={`Remove ${row.description || "row"}`}
                    className="rounded px-1.5 text-slate-400 hover:bg-red-50 hover:text-red-600"
                  >
                    ×
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg bg-slate-50 p-2">
        <input
          value={draft.date}
          onChange={(e) => setDraft({ ...draft, date: e.target.value })}
          placeholder="Date"
          className="w-32 rounded border border-slate-300 px-2 py-1 text-xs"
        />
        <input
          value={draft.description}
          onChange={(e) => setDraft({ ...draft, description: e.target.value })}
          placeholder="Description"
          className="min-w-40 flex-1 rounded border border-slate-300 px-2 py-1 text-xs"
        />
        <input
          value={draft.amount}
          onChange={(e) => setDraft({ ...draft, amount: e.target.value })}
          onKeyDown={(e) => e.key === "Enter" && add()}
          placeholder="Amount"
          inputMode="decimal"
          className="w-24 rounded border border-slate-300 px-2 py-1 text-right text-xs tabular-nums"
        />
        <button
          type="button"
          onClick={add}
          disabled={!draft.amount.trim()}
          className="rounded border border-slate-300 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 hover:border-slate-400 disabled:opacity-40"
        >
          Add row
        </button>
      </div>
    </div>
  );
}