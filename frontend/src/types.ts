// These types mirror the backend's Pydantic response models (app/schemas/*.py). Nothing here is invented.

export type PolicyVerdict = {
  allowed: boolean; action: string; rule: string; reason: string; category: string;
  rules_evaluated: string[]; alternatives: string[];
};
export type ToolCallRecord = {
  name: string | null; arguments: Record<string, unknown>;
  status: "executed_simulated" | "rejected" | "error";
  result: Record<string, unknown> | null; error: string | null; policy: PolicyVerdict | null;
};
export type DecisionSummary = {
  id: string; proposed_action: string; parameters: Record<string, unknown> | null;
  policy_status: "approved" | "blocked"; policy_reason: string; summary: string;
};
export type SimulatedActionSummary = { id: string; action: string; status: string; scheduled_for: string | null };
export type RecoveryRun = {
  simulated: true; payment_id: string; action_taken: string | null;
  stopped_reason: "completed" | "turn_limit" | "rejection_limit"; tool_calls: ToolCallRecord[];
  final_message: string | null; final_message_is_narrative_only: true; run_id: string; idempotency_key: string;
  run_status: "completed" | "blocked"; idempotent_replay: boolean;
  decision: DecisionSummary | null; simulated_action: SimulatedActionSummary | null; message: string;
};

export type PaymentRow = {
  id: string; customer_id: string; customer_name: string; amount: number; currency: string;
  status: string; failure_reason: string | null; created_at: string;
  recovery_status: string; last_action: string | null; last_policy_status: string | null;
};
export type PaymentPage = { total: number; items: PaymentRow[] };
export type AttemptRow = { id: string; strategy: string; status: string; outcome: string | null; attempted_at: string };
export type AuditRecord = {
  decision_id: string; payment_id: string; customer_id: string; customer_name: string;
  proposed_action: string; parameters: Record<string, unknown> | null;
  policy_status: string | null; policy_reason: string | null; explanation: string | null;
  failure_reason: string | null; attempts: ToolCallRecord[]; simulated_action: AttemptRow | null; created_at: string;
};
export type PaymentDetail = {
  payment: PaymentRow; subscription_plan: string | null; recovery_attempts: AttemptRow[];
  decisions: AuditRecord[]; customer_recent_payments: PaymentRow[];
};

export type Metrics = {
  currency: string; failed_payments: number; revenue_at_risk: number; recovered_payments: number;
  revenue_recovered: number; payments_processed: number; recovery_success_rate: number | null;
  retries_settled: number; retries_confirmed: number; retry_confirmation_rate: number | null;
  escalations_open: number; decisions_total: number; decisions_approved: number; decisions_blocked: number;
  by_action: { action: string; approved: number; blocked: number }[];
  by_failure_reason: { reason: string; count: number; amount: number }[];
  by_day: { date: string; approved: number; blocked: number; recovered_amount: number }[];
  recent_activity: { decision_id: string; payment_id: string; customer_name: string; action: string;
                     policy_status: string | null; summary: string | null; created_at: string }[];
  definitions: Record<string, string>;
};

export type EventKind = "insufficient_funds" | "gateway_timeout" | "success" | "retry_limit" |
  "expired_card" | "authentication_required" | "processor_decline" | "suspected_fraud";
export type GatewayStatus = {
  demo_mode: boolean; llm_provider: string; currency: string; batch_enabled: boolean; batch_max: number;
};
export type GatewayEvent = {
  kind: EventKind; payment_id: string; customer_id: string; customer_name: string; plan: string; amount: number;
  currency: string; payment_status: string; failure_reason: string | null; received_at: string; prior_retries: number;
};
export type PolicyPreview = { action: string; allowed: boolean; rule: string; reason: string; category: string };
export type GatewayEventResponse = {
  event: GatewayEvent; eligible_for_recovery: boolean; ineligible_reason: string | null;
  context_preview: Record<string, any> | null; retry_policy_preview: PolicyPreview | null;
};
export type BatchItem = {
  payment_id: string; kind: EventKind; result: "completed" | "blocked" | "rejected" | "error";
  action_taken: string | null; detail: string | null;
};
