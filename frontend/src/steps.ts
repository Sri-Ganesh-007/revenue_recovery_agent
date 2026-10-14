import { ApiError } from "./api";
import { dateTime, label, money, pct } from "./format";
import type { AuditRecord, EventKind, GatewayEventResponse, PaymentDetail, RecoveryRun } from "./types";

export type StepState = "pending" | "running" | "done" | "blocked" | "skipped" | "error";
export type Step = { key: string; title: string; state: StepState; lines: [string, string][]; note?: string };

/** Everything the timeline shows comes from these real API responses; nothing is simulated in the browser. */
export type RunView = {
  kind: EventKind; phase: "event" | "recovery" | "finished";
  event?: GatewayEventResponse; eventError?: unknown;
  run?: RecoveryRun; runError?: unknown; replayed?: boolean; audit?: AuditRecord; elapsedMs?: number;
  settled?: { outcome: "success" | "failure"; detail: PaymentDetail }; settleError?: unknown;
};

const msg = (e: unknown) => (e instanceof ApiError ? (e.status ? `HTTP ${e.status}: ${e.detail}` : e.detail) : e instanceof Error ? e.message : String(e));
const args = (a: Record<string, unknown>) => (Object.keys(a).length ? ` ${JSON.stringify(a)}` : "");

export function buildSteps(v: RunView): Step[] {
  const ev = v.event, ctx = ev?.context_preview ?? null, run = v.run;
  const rejected = v.runError instanceof ApiError && v.runError.status === 409;
  const recoveryDown = Boolean(v.runError) && !rejected;
  const waiting = v.phase !== "finished" && !run;

  const s1: Step = v.eventError
    ? { key: "event", title: "Event received", state: "error", lines: [["Error", msg(v.eventError)]] }
    : !ev
      ? { key: "event", title: "Event received", state: "running", lines: [] }
      : { key: "event", title: "Event received", state: "done", lines: [
          ["Payment", ev.event.payment_id], ["Customer", `${ev.event.customer_name} (${ev.event.customer_id})`],
          ["Plan / amount", `${ev.event.plan} · ${money(ev.event.amount, ev.event.currency)}`],
          ["Gateway reported", `${ev.event.payment_status}${ev.event.failure_reason ? ` · ${label(ev.event.failure_reason)}` : ""}`],
          ["Received", dateTime(ev.event.received_at)]] };

  const s2: Step = !ev
    ? { key: "context", title: "Payment context loaded", state: "pending", lines: [] }
    : !ctx
      ? { key: "context", title: "Payment context loaded", state: "blocked", lines: [["Not eligible", ev.ineligible_reason ?? "unknown"]],
          note: "The recovery context builder only accepts payments that are still failed." }
      : { key: "context", title: "Payment context loaded", state: "done", lines: [
          ["Failure reason", label(ctx.failed_payment?.failure_reason)],
          ["Subscription", `${ctx.subscription?.status} · ${ctx.subscription?.plan}`],
          ["Payment history", `${ctx.payment_pattern?.successful_payments}/${ctx.payment_pattern?.total_payments} succeeded (${pct(ctx.payment_pattern?.success_rate ?? null)})`],
          ["Earlier recovery attempts", String(ctx.recovery_history?.length ?? 0)],
          ...(ev.retry_policy_preview ? [["Policy dry run (retry)", `${ev.retry_policy_preview.allowed ? "would be allowed" : "would be blocked"} · ${ev.retry_policy_preview.rule}`] as [string, string]] : [])],
          note: "This is the context the agent is shown (customer name and email are not sent to the model). The dry run is not executed." };

  let s3: Step, s4: Step, s5: Step, s6: Step;
  if (rejected || (ev && !ev.eligible_for_recovery && v.phase === "finished" && !run)) {
    const why = v.runError ? msg(v.runError) : ev?.ineligible_reason ?? "";
    s3 = { key: "proposal", title: "AI proposal generated", state: "skipped", lines: [["Backend response", why]], note: "The model was not called: the backend rejected the request before reaching it." };
    s4 = { key: "policy", title: "Policy check", state: "skipped", lines: [] };
    s5 = { key: "tool", title: "Mock tool", state: "blocked", lines: [["Execution prevented", "Recovery was rejected for this payment"]] };
    s6 = { key: "persist", title: "Outcome persisted", state: "skipped", lines: [], note: "Nothing was saved: no decision and no recovery attempt." };
  } else if (recoveryDown) {
    const e = { key: "proposal", title: "AI proposal generated", state: "error" as StepState, lines: [["Error", msg(v.runError)]] as [string, string][] };
    s3 = e;
    s4 = { key: "policy", title: "Policy check", state: "skipped", lines: [] };
    s5 = { key: "tool", title: "Mock tool", state: "skipped", lines: [] };
    s6 = { key: "persist", title: "Outcome persisted", state: "skipped", lines: [], note: "Nothing was saved, so the same request can simply be retried." };
  } else if (!run) {
    const state: StepState = waiting && v.phase === "recovery" ? "running" : "pending";
    s3 = { key: "proposal", title: "AI proposal generated", state, lines: [], note: state === "running" ? "Waiting for the LLM (this can take several seconds)…" : undefined };
    s4 = { key: "policy", title: "Policy check", state: "pending", lines: [] };
    s5 = { key: "tool", title: "Mock tool", state: "pending", lines: [] };
    s6 = { key: "persist", title: "Outcome persisted", state: "pending", lines: [] };
  } else {
    const calls = run.tool_calls;
    s3 = { key: "proposal", title: "AI proposal generated", state: "done", lines: [
      ...calls.map((c, i): [string, string] => [`Proposal ${i + 1}`, c.name ? `${label(c.name)}${args(c.arguments)}` : "unusable model output"]),
      ...(run.decision ? [["Explanation (narrative)", run.decision.summary] as [string, string]] : [])],
      note: calls.length > 1 ? "The first proposal was blocked, so the model was asked once more with the policy's reason." : undefined };
    s4 = { key: "policy", title: "Policy approved or blocked", state: run.run_status === "completed" ? "done" : "blocked",
      lines: calls.map((c, i): [string, string] => [`Verdict ${i + 1}`, c.policy
        ? `${c.policy.allowed ? "ALLOWED" : "BLOCKED"} · ${c.policy.rule} · ${c.policy.reason}` : `rejected · ${c.error ?? ""}`]),
      note: v.replayed ? "Replay of an identical earlier request: stored result returned, no new model call." : undefined };
    const exec = calls.find((c) => c.status === "executed_simulated");
    s5 = run.simulated_action
      ? { key: "tool", title: "Mock tool executed", state: "done", lines: [
          ["Tool", label(run.simulated_action.action)], ["Recorded status", label(run.simulated_action.status)],
          ...(run.simulated_action.scheduled_for ? [["Retry planned for", dateTime(run.simulated_action.scheduled_for)] as [string, string]] : []),
          ["Tool result", String(exec?.result?.detail ?? "")]], note: "Simulated: no card was charged and no message was sent." }
      : { key: "tool", title: "Execution prevented", state: "blocked", lines: [["Why", "No proposal was approved by the policy, so nothing ran."]] };
    s6 = { key: "persist", title: "Outcome persisted", state: "done", lines: [
      ["Decision id", run.run_id], ["Policy status", run.decision?.policy_status ?? "-"],
      ["Recovery attempt", run.simulated_action?.id ?? "none (nothing executed)"],
      ...(v.audit ? [["Saved at", dateTime(v.audit.created_at)] as [string, string]] : []),
      ["Idempotency key", run.idempotency_key]] };
  }

  const steps = [s1, s2, s3, s4, s5, s6];
  if (run?.simulated_action?.status === "scheduled") {
    steps.push(v.settleError
      ? { key: "processor", title: "Mock processor", state: "error", lines: [["Error", msg(v.settleError)]] }
      : v.settled
        ? { key: "processor", title: v.settled.outcome === "success" ? "Processor confirmed the retry" : "Processor declined the retry",
            state: v.settled.outcome === "success" ? "done" : "blocked", lines: [
              ["Payment status", v.settled.detail.payment.status], ["Recovery status", label(v.settled.detail.payment.recovery_status)],
              ["Revenue", v.settled.outcome === "success" ? `${money(v.settled.detail.payment.amount, v.settled.detail.payment.currency)} recovered` : "not recovered"]],
          note: "Revenue counts as recovered only when the simulated processor confirms." }
        : { key: "processor", title: "Mock processor", state: "pending", lines: [], note: "The retry is only scheduled. It is not revenue until the processor confirms it." });
  }
  return steps;
}
