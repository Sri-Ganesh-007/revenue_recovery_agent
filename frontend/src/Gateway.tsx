import { useRef, useState } from "react";
import { api } from "./api";
import { Badge, ErrorBox, Kpi, Panel, Spinner } from "./components";
import { money } from "./format";
import { useLoad } from "./hooks";
import { Timeline } from "./Timeline";
import { buildSteps, type RunView } from "./steps";
import type { BatchItem, EventKind } from "./types";

const SCENARIOS: { kind: EventKind; title: string; desc: string }[] = [
  { kind: "insufficient_funds", title: "Insufficient-funds failure", desc: "A retry may be allowed if the policy conditions are met." },
  { kind: "gateway_timeout", title: "Gateway timeout", desc: "Ambiguous outcome: must escalate to a human, never auto-retry." },
  { kind: "success", title: "Successful payment (control)", desc: "Already paid: recovery must be rejected before the model is called." },
  { kind: "retry_limit", title: "Retry-limit test", desc: "Three retries were already declined: a fourth must be blocked." },
];

export function Gateway({ refreshKey, onChanged, onOpenPayment }: { refreshKey: number; onChanged: () => void; onOpenPayment: (id: string) => void }) {
  const status = useLoad(api.gatewayStatus, []);
  const metrics = useLoad(api.metrics, [refreshKey]);
  const [view, setView] = useState<RunView | null>(null);
  const [busy, setBusy] = useState(false);
  const [replay, setReplay] = useState<string | null>(null);
  const [batch, setBatch] = useState<BatchItem[] | null>(null);
  const [batchError, setBatchError] = useState<unknown>(null);
  const live = useRef<RunView | null>(null);

  const update = (patch: Partial<RunView>) => { live.current = { ...(live.current as RunView), ...patch }; setView(live.current); };
  const disabled = busy || status.data?.demo_mode === false;

  async function run(kind: EventKind) {
    if (disabled) return;
    setBusy(true); setReplay(null); setBatch(null);
    live.current = { kind, phase: "event" }; setView(live.current);
    let paymentId: string;
    try {
      const ev = await api.createEvent(kind);                       // 1. the mock gateway announces a payment outcome
      paymentId = ev.event.payment_id;
      update({ event: ev, phase: "recovery" });
    } catch (e) { update({ eventError: e, phase: "finished" }); setBusy(false); return; }

    const t0 = performance.now();
    try {
      const { run, replayed } = await api.simulate(paymentId, `gw-${paymentId}`);   // 2. the EXISTING recovery endpoint
      update({ run, replayed });
      try { update({ audit: (await api.audit({ payment_id: paymentId, limit: 1 }))[0] }); } catch { /* timestamps are optional */ }
    } catch (e) { update({ runError: e }); }
    update({ phase: "finished", elapsedMs: performance.now() - t0 });
    onChanged(); setBusy(false);
  }

  async function replayRequest() {
    const id = view?.run?.payment_id; if (!id) return;
    setBusy(true);
    try {
      const { run, replayed } = await api.simulate(id, `gw-${id}`);
      setReplay(replayed && run.run_id === view?.run?.run_id ? "Same request again → stored result returned (idempotent replay). No second model call, no duplicate rows." : `Unexpected: replayed=${replayed}`);
    } catch (e) { setReplay(`Replay failed: ${e instanceof Error ? e.message : e}`); }
    setBusy(false);
  }

  async function settle(outcome: "success" | "failure") {
    const id = view?.run?.payment_id; if (!id) return;
    setBusy(true);
    try { update({ settled: { outcome, detail: await api.settle(id, outcome) }, settleError: undefined }); onChanged(); }
    catch (e) { update({ settleError: e }); }
    setBusy(false);
  }

  const batchSize = Math.max(1, Math.min(5, status.data?.batch_max ?? 1));

  async function runBatch() {
    setBusy(true); setBatch(null); setBatchError(null); setView(null);
    try { setBatch(await api.batch(batchSize)); onChanged(); } catch (e) { setBatchError(e); }
    setBusy(false);
  }

  const m = metrics.data;
  const scheduled = view?.run?.simulated_action?.status === "scheduled" && !view.settled;

  return (
    <div className="grid">
      {status.error ? <ErrorBox error={status.error} onRetry={status.reload} /> : null}
      {status.data && !status.data.demo_mode && (
        <div className="notice"><b>Demo gateway is switched off.</b> Set <code>DEMO_MODE=true</code> on the backend to enable the simulator. The rest of the console still works.</div>
      )}

      <div className="grid three">
        <Kpi title="Revenue at risk" tone="amber" value={m ? money(m.revenue_at_risk, m.currency) : "…"} sub={m ? `${m.failed_payments} failed payments` : undefined} />
        <Kpi title="Revenue recovered" tone="teal" value={m ? money(m.revenue_recovered, m.currency) : "…"} sub="only after the processor confirms" />
        <Kpi title="LLM provider" value={status.data?.llm_provider ?? "…"} sub="the agent's decision-maker" />
      </div>

      <Panel title="Generate a synthetic gateway event" action={busy ? <span className="muted small"><Spinner /> working…</span> : <Badge tone="blue">simulated</Badge>}>
        <div className="scenarios">
          {SCENARIOS.map((s) => (
            <button key={s.kind} className="scenario" disabled={disabled} onClick={() => run(s.kind)}>
              <b>{s.title}</b><span>{s.desc}</span>
            </button>
          ))}
        </div>
        {status.data?.batch_enabled ? (
          <div className="row" style={{ marginTop: 14 }}>
            <button className="btn" disabled={disabled} onClick={runBatch}>Run a batch of {batchSize} events</button>
            <span className="faint small">Each event is a real LLM call. Nothing is charged and no message is sent.</span>
          </div>
        ) : <p className="faint small" style={{ marginTop: 14 }}>Batch runs are switched off on this server (DEMO_BATCH_ENABLED).</p>}
      </Panel>

      {batchError ? <ErrorBox error={batchError} /> : null}
      {batch && (
        <Panel title="Batch result">
          <div className="table-wrap"><table>
            <thead><tr><th>Payment</th><th>Event</th><th>Result</th><th>Action taken</th><th>Detail</th></tr></thead>
            <tbody>{batch.map((b) => (
              <tr key={b.payment_id} className="click" onClick={() => onOpenPayment(b.payment_id)}>
                <td className="mono">{b.payment_id}</td><td>{b.kind.replace(/_/g, " ")}</td>
                <td><Badge tone={b.result === "completed" ? "teal" : b.result === "blocked" ? "amber" : b.result === "rejected" ? "gray" : "red"}>{b.result}</Badge></td>
                <td>{b.action_taken?.replace(/_/g, " ") ?? "none"}</td><td className="muted small">{b.detail}</td>
              </tr>))}</tbody>
          </table></div>
        </Panel>
      )}

      {view && (
        <Panel title="Agent activity" action={view.elapsedMs ? <span className="faint small">recovery request took {(view.elapsedMs / 1000).toFixed(1)} s (measured in your browser)</span> : undefined}>
          <Timeline steps={buildSteps(view)} />
          {scheduled && (
            <div className="row" style={{ marginTop: 6 }}>
              <button className="btn primary" disabled={busy} onClick={() => settle("success")}>Processor confirms payment</button>
              <button className="btn danger" disabled={busy} onClick={() => settle("failure")}>Processor declines</button>
            </div>
          )}
          {view.run && (
            <div className="row" style={{ marginTop: 14 }}>
              <button className="btn small" disabled={busy} onClick={replayRequest}>Send the same request again (idempotency)</button>
              <button className="btn small" onClick={() => onOpenPayment(view.run!.payment_id)}>Open payment</button>
              {replay && <span className="muted small">{replay}</span>}
            </div>
          )}
        </Panel>
      )}
    </div>
  );
}
