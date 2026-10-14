import type {
  AuditRecord, BatchItem, EventKind, GatewayEventResponse, GatewayStatus, Metrics, PaymentDetail, PaymentPage, RecoveryRun,
} from "./types";

const BASE = ((import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(`HTTP ${status}: ${detail}`);
  }
}

function describe(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => (d && typeof d === "object" && "msg" in d ? String((d as any).msg) : JSON.stringify(d))).join("; ");
  return detail ? JSON.stringify(detail) : "Unknown error";
}

// The demo access code is typed by the visitor and kept only for this browser tab; it is never part of the bundle.
const CODE_KEY = "demo_access_code";
export const accessCode = {
  get(): string { try { return sessionStorage.getItem(CODE_KEY) ?? ""; } catch { return ""; } },
  set(code: string) { try { sessionStorage.setItem(CODE_KEY, code); } catch { /* storage unavailable */ } },
  clear() { try { sessionStorage.removeItem(CODE_KEY); } catch { /* storage unavailable */ } },
};

async function request<T>(path: string, init: RequestInit = {}): Promise<{ data: T; headers: Headers }> {
  let response: Response;
  const code = accessCode.get();
  const headers = { ...(init.headers as Record<string, string> | undefined), ...(code ? { "X-Demo-Token": code } : {}) };
  try {
    response = await fetch(`${BASE}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, `Cannot reach the backend at ${BASE}. Is it running, and is this site's origin listed in CORS_ORIGINS?`);
  }
  const text = await response.text();
  let body: any = null;
  try { body = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
  if (!response.ok) throw new ApiError(response.status, describe(body?.detail ?? (text.slice(0, 200) || response.statusText)));
  return { data: body as T, headers: response.headers };
}

const json = (body: unknown, extra: Record<string, string> = {}): RequestInit => ({
  method: "POST", headers: { "Content-Type": "application/json", ...extra }, body: JSON.stringify(body),
});
const qs = (params: Record<string, string | number | undefined>) => {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== "") q.set(k, String(v)); });
  const s = q.toString();
  return s ? `?${s}` : "";
};

export const api = {
  base: BASE,
  gatewayStatus: async () => (await request<GatewayStatus>("/gateway/status")).data,
  metrics: async () => (await request<Metrics>("/dashboard/metrics")).data,
  payments: async (p: { status?: string; recovery_status?: string; failure_reason?: string; search?: string; limit?: number; offset?: number }) =>
    (await request<PaymentPage>(`/payments${qs(p)}`)).data,
  payment: async (id: string) => (await request<PaymentDetail>(`/payments/${encodeURIComponent(id)}`)).data,
  audit: async (p: { payment_id?: string; limit?: number } = {}) => (await request<AuditRecord[]>(`/audit${qs(p)}`)).data,
  createEvent: async (kind: EventKind) => (await request<GatewayEventResponse>("/gateway/events", json({ kind }))).data,
  /** The EXISTING recovery endpoint. The key makes a repeat of the same request a replay, not a second run. */
  simulate: async (paymentId: string, key: string) => {
    const { data, headers } = await request<RecoveryRun>("/recovery/simulate", json({ payment_id: paymentId }, { "Idempotency-Key": key }));
    return { run: data, replayed: headers.get("Idempotent-Replayed") === "true" };
  },
  settle: async (paymentId: string, outcome: "success" | "failure") =>
    (await request<PaymentDetail>(`/gateway/payments/${encodeURIComponent(paymentId)}/settle`, json({ outcome }))).data,
  batch: async (count: number) => (await request<BatchItem[]>("/gateway/batch", json({ count }))).data,
};
