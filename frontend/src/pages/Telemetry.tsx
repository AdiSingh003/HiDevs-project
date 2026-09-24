import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import { Timeline } from '../components/Timeline';
import { Badge, Card, Empty, ErrorBox, Label, StatusBadge } from '../components/ui';
import { fmtMoney, fmtValue, pretty } from '../format';
import { useAsync, useRunStream } from '../hooks';
import { reduceEvents } from '../state';

interface Preset {
  id: string; title: string; desc: string;
  event: Record<string, string | number>;
}

const PRESETS: Preset[] = [
  { id: 'cyclone', title: '🌀 Cyclone - port closed', desc: 'Severe weather, severity 4, +7 days (force majeure)',
    event: { event_type: 'severe_weather', severity: 4, expected_delay_days: 7, source: 'imd-weather-feed', region: 'Bay of Bengal',
      description: 'Cyclone warning: port operations suspended' } },
  { id: 'port', title: '⚓ Port closure', desc: 'Authority closure, severity 3, +5 days (force majeure)',
    event: { event_type: 'port_closure', severity: 3, expected_delay_days: 5, source: 'port-authority-api', region: 'Rotterdam',
      description: 'Terminal closed by harbour master' } },
  { id: 'carrier', title: '🚚 Carrier delay', desc: 'Not force majeure, +3 days - LD schedule applies',
    event: { event_type: 'carrier_delay', severity: 2, expected_delay_days: 3, source: 'tms-tracking', region: 'NH-48',
      description: 'Carrier breakdown en route' } },
  { id: 'copper', title: '📈 Input index +12%', desc: 'Commodity spike above the 3% adjustment threshold',
    event: { event_type: 'commodity_price_spike', severity: 3, price_index: 'LME Copper', price_index_change_pct: 12,
      material_share_pct: 60, source: 'lme-market-data', description: 'Input-cost index spike' } },
  { id: 'small', title: '📉 Index +1.5%', desc: 'Below threshold - contract price stands',
    event: { event_type: 'commodity_price_spike', severity: 1, price_index: 'LME Copper', price_index_change_pct: 1.5,
      material_share_pct: 60, source: 'lme-market-data', description: 'Minor index movement' } },
];

export function Telemetry({ contractId, navigate }: { contractId?: string; navigate: (p: string) => void }) {
  const contracts = useAsync(() => api.contracts(), []);
  const selected = contractId ?? contracts.data?.[0]?.contract_id;
  const contract = useAsync(() => (selected ? api.contract(selected) : Promise.resolve(null)), [selected]);
  const [preset, setPreset] = useState<Preset>(PRESETS[0]);
  const [form, setForm] = useState<Record<string, string | number>>(PRESETS[0].event);
  const [resp, setResp] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { events, live } = useRunStream(resp?.renegotiation_id ?? null, 'god');
  const state = useMemo(() => reduceEvents(events), [events]);

  useEffect(() => { setForm(preset.event); }, [preset]);
  useEffect(() => { setResp(null); }, [selected]);
  useEffect(() => { if (state.contract) contract.reload(); /* refresh to latest version */ // eslint-disable-next-line
  }, [state.contract?.version]);

  async function send() {
    if (!selected) return;
    setBusy(true);
    setErr(null);
    setResp(null);
    try {
      setResp(await api.simulateTelemetry({ ...form, contract_id: selected, speed_ms: 450 }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const c = contract.data;
  const assessment = resp?.assessment;
  const curl = resp?.signed_headers ? [
    `curl -X POST http://localhost:8000/api/webhooks/telemetry \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -H "X-Telemetry-Timestamp: <unix-seconds>" \\`,
    `  -H "X-Telemetry-Signature: sha256=<hmac_sha256(secret, timestamp + '.' + body)>" \\`,
    `  -d '${JSON.stringify({ ...form, contract_id: selected })}'`,
  ].join('\n') : '';

  if (contracts.data && contracts.data.length === 0) {
    return <Empty title="No executed contracts yet">Run a negotiation first - telemetry renegotiates live contracts.</Empty>;
  }
  return (
    <div className="split">
      <div className="stack" style={{ gap: 12 }}>
        <Card>
          <Label>Target contract</Label>
          <select className="select" value={selected ?? ''} onChange={(e) => navigate(`telemetry/${e.target.value}`)}>
            {(contracts.data ?? []).map((x) => <option key={x.contract_id} value={x.contract_id}>{x.contract_id} · {x.supplier} (v{x.version})</option>)}
          </select>
          {c ? (
            <table className="table" style={{ marginTop: 10 }}><tbody>
              {Object.entries(c.terms).map(([k, v]) => {
                const spec = c.issues.find((i) => i.key === k);
                return <tr key={k}><td className="muted">{spec?.label ?? k}</td><td className="num">{fmtValue(spec, v)}</td></tr>;
              })}
              <tr><td className="muted">Version</td><td className="num">v{c.version} <StatusBadge status={c.status} /></td></tr>
            </tbody></table>
          ) : null}
        </Card>
        <Card>
          <Label>Event presets</Label>
          <div className="stack">
            {PRESETS.map((p) => (
              <button key={p.id} className={`preset ${preset.id === p.id ? 'on' : ''}`} onClick={() => setPreset(p)}>
                <div className="t">{p.title}</div><div className="d">{p.desc}</div>
              </button>
            ))}
          </div>
        </Card>
      </div>
      <div className="stack" style={{ gap: 12 }}>
        <Card>
          <Label hint="HMAC-SHA256 signed">Telemetry webhook</Label>
          <div className="grid-3">
            {Object.entries(form).map(([k, v]) => (
              <label key={k} className="field"><span>{pretty(k)}</span>
                <input className="input" value={v} onChange={(e) => setForm({ ...form,
                  [k]: typeof v === 'number' ? Number(e.target.value) : e.target.value })} />
              </label>
            ))}
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <span className="small muted">The server signs the event with the webhook secret and dispatches it through the verified webhook path.</span>
            <span className="spacer" />
            <button className="btn primary" disabled={busy || !selected} onClick={send}>⚡ Send signed event</button>
          </div>
          <ErrorBox error={err} />
        </Card>
        {assessment ? (
          <Card>
            <Label hint={resp.event_id}>Legal Arbiter assessment</Label>
            <div className="row wrap" style={{ gap: 6, marginBottom: 8 }}>
              <Badge tone={assessment.action === 'renegotiate' ? 'accent' : assessment.action === 'apply_sla' ? 'warn' : ''}>{pretty(assessment.action)}</Badge>
              {assessment.force_majeure ? <Badge tone="arbiter">force majeure</Badge> : null}
              {assessment.affected_issues?.map((i: string) => <Badge key={i}>{pretty(i)}</Badge>)}
            </div>
            <div className="small">{assessment.reason}</div>
            {assessment.sla_estimate ? (
              <div className="kpi" style={{ marginTop: 10 }}><div className="k">Liquidated damages</div>
                <div className="v">{fmtMoney(assessment.sla_estimate.amount, c?.commercial_terms.currency)}</div>
                <div className="s">{assessment.sla_estimate.explanation}</div></div>
            ) : null}
            <details style={{ marginTop: 10 }}><summary className="small faint">Equivalent signed webhook call</summary>
              <pre className="json" style={{ marginTop: 6 }}>{curl}</pre>
            </details>
          </Card>
        ) : null}
        {resp?.renegotiation_id ? (
          <Card>
            <div className="stage-head">
              <div className="h2">Live renegotiation</div>
              <span className="small mono muted">{resp.renegotiation_id}</span>
              <span className="spacer" />
              {live ? <Badge tone="accent"><span className="dot pulse" /> live</Badge> : null}
              <StatusBadge status={state.status} />
            </div>
            <Timeline items={state.timeline} issues={state.issues} compact />
            {state.contract?.amendment ? (
              <div className="row wrap" style={{ marginTop: 12 }}>
                <button className="btn primary" onClick={() => navigate(`contracts/${state.contract!.contract_id}`)}>
                  📜 View amendment v{state.contract.version}
                </button>
                <a className="btn" href={api.pdfUrl(state.contract.contract_id, state.contract.version)} target="_blank" rel="noreferrer">PDF</a>
              </div>
            ) : null}
          </Card>
        ) : null}
      </div>
    </div>
  );
}
