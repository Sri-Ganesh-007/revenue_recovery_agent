// The backend stores naive UTC timestamps (no zone suffix); treat them as UTC.
export const parseUtc = (s: string): Date => new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : `${s}Z`);

export function money(value: number, currency = "INR"): string {
  return new Intl.NumberFormat("en-IN", { style: "currency", currency, maximumFractionDigits: 0 }).format(value);
}
export const pct = (v: number | null): string => (v === null ? "n/a" : `${(v * 100).toFixed(0)}%`);
export const dateTime = (s: string): string =>
  parseUtc(s).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
export function ago(s: string): string {
  const seconds = Math.max(0, (Date.now() - parseUtc(s).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}
export const label = (s: string | null | undefined): string => (s ? s.replace(/_/g, " ") : "none");
export const RECOVERY_STATUSES = ["unprocessed", "retry_scheduled", "retry_declined", "reminder_sent", "update_requested",
  "escalated", "no_action", "blocked", "recovered", "paid"] as const;
export const FAILURE_REASONS = ["insufficient_funds", "gateway_timeout", "expired_card", "processor_decline", "authentication_required",
  "invalid_payment_details", "suspected_fraud", "processing_limit_exceeded", "transaction_not_allowed"] as const;
