import { useState } from "react";
import { accessCode, api } from "./api";
import { Audit } from "./Audit";
import { Badge } from "./components";
import { Gateway } from "./Gateway";
import { useLoad } from "./hooks";
import { Overview } from "./Overview";
import { Payments } from "./Payments";

type Page = "overview" | "payments" | "gateway" | "audit";
const NAV: { id: Page; label: string; icon: string; title: string; blurb: string }[] = [
  { id: "overview", label: "Overview", icon: "◧", title: "Overview", blurb: "Recovery performance, computed from persisted payments and agent decisions." },
  { id: "payments", label: "Payments", icon: "☰", title: "Payment explorer", blurb: "Every payment, its failure reason and what the agent did about it." },
  { id: "gateway", label: "Gateway simulator", icon: "⚡", title: "Gateway simulator", blurb: "Send a synthetic gateway event through the real agent, policy and mock tools." },
  { id: "audit", label: "Audit history", icon: "☷", title: "Audit history", blurb: "Why each action was taken or blocked, straight from the saved decision records." },
];

function AccessGate({ onUnlocked }: { onUnlocked: () => void }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null); accessCode.set(code.trim());
    try { await api.metrics(); onUnlocked(); } catch (err) {
      accessCode.clear(); setError(err instanceof Error ? err.message : "Could not verify the code");
    }
    setBusy(false);
  }
  return (
    <div className="shell"><main className="main" style={{ maxWidth: 420, margin: "12vh auto" }}>
      <h1>Recovery Console</h1>
      <p>This public demo needs an access code. It is checked by the backend and kept only in this browser tab.</p>
      <form onSubmit={submit} className="row">
        <input type="password" autoComplete="off" placeholder="Access code" value={code} onChange={(e) => setCode(e.target.value)} />
        <button className="btn" disabled={busy || !code.trim()}>Unlock</button>
      </form>
      {error ? <p className="faint small">{error}</p> : null}
    </main></div>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>("overview");
  const [refreshKey, setRefreshKey] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const status = useLoad(api.gatewayStatus, [refreshKey]);
  const bump = () => setRefreshKey((k) => k + 1);
  const [unlocked, setUnlocked] = useState(accessCode.get() !== "");
  const openPayment = (id: string) => { setPage("payments"); setSelected(id); };
  const current = NAV.find((n) => n.id === page)!;

  if (status.data?.access_required && !unlocked) return <AccessGate onUnlocked={() => { setUnlocked(true); bump(); }} />;

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand"><div className="brand-mark">R</div><div><b>Recovery Console</b><span>AI payment recovery</span></div></div>
        <nav className="nav">
          {NAV.map((n) => (
            <button key={n.id} className={page === n.id ? "active" : ""} onClick={() => setPage(n.id)}><span>{n.icon}</span>{n.label}</button>
          ))}
        </nav>
        <div className="side-foot">
          {status.error ? <Badge tone="red">backend unreachable</Badge> : status.data ? (
            <div className="chips"><Badge tone="teal">backend online</Badge><Badge tone="blue">{status.data.llm_provider}</Badge>
              <Badge tone={status.data.demo_mode ? "violet" : "gray"}>{status.data.demo_mode ? "demo mode" : "read-only"}</Badge></div>
          ) : <span>connecting…</span>}
          <span className="faint">All actions are simulated.</span>
          {status.data?.access_required && (
            <button className="btn small" onClick={() => { accessCode.clear(); setUnlocked(false); }}>Lock</button>
          )}
        </div>
      </aside>
      <main className="main">
        <div className="page-head">
          <div><h1>{current.title}</h1><p>{current.blurb}</p></div>
          <button className="btn small" onClick={bump}>Refresh</button>
        </div>
        {page === "overview" && <Overview refreshKey={refreshKey} onOpenPayment={openPayment} />}
        {page === "payments" && <Payments refreshKey={refreshKey} selected={selected} onSelect={setSelected} onChanged={bump} />}
        {page === "gateway" && <Gateway refreshKey={refreshKey} onChanged={bump} onOpenPayment={openPayment} />}
        {page === "audit" && <Audit refreshKey={refreshKey} focusPayment={null} onOpenPayment={openPayment} />}
      </main>
    </div>
  );
}
