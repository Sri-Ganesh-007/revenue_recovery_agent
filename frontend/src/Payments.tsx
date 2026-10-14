import { useEffect, useState } from "react";
import { api } from "./api";
import { Badge, Empty, ErrorBox, PaymentStatusBadge, Panel, PolicyBadge, RecoveryBadge, Spinner } from "./components";
import { dateTime, FAILURE_REASONS, label, money, RECOVERY_STATUSES } from "./format";
import { useLoad } from "./hooks";
import type { AuditRecord, RecoveryRun } from "./types";

const PAGE = 25;

export function Payments({ refreshKey, selected, onSelect, onChanged }: {
  refreshKey: number; selected: string | null; onSelect: (id: string | null) => void; onChanged: () => void;
}) {
  const [status, setStatus] = useState(""); const [recovery, setRecovery] = useState("");
  const [reason, setReason] = useState(""); const [search, setSearch] = useState(""); const [page, setPage] = useState(0);
  const [debounced, setDebounced] = useState("");
  useEffect(() => { const t = setTimeout(() => setDebounced(search), 250); return () => clearTimeout(t); }, [search]);
  useEffect(() => setPage(0), [status, recovery, reason, debounced]);

  const { data, error, loading, reload } = useLoad(
    () => api.payments({ status, recovery_status: recovery, failure_reason: reason, search: debounced, limit: PAGE, offset: page * PAGE }),
    [status, recovery, reason, debounced, page, refreshKey]);

  return (
    <div className="grid">
      <Panel>
        <div className="row">
          <input placeholder="Search payment, customer…" value={search} onChange={(e) => setSearch(e.target.value)} style={{ minWidth: 220 }} />
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Payment status">
            <option value="">All payment statuses</option><option value="success">Success</option><option value="failed">Failed</option>
          </select>
          <select value={recovery} onChange={(e) => setRecovery(e.target.value)} aria-label="Recovery status">
            <option value="">All recovery states</option>
            {RECOVERY_STATUSES.map((s) => <option key={s} value={s}>{label(s)}</option>)}
          </select>
          <select value={reason} onChange={(e) => setReason(e.target.value)} aria-label="Failure reason">
            <option value="">All failure reasons</option>
            {FAILURE_REASONS.map((s) => <option key={s} value={s}>{label(s)}</option>)}
          </select>
          <button className="btn small" onClick={() => { setStatus("failed"); setRecovery("escalated"); }}>Escalation queue</button>
          <button className="btn small" onClick={() => { setStatus(""); setRecovery(""); setReason(""); setSearch(""); }}>Clear</button>
          <span style={{ marginLeft: "auto" }} className="muted small">{loading ? <Spinner /> : `${data?.total ?? 0} payments`}</span>
        </div>
      </Panel>

      {error ? <ErrorBox error={error} onRetry={reload} /> : (
        <Panel>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Payment</th><th>Customer</th><th className="num">Amount</th><th>Failure reason</th><th>Status</th><th>Recovery</th><th>Last action</th><th>Created</th></tr></thead>
              <tbody>
                {(data?.items ?? []).map((p) => (
                  <tr key={p.id} className={`click ${selected === p.id ? "sel" : ""}`} onClick={() => onSelect(p.id)}>
                    <td className="mono">{p.id}</td>
                    <td>{p.customer_name}<div className="faint small mono">{p.customer_id}</div></td>
                    <td className="num">{money(p.amount, p.currency)}</td>
                    <td>{p.failure_reason ? label(p.failure_reason) : <span className="faint">-</span>}</td>
                    <td><PaymentStatusBadge status={p.status} /></td>
                    <td><RecoveryBadge status={p.recovery_status} /></td>
                    <td className="muted">{label(p.last_action)}</td>
                    <td className="muted small">{dateTime(p.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data && data.items.length === 0 && <Empty>No payments match these filters.</Empty>}
          </div>
          {data && data.total > PAGE && (
            <div className="row" style={{ justifyContent: "space-between", marginTop: 12 }}>
              <button className="btn small" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
              <span className="muted small">Page {page + 1} of {Math.ceil(data.total / PAGE)}</span>
              <button className="btn small" disabled={(page + 1) * PAGE >= data.total} onClick={() => setPage(page + 1)}>Next</button>
            </div>
          )}
        </Panel>
      )}

      {selected && <PaymentDrawer id={selected} refreshKey={refreshKey} onClose={() => onSelect(null)} onChanged={onChanged} />}
    </div>
  );
}

function PaymentDrawer({ id, refreshKey, onClose, onChanged }: { id: string; refreshKey: number; onClose: () => void; onChanged: () => void }) {
  const { data, error, loading, reload } = useLoad(() => api.payment(id), [id, refreshKey]);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<unknown>(null);
  const [result, setResult] = useState<{ run: RecoveryRun; replayed: boolean } | null>(null);
  useEffect(() => { setResult(null); setRunError(null); }, [id]);

  async function runRecovery() {
    setRunning(true); setRunError(null); setResult(null);
    try {
      // Same key per payment: pressing it twice demonstrates idempotency (the second time is a replay).
      setResult(await api.simulate(id, `console-${id}`));
      onChanged(); reload();
    } catch (e) { setRunError(e); } finally { setRunning(false); }
  }

  const p = data?.payment;
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label="Payment details">
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 14 }}>
          <div><h1 style={{ fontSize: 18 }}>Payment details</h1><div className="mono muted">{id}</div></div>
          <button className="btn small" onClick={onClose}>Close</button>
        </div>
        {error ? <ErrorBox error={error} onRetry={reload} /> : null}
        {loading && !data && <div className="empty"><Spinner /></div>}
        {p && data && (
          <div className="grid">
            <Panel>
              <dl className="kv">
                <dt>Customer</dt><dd>{p.customer_name} <span className="faint mono">({p.customer_id})</span></dd>
                <dt>Plan</dt><dd>{data.subscription_plan ?? "-"}</dd>
                <dt>Amount</dt><dd>{money(p.amount, p.currency)}</dd>
                <dt>Payment status</dt><dd><PaymentStatusBadge status={p.status} /></dd>
                <dt>Failure reason</dt><dd>{p.failure_reason ? label(p.failure_reason) : "-"}</dd>
                <dt>Recovery status</dt><dd><RecoveryBadge status={p.recovery_status} /></dd>
                <dt>Created</dt><dd>{dateTime(p.created_at)}</dd>
              </dl>
              {p.status === "failed" && (
                <div style={{ marginTop: 14 }}>
                  <button className="btn primary" disabled={running} onClick={runRecovery}>{running ? <><Spinner /> Running agent…</> : "Run recovery agent"}</button>
                  <div className="faint small" style={{ marginTop: 6 }}>Calls the existing /recovery/simulate endpoint. Everything is simulated.</div>
                </div>
              )}
              {runError ? <div style={{ marginTop: 12 }}><ErrorBox error={runError} /></div> : null}
              {result && (
                <div className="notice" style={{ marginTop: 12 }}>
                  <b>{result.replayed ? "Replay (no new LLM call): " : ""}{result.run.message}</b>
                </div>
              )}
            </Panel>

            <Panel title="Recovery attempts (recorded outcomes)">
              {data.recovery_attempts.length === 0 ? <Empty>No recovery attempts recorded.</Empty> : (
                <div className="grid" style={{ gap: 10 }}>
                  {data.recovery_attempts.map((a) => (
                    <div key={a.id}>
                      <div className="row"><b>{label(a.strategy)}</b><Badge>{label(a.status)}</Badge><span className="faint small">{dateTime(a.attempted_at)}</span></div>
                      <div className="muted small">{a.outcome ?? "no outcome recorded"}</div>
                    </div>
                  ))}
                </div>
              )}
            </Panel>

            <Panel title="Agent decisions">
              {data.decisions.length === 0 ? <Empty>The agent has not processed this payment.</Empty> : data.decisions.map((d) => <DecisionCard key={d.decision_id} d={d} />)}
            </Panel>

            <Panel title="Customer's recent payments">
              <div className="grid" style={{ gap: 8 }}>
                {data.customer_recent_payments.map((r) => (
                  <div key={r.id} className="row" style={{ justifyContent: "space-between" }}>
                    <span className="mono small">{r.id}</span><span>{money(r.amount, r.currency)}</span><PaymentStatusBadge status={r.status} />
                  </div>
                ))}
              </div>
            </Panel>
          </div>
        )}
      </aside>
    </>
  );
}

export function DecisionCard({ d }: { d: AuditRecord }) {
  return (
    <div style={{ borderBottom: "1px solid var(--line-soft)", padding: "10px 0" }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span><b>{label(d.proposed_action)}</b> <PolicyBadge status={d.policy_status} /></span>
        <span className="faint small">{dateTime(d.created_at)}</span>
      </div>
      {d.explanation && <div className="muted small" style={{ marginTop: 4 }}>“{d.explanation}” <span className="faint">(LLM explanation, narrative only)</span></div>}
      <div className="small" style={{ marginTop: 4 }}><b>Policy:</b> <span className="muted">{d.policy_reason}</span></div>
      {d.attempts.length > 1 && <div className="small muted" style={{ marginTop: 4 }}>The model was asked {d.attempts.length} times: {d.attempts.map((a, i) => `${i + 1}. ${a.name ?? "unusable output"} → ${a.policy ? (a.policy.allowed ? "allowed" : "blocked") : "rejected"}`).join("; ")}</div>}
    </div>
  );
}
