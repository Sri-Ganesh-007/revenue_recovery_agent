import { Fragment } from "react";
import { Bar, BarChart, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "./api";
import { Badge, ErrorBox, Kpi, Panel, PolicyBadge, Spinner } from "./components";
import { ago, label, money, pct } from "./format";
import { useLoad } from "./hooks";

const COLORS = { teal: "#3ecfb2", amber: "#f2b84b", blue: "#6ea8ff", red: "#ef6b7b", grid: "#1d2c4d", axis: "#7f90b6" };
const tooltip = { contentStyle: { background: "#0c1730", border: "1px solid #223255", borderRadius: 10, color: "#e8edf7", fontSize: 12 }, cursor: { fill: "rgba(110,168,255,.06)" } };

export function Overview({ refreshKey, onOpenPayment }: { refreshKey: number; onOpenPayment: (id: string) => void }) {
  const { data: m, error, loading, reload } = useLoad(api.metrics, [refreshKey]);
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!m) return <div className="empty"><Spinner /> Loading metrics…</div>;
  const cur = m.currency;
  const days = m.by_day.map((d) => ({ ...d, day: d.date.slice(5) }));
  const noActivity = m.decisions_total === 0;

  return (
    <div className="grid">
      <div className="grid kpis">
        <Kpi title="Failed payments" tone="red" value={String(m.failed_payments)} sub={`${m.escalations_open} escalated to a human`} />
        <Kpi title="Revenue at risk" tone="amber" value={money(m.revenue_at_risk, cur)} sub="failed payments, not yet recovered" />
        <Kpi title="Revenue recovered" tone="teal" value={money(m.revenue_recovered, cur)} sub={`${m.recovered_payments} payment(s) confirmed by the processor`} />
        <Kpi title="Recovery success rate" tone="blue" value={pct(m.recovery_success_rate)} sub={`${m.recovered_payments} of ${m.payments_processed} payments the agent handled`} />
      </div>

      <div className="grid three">
        <Kpi title="Policy blocks" value={`${m.decisions_blocked} / ${m.decisions_total}`} sub="decisions the policy refused to execute" />
        <Kpi title="Retries confirmed" value={`${m.retries_confirmed} / ${m.retries_settled}`} sub={`retry confirmation rate ${pct(m.retry_confirmation_rate)}`} />
        <Kpi title="Decisions approved" value={String(m.decisions_approved)} sub="executed as simulated actions" />
      </div>

      <div className="grid two">
        <Panel title="Agent activity, last 14 days" action={loading ? <Spinner /> : undefined}>
          {noActivity ? <div className="empty">No agent decisions yet. Run a scenario in the Gateway simulator.</div> : (
            <div style={{ height: 290 }}>
              <ResponsiveContainer>
                <ComposedChart data={days} margin={{ left: -10, right: 4, top: 6 }}>
                  <CartesianGrid stroke={COLORS.grid} vertical={false} />
                  <XAxis dataKey="day" stroke={COLORS.axis} tickLine={false} fontSize={11} />
                  <YAxis yAxisId="n" stroke={COLORS.axis} tickLine={false} axisLine={false} allowDecimals={false} fontSize={11} />
                  <YAxis yAxisId="r" orientation="right" stroke={COLORS.axis} tickLine={false} axisLine={false} fontSize={11} />
                  <Tooltip {...tooltip} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Bar yAxisId="n" dataKey="approved" name="Approved" stackId="a" fill={COLORS.teal} radius={[0, 0, 0, 0]} />
                  <Bar yAxisId="n" dataKey="blocked" name="Blocked by policy" stackId="a" fill={COLORS.amber} radius={[4, 4, 0, 0]} />
                  <Line yAxisId="r" type="monotone" dataKey="recovered_amount" name={`Recovered (${cur})`} stroke={COLORS.blue} strokeWidth={2} dot={false} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          )}
        </Panel>

        <Panel title="Failed payments by reason">
          {m.by_failure_reason.length === 0 ? <div className="empty">No failed payments.</div> : (
            <div style={{ height: 290 }}>
              <ResponsiveContainer>
                <BarChart data={m.by_failure_reason.map((r) => ({ ...r, name: label(r.reason) }))} layout="vertical" margin={{ left: 20, right: 12 }}>
                  <CartesianGrid stroke={COLORS.grid} horizontal={false} />
                  <XAxis type="number" stroke={COLORS.axis} allowDecimals={false} fontSize={11} tickLine={false} />
                  <YAxis type="category" dataKey="name" stroke={COLORS.axis} width={130} fontSize={11} tickLine={false} />
                  <Tooltip {...tooltip} formatter={(v: number, _n, p) => [`${v} payment(s), ${money(p.payload.amount, cur)}`, "Failed"]} />
                  <Bar dataKey="count" fill={COLORS.red} radius={[0, 4, 4, 0]} barSize={14} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </Panel>
      </div>

      <div className="grid two">
        <Panel title="What the agent proposed, and what the policy allowed">
          {m.by_action.length === 0 ? <div className="empty">No decisions yet.</div> : (
            <div style={{ height: 240 }}>
              <ResponsiveContainer>
                <BarChart data={m.by_action.map((a) => ({ ...a, name: label(a.action) }))} layout="vertical" margin={{ left: 20, right: 12 }}>
                  <CartesianGrid stroke={COLORS.grid} horizontal={false} />
                  <XAxis type="number" stroke={COLORS.axis} allowDecimals={false} fontSize={11} tickLine={false} />
                  <YAxis type="category" dataKey="name" stroke={COLORS.axis} width={140} fontSize={11} tickLine={false} />
                  <Tooltip {...tooltip} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="approved" name="Approved" stackId="a" fill={COLORS.teal} barSize={14} />
                  <Bar dataKey="blocked" name="Blocked" stackId="a" fill={COLORS.amber} barSize={14} radius={[0, 4, 4, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </Panel>

        <Panel title="Recent recovery activity">
          {m.recent_activity.length === 0 ? <div className="empty">Nothing yet.</div> : (
            <div className="grid" style={{ gap: 0 }}>
              {m.recent_activity.map((a) => (
                <button key={a.decision_id} className="btn" style={{ textAlign: "left", border: 0, borderBottom: "1px solid var(--line-soft)", borderRadius: 0, background: "transparent", padding: "10px 4px" }}
                        onClick={() => onOpenPayment(a.payment_id)}>
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <span><b>{label(a.action)}</b> <span className="muted">· {a.customer_name}</span></span>
                    <span className="row"><PolicyBadge status={a.policy_status} /><span className="faint small">{ago(a.created_at)}</span></span>
                  </div>
                  <div className="faint small mono">{a.payment_id}</div>
                </button>
              ))}
            </div>
          )}
        </Panel>
      </div>

      <Panel title="How these numbers are calculated" action={<Badge tone="blue">from persisted data</Badge>}>
        <dl className="kv" style={{ gridTemplateColumns: "200px 1fr" }}>
          {Object.entries(m.definitions).map(([k, v]) => (<Fragment key={k}><dt>{label(k)}</dt><dd>{v}</dd></Fragment>))}
        </dl>
      </Panel>
    </div>
  );
}
