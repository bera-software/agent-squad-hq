import { useId } from "react";

export interface Bar {
  label: string;
  value: number;
  /** Optional reference value drawn as a dashed tick (e.g. a budget). */
  target?: number | undefined;
}

/**
 * Horizontal single-series bar chart in plain SVG.
 * Accessible: role="img" with title/desc, values labelled directly, a native
 * tooltip per bar (<title>), and a data table alternative under <details>.
 */
export function BarChart({
  title,
  bars,
  format,
  targetLabel = "Budget",
}: {
  title: string;
  bars: readonly Bar[];
  format: (n: number) => string;
  targetLabel?: string;
}) {
  const id = useId();
  const sorted = [...bars].sort((a, b) => b.value - a.value);
  if (sorted.length === 0) {
    return (
      <figure className="card chart">
        <figcaption>
          <h2>{title}</h2>
        </figcaption>
        <p className="subtle">No spend recorded.</p>
      </figure>
    );
  }
  const hasTarget = sorted.some((b) => b.target !== undefined);
  const max = Math.max(...sorted.map((b) => Math.max(b.value, b.target ?? 0)), 0.0001);
  const labelW = 120;
  const valueW = 70;
  const plotW = 400;
  const row = 28;
  const barH = 14;
  const width = labelW + plotW + valueW;
  const height = sorted.length * row + 8;
  const scale = (n: number) => (n / max) * plotW;
  const desc = sorted.map((b) => `${b.label}: ${format(b.value)}${b.target !== undefined ? ` of ${format(b.target)}` : ""}`).join("; ");

  return (
    <figure className="card chart" style={{ margin: 0 }}>
      <figcaption>
        <h2 id={`${id}-t`}>{title}</h2>
        {hasTarget ? (
          <p className="hint">
            <svg width="18" height="10" aria-hidden="true" focusable="false">
              <line x1="0" y1="5" x2="18" y2="5" className="budget" />
            </svg>{" "}
            {targetLabel}
          </p>
        ) : null}
      </figcaption>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        role="img"
        aria-labelledby={`${id}-t ${id}-d`}
        preserveAspectRatio="xMinYMin meet"
      >
        <desc id={`${id}-d`}>{desc}</desc>
        <line className="baseline" x1={labelW} x2={labelW} y1={0} y2={height} />
        {sorted.map((b, i) => {
          const y = i * row + 4;
          const w = Math.max(scale(b.value), b.value > 0 ? 2 : 0);
          return (
            <g key={b.label} className="bar-group">
              <title>{`${b.label}: ${format(b.value)}${b.target !== undefined ? ` (${targetLabel.toLowerCase()} ${format(b.target)})` : ""}`}</title>
              <text x={labelW - 8} y={y + barH / 2 + 4} textAnchor="end">
                {b.label.length > 16 ? `${b.label.slice(0, 15)}…` : b.label}
              </text>
              {/* Bar anchored square on the baseline, 4px rounded data end. */}
              <path
                className="bar"
                d={
                  w > 4
                    ? `M${labelW},${y} h${w - 4} a4,4 0 0 1 4,4 v${barH - 8} a4,4 0 0 1 -4,4 h${-(w - 4)} z`
                    : `M${labelW},${y} h${w} v${barH} h${-w} z`
                }
              />
              {b.target !== undefined ? (
                <line className="budget" x1={labelW + scale(b.target)} x2={labelW + scale(b.target)} y1={y - 3} y2={y + barH + 3} />
              ) : null}
              <text className="value" x={labelW + plotW + 6} y={y + barH / 2 + 4}>
                {format(b.value)}
              </text>
            </g>
          );
        })}
      </svg>
      <details>
        <summary>Show data table</summary>
        <table>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col" className="num">
                Spend
              </th>
              {hasTarget ? (
                <th scope="col" className="num">
                  {targetLabel}
                </th>
              ) : null}
            </tr>
          </thead>
          <tbody>
            {sorted.map((b) => (
              <tr key={b.label}>
                <th scope="row">{b.label}</th>
                <td className="num">{format(b.value)}</td>
                {hasTarget ? <td className="num">{b.target !== undefined ? format(b.target) : "–"}</td> : null}
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}
