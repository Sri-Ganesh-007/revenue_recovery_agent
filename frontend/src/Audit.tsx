import { Fragment, useState } from "react";
import { api } from "./api";
import { Badge, Empty, ErrorBox, Panel, PolicyBadge, Spinner } from "./components";
import { dateTime, label } from "./format";
import { useLoad } from "./hooks";
import type { AuditRecord } from "./types";

export function Audit({ refreshKey, focusPayment, onOpenPayment }: { refreshKey: number; focusPayment: string | null; onOpenPayment: (id: string) => void }) {
  const [filter, setFilter] = useState(focusPayment ?? "");
  const [open, setOpen] = useState<string | null>(null);
  const { data, error, loading, reload } = useLoad(() => api.audit({ payment_id: filter.trim() || undefined, limit: 100 }), [filter, refreshKey]);
  return (
    <div className="grid">
      <Panel>
        <div className="row">
          <input placeholder="Filter by payment id (exact)…" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ minWidth: 280 }} />
          <button className="btn small" onClick={() => setFilter("")}>Clear</button>
          <span className="muted small" style={{ marginLeft: "auto" }}>{loading ? <Spinner /> : `${data?.length ?? 0} decisions`}</span>
        </div>
      </Panel>
      {error ? <ErrorBox error={error} onRetry={reload} /> : (
        <Panel>
          {data && data.length === 0 && <Empty>No agent decisions recorded yet.</Empty>}
          <div className="table-wrap">
            <table>
              <thead><tr><th>When</th><th>Payment</th><th>Proposed action</th><th>Policy</th><th>Simulated outcome</th></tr></thead>
              <tbody>
                {(data ?? []).map((d) => (
                  <Fragment key={d.decision_id}>
                    <tr className="click" onClick={() => setOpen(open === d.decision_id ? null : d.decision_id)}>
                      <td className="muted small">{dateTime(d.created_at)}</td>
                      <td><span className="mono">{d.payment_id}</span><div className="faint small">{d.customer_name}</div></td>
                      <td><b>{label(d.proposed_action)}</b>{d.parameters && Object.keys(d.parameters).length > 0 && <div className="faint small mono">{JSON.stringify(d.parameters)}</div>}</td>
                      <td><PolicyBadge status={d.policy_status} /></td>
                      <td className="muted small">{d.simulated_action ? d.simulated_action.outcome : <Badge tone="amber">nothing executed</Badge>}</td>
                    </tr>
                    {open === d.decision_id && <tr><td colSpan={5}><Why d={d} onOpenPayment={onOpenPayment} /></td></tr>}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
    </div>
  );
}

/** Why was this action taken or blocked? Everything shown is the persisted audit record. */
function Why({ d, onOpenPayment }: { d: AuditRecord; onOpenPayment: (id: string) => void }) {
  return (
    <div className="panel" style={{ margin: "6px 0 12px", background: "var(--bg-2)" }}>
      <dl className="kv">
        <dt>Decision id</dt><dd className="mono">{d.decision_id}</dd>
        <dt>Original failure reason</dt><dd>{label(d.failure_reason)}</dd>
        <dt>LLM explanation</dt><dd>{d.explanation ?? "-"} <span className="faint">(narrative only)</span></dd>
        <dt>Policy verdict</dt><dd><PolicyBadge status={d.policy_status} /> <span className="muted">{d.policy_reason}</span></dd>
        <dt>Proposals</dt>
        <dd>
          {d.attempts.length === 0 ? "-" : d.attempts.map((a, i) => (
            <div key={i} style={{ marginBottom: 6 }}>
              <b>{i + 1}. {a.name ? label(a.name) : "unusable model output"}</b> {a.arguments && Object.keys(a.arguments).length > 0 && <span className="mono faint">{JSON.stringify(a.arguments)}</span>}
              <div className="small">{a.policy ? <>{a.policy.allowed ? <Badge tone="teal">allowed</Badge> : <Badge tone="red">blocked</Badge>} <span className="muted">rule <code>{a.policy.rule}</code>, category {label(a.policy.category)}</span></> : <span className="muted">{a.error}</span>}</div>
              <div className="small muted">{a.status === "executed_simulated" ? "→ executed as a simulated action" : "→ not executed"}{a.error && a.policy ? `: ${a.error}` : ""}</div>
            </div>
          ))}
        </dd>
        <dt>Recorded outcome</dt>
        <dd>{d.simulated_action ? <>{label(d.simulated_action.strategy)} · {label(d.simulated_action.status)} · {d.simulated_action.outcome} · {dateTime(d.simulated_action.attempted_at)}</> : "None: this decision was blocked, so nothing was executed."}</dd>
      </dl>
      <button className="btn small" style={{ marginTop: 10 }} onClick={() => onOpenPayment(d.payment_id)}>Open payment</button>
    </div>
  );
}
