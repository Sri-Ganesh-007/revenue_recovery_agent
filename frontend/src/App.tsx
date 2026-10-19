import { useState } from "react";
import { api } from "./api";
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

export default function App() {
  const [page, setPage] = useState<Page>("overview");
  const [refreshKey, setRefreshKey] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const status = useLoad(api.gatewayStatus, [refreshKey]);
  const bump = () => setRefreshKey((k) => k + 1);
  const openPayment = (id: string) => { setPage("payments"); setSelected(id); };
  const current = NAV.find((n) => n.id === page)!;

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
