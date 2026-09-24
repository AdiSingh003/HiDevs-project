import { api } from './api';
import { Badge } from './components/ui';
import { useAsync, useHashRoute } from './hooks';
import { Arena } from './pages/Arena';
import { Audit } from './pages/Audit';
import { Contracts } from './pages/Contracts';
import { Rfq } from './pages/Rfq';
import { Telemetry } from './pages/Telemetry';

const NAV = [
  { key: 'arena', label: 'Arena' },
  { key: 'rfq', label: 'Multi-vendor RFQ' },
  { key: 'contracts', label: 'Contracts' },
  { key: 'telemetry', label: 'Telemetry' },
  { key: 'audit', label: 'Audit · AIMS' },
];

export default function App() {
  const [route, navigate] = useHashRoute();
  const status = useAsync(() => api.status(), []);
  const page = route[0] ?? 'arena';
  const param = route[1];
  const s = status.data;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">⇄</div>
          <div>
            <div className="brand-title">Negotiation Arena</div>
            <div className="brand-sub">bounded agents · Legal Arbiter</div>
          </div>
        </div>
        <nav className="nav">
          {NAV.map((n) => (
            <a key={n.key} href={`#/${n.key}`} className={page === n.key ? 'active' : ''}>{n.label}</a>
          ))}
        </nav>
        <div className="status-pills">
          {s ? (
            <>
              <Badge tone={s.lyzr.negotiator_agents ? 'ok' : ''} title={s.lyzr.configured ? 'Lyzr API key configured' : 'Offline: deterministic policy engine'}>
                Lyzr {s.lyzr.negotiator_agents ? 'agents' : s.lyzr.configured ? 'key only' : 'offline'}
              </Badge>
              <Badge tone="arbiter" title={`Legal Arbiter rules always on${s.safe_ai.lyzr_rai ? '; Lyzr RAI' : ''}${
                s.lyzr.rai_per_party ? ' (per-party policies)' : ''}${s.safe_ai.lyzr_opa ? '; Lyzr OPA guardrails' : ''}`}>
                Safe AI {s.safe_ai.lyzr_rai || s.safe_ai.lyzr_opa ? 'local + Lyzr' : 'local'}
              </Badge>
              <Badge tone={s.aims.mode !== 'local' ? 'ok' : ''} title="Hash-chained audit ledger, mirrored to Lyzr AIMS when configured">
                AIMS {s.aims.mode}
              </Badge>
              <Badge title={s.automata.engine}>Automata · {s.automata.model}</Badge>
            </>
          ) : <Badge tone="bad">API unreachable</Badge>}
        </div>
      </header>
      <main className="page">
        {page === 'arena' ? <Arena status={s} runId={param} navigate={navigate} /> : null}
        {page === 'rfq' ? <Rfq status={s} runId={param} navigate={navigate} /> : null}
        {page === 'contracts' ? <Contracts contractId={param} navigate={navigate} /> : null}
        {page === 'telemetry' ? <Telemetry contractId={param} navigate={navigate} /> : null}
        {page === 'audit' ? <Audit stream={param} navigate={navigate} /> : null}
      </main>
      <footer className="footer">Lyzr Agent API · Lyzr Automata · Lyzr Safe AI (RAI + OPA) · Lyzr AIMS · v{s?.version ?? '1.0.0'}</footer>
    </div>
  );
}
