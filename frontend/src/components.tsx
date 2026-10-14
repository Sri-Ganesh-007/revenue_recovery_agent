import type { ReactNode } from "react";
import { ApiError } from "./api";
import { label } from "./format";

export function Panel({ title, action, children, className = "" }: { title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`panel ${className}`}>
      {(title || action) && <div className="panel-head"><h2>{title}</h2>{action}</div>}
      {children}
    </section>
  );
}

export function Kpi({ title, value, sub, tone }: { title: string; value: string; sub?: string; tone?: "teal" | "amber" | "red" | "blue" }) {
  return (
    <div className={`panel kpi ${tone ?? ""}`}>
      <h3>{title}</h3>
      <div className="value">{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

type Tone = "teal" | "red" | "amber" | "blue" | "violet" | "gray";
export function Badge({ tone = "gray", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

const RECOVERY_TONE: Record<string, Tone> = {
  recovered: "teal", paid: "gray", retry_scheduled: "blue", retry_declined: "red", reminder_sent: "violet",
  update_requested: "violet", escalated: "amber", no_action: "gray", blocked: "red", unprocessed: "gray",
};
export const RecoveryBadge = ({ status }: { status: string }) => <Badge tone={RECOVERY_TONE[status] ?? "gray"}>{label(status)}</Badge>;
export const PaymentStatusBadge = ({ status }: { status: string }) => <Badge tone={status === "success" ? "teal" : status === "failed" ? "red" : "gray"}>{status}</Badge>;
export const PolicyBadge = ({ status }: { status: string | null }) =>
  status === "approved" ? <Badge tone="teal">approved</Badge> : status === "blocked" ? <Badge tone="red">blocked</Badge> : <Badge>n/a</Badge>;

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const status = error instanceof ApiError ? error.status : null;
  const detail = error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
  return (
    <div className="error" role="alert">
      <b>{status ? `Request failed (HTTP ${status})` : "Request failed"}</b>
      <div className="small" style={{ marginTop: 4 }}>{detail}</div>
      {onRetry && <button className="btn small" style={{ marginTop: 10 }} onClick={onRetry}>Try again</button>}
    </div>
  );
}

export const Spinner = () => <span className="spinner" aria-label="loading" />;
export const Empty = ({ children }: { children: ReactNode }) => <div className="empty">{children}</div>;
