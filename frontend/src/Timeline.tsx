import { Spinner } from "./components";
import type { Step } from "./steps";

const ICON: Record<string, string> = { done: "✓", blocked: "!", error: "×", skipped: "–", pending: "" };

export function Timeline({ steps }: { steps: Step[] }) {
  return (
    <div className="timeline">
      {steps.map((s, i) => (
        <div key={s.key} className={`step ${s.state}`}>
          <div className="dot">{s.state === "running" ? <Spinner /> : ICON[s.state] || i + 1}</div>
          <div>
            <h4>{s.title}</h4>
            {s.lines.length > 0 && <ul>{s.lines.map(([k, v], j) => <li key={j}><b>{k}:</b> {v}</li>)}</ul>}
            {s.note && <div className="faint small" style={{ marginTop: 4 }}>{s.note}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}
