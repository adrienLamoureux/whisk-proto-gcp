interface Props {
  label: string;
  used: number;
  cap: number;
  unit?: string;
}

/** Shows quota headroom *before* the user hits a 429 rather than after. */
export function Meter({ label, used, cap, unit = "" }: Props) {
  const pct = cap > 0 ? Math.min(100, (used / cap) * 100) : 0;
  const tone = pct >= 90 ? "danger" : pct >= 65 ? "warn" : "";
  return (
    <div className="wsk-meter">
      <div className="wsk-meter-head">
        <span>{label}</span>
        <span>
          {used.toLocaleString()} / {cap.toLocaleString()}
          {unit}
        </span>
      </div>
      <div
        className="wsk-meter-track"
        role="progressbar"
        aria-valuenow={used}
        aria-valuemin={0}
        aria-valuemax={cap}
        aria-label={label}
      >
        <div className={`wsk-meter-fill ${tone}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
