import React from "react";
import { useId, useMemo, useState } from "react";

// Single-series balance projection. One series, so no legend — the card title names it.
// Specs follow the project's viz rules: 2px line, ~10% area wash, hairline solid grid,
// 8px end marker with a 2px surface ring, labels only at the low point and the endpoint,
// and a table view so no value is reachable only through the tooltip.

const WIDTH = 640;
const HEIGHT = 200;
const PAD = { top: 16, right: 56, bottom: 26, left: 8 };

const fmt = (n) =>
  Number(n).toLocaleString("en-SG", { minimumFractionDigits: 0, maximumFractionDigits: 0 });

function money(n) {
  const value = Number(n);
  return `${value < 0 ? "−" : ""}S$${fmt(Math.abs(value))}`;
}

function niceTicks(min, max, count = 4) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) return [min];
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step / 2; v += step) ticks.push(Math.round(v * 100) / 100);
  return ticks;
}

export default function BalanceChart({ forecast, currency = "SGD" }) {
  const gradientId = useId();
  const [hover, setHover] = useState(null);

  const points = forecast?.points ?? [];
  const geometry = useMemo(() => {
    if (points.length < 2) return null;
    const values = points.map((p) => Number(p.balance));
    const low = Math.min(...values);
    const high = Math.max(...values);
    // Anchor the axis at zero only when it is close by; otherwise the shape flattens.
    const floor = low < 0 ? low : Math.min(0, low);
    const ceil = high;
    const span = ceil - floor || 1;
    const plotW = WIDTH - PAD.left - PAD.right;
    const plotH = HEIGHT - PAD.top - PAD.bottom;
    const x = (i) => PAD.left + (i / (points.length - 1)) * plotW;
    const y = (v) => PAD.top + plotH - ((v - floor) / span) * plotH;

    const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(Number(p.balance))}`).join(" ");
    const area = `${line} L${x(points.length - 1)},${PAD.top + plotH} L${x(0)},${PAD.top + plotH} Z`;

    const lowIndex = values.indexOf(low);
    const ticks = niceTicks(floor, ceil, 4);

    return { x, y, line, area, plotH, plotW, floor, lowIndex, ticks, values };
  }, [points]);

  if (!geometry) {
    return <p className="text-sm text-slate-500">Not enough forecast points to draw a line.</p>;
  }

  const { x, y, line, area, plotW, floor, lowIndex, ticks, values } = geometry;
  const lastIndex = points.length - 1;
  const axisY = PAD.top + (HEIGHT - PAD.top - PAD.bottom);
  const showZero = floor < 0;

  const active = hover ?? null;
  const activePoint = active !== null ? points[active] : null;

  // Month gridlines: one per calendar month change, so the reader can see where rent or
  // salary lands against the calendar rather than decoding day offsets.
  const monthTicks = points
    .map((p, i) => ({ i, month: p.date.slice(0, 7) }))
    .filter((entry, index, all) => index === 0 || entry.month !== all[index - 1].month);

  const labelFor = (index, text, anchor = "start") => {
    const value = Number(points[index].balance);
    const px = x(index);
    const py = y(value);
    // Keep the label inside the plot: at the right edge it flips to end-anchored.
    const flip = anchor === "start" && px > WIDTH - PAD.right - 44;
    return (
      <text
        key={`label-${index}`}
        x={flip ? px - 8 : px + (anchor === "end" ? -8 : 8)}
        y={Math.max(PAD.top + 8, Math.min(py - 8, axisY - 4))}
        textAnchor={flip ? "end" : anchor}
        className="fill-slate-600 text-[11px] tabular-nums"
      >
        {text}
      </text>
    );
  };

  const onKeyDown = (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const next = active === null ? lastIndex : active + (event.key === "ArrowRight" ? 1 : -1);
    setHover(Math.max(0, Math.min(lastIndex, next)));
  };

  return (
    <figure className="m-0">
      <div className="relative">
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          role="img"
          aria-label={`Projected balance from ${points[0].date} to ${points[lastIndex].date}, lowest ${money(
            values[lowIndex]
          )} on ${points[lowIndex].date}.`}
          tabIndex={0}
          onKeyDown={onKeyDown}
          onFocus={() => setHover(active ?? lowIndex)}
          onBlur={() => setHover(null)}
          className="w-full rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-900"
        >
          <defs>
            <linearGradient id={gradientId} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor="#2a78d6" stopOpacity="0.14" />
              <stop offset="100%" stopColor="#2a78d6" stopOpacity="0.02" />
            </linearGradient>
          </defs>

          {ticks.map((tick) => (
            <g key={tick}>
              <line
                x1={PAD.left}
                x2={WIDTH - PAD.right}
                y1={y(tick)}
                y2={y(tick)}
                stroke="#e1e0d9"
                strokeWidth="1"
              />
              <text
                x={WIDTH - PAD.right + 6}
                y={y(tick) + 3}
                className="fill-slate-500 text-[10px] tabular-nums"
              >
                {fmt(tick)}
              </text>
            </g>
          ))}

          {showZero && (
            <line
              x1={PAD.left}
              x2={WIDTH - PAD.right}
              y1={y(0)}
              y2={y(0)}
              stroke="#c3c2b7"
              strokeWidth="1"
            />
          )}

          <path d={area} fill={`url(#${gradientId})`} />
          <path d={line} fill="none" stroke="#2a78d6" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />

          {monthTicks.map(({ i, month }) => (
            <text
              key={month}
              x={x(i)}
              y={axisY + 16}
              textAnchor={i === 0 ? "start" : "middle"}
              className="fill-slate-500 text-[10px]"
            >
              {new Date(`${month}-01T00:00:00`).toLocaleDateString("en-SG", { month: "short" })}
            </text>
          ))}

          {/* Selective direct labels: the low point and the endpoint only. */}
          <circle
            cx={x(lowIndex)}
            cy={y(values[lowIndex])}
            r="4"
            fill="#2a78d6"
            stroke="#fcfcfb"
            strokeWidth="2"
          />
          {labelFor(lowIndex, money(values[lowIndex]))}

          <circle
            cx={x(lastIndex)}
            cy={y(values[lastIndex])}
            r="4"
            fill="#2a78d6"
            stroke="#fcfcfb"
            strokeWidth="2"
          />

          {active !== null && (
            <g>
              <line
                x1={x(active)}
                x2={x(active)}
                y1={PAD.top}
                y2={axisY}
                stroke="#898781"
                strokeWidth="1"
              />
              <circle
                cx={x(active)}
                cy={y(values[active])}
                r="4"
                fill="#2a78d6"
                stroke="#fcfcfb"
                strokeWidth="2"
              />
            </g>
          )}

          {/* Invisible hit bands: bigger targets than the 2px line. */}
          {points.map((point, i) => (
            <rect
              key={point.date}
              x={PAD.left + (i / lastIndex) * plotW - plotW / lastIndex / 2}
              y={PAD.top}
              width={plotW / lastIndex}
              height={HEIGHT - PAD.top - PAD.bottom}
              fill="transparent"
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
            />
          ))}
        </svg>

        {activePoint && (
          <div
            className="pointer-events-none absolute -translate-x-1/2 -translate-y-full rounded-md border border-slate-200 bg-white px-2 py-1 text-xs shadow-sm"
            style={{ left: `${(x(active) / WIDTH) * 100}%`, top: `${(y(values[active]) / HEIGHT) * 100}%` }}
          >
            <div className="font-medium text-slate-900 tabular-nums">{money(activePoint.balance)}</div>
            <div className="text-slate-500">{activePoint.date}</div>
          </div>
        )}
      </div>

      <figcaption className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
        <span>Every day from {points[0].date} to {points[lastIndex].date}.</span>
        <span>
          Lowest <span className="font-medium text-slate-700 tabular-nums">{money(values[lowIndex])}</span>{" "}
          on {points[lowIndex].date}
        </span>
      </figcaption>

      <details className="mt-3">
        <summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-700">
          View as a table
        </summary>
        <div className="mt-2 max-h-56 overflow-auto rounded-md border border-slate-200">
          <table className="w-full text-xs tabular-nums">
            <thead className="sticky top-0 bg-slate-50 text-left text-slate-500">
              <tr>
                <th className="px-3 py-1.5 font-medium">Date</th>
                <th className="px-3 py-1.5 text-right font-medium">Projected balance</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {points.map((point) => (
                <tr key={point.date}>
                  <td className="px-3 py-1 text-slate-600">{point.date}</td>
                  <td className="px-3 py-1 text-right text-slate-800">{money(point.balance)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}