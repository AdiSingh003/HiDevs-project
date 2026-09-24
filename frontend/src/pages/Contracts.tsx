import { useEffect, useState } from 'react';
import { api } from '../api';
import { Badge, Card, Empty, ErrorBox, Json, Kpi, Label, StatusBadge } from '../components/ui';
import { fmtDateTime, fmtMoney, fmtValue, pretty, shortHash } from '../format';
import { useAsync } from '../hooks';
import type { Contract, Verification } from '../types';

type Tab = 'overview' | 'clauses' | 'sla' | 'integrity' | 'json';

function issueSpec(c: Contract, key: string) {
  const i = c.issues.find((x) => x.key === key);
  return i ? { unit: i.unit, decimals: i.decimals } : { unit: '', decimals: 2 };
}

export function Contracts({ contractId, navigate }: { contractId?: string; navigate: (p: string) => void }) {
  const list = useAsync(() => api.contracts(), []);
  const selected = contractId ?? list.data?.[0]?.contract_id;
  return (
    <div className="split">
      <Card>
        <Label hint={`${list.data?.length ?? 0} contracts`}>Contract register</Label>
        <ErrorBox error={list.error} />
        {list.data?.length === 0 ? <Empty title="No contracts yet">Run a negotiation in the Arena first.</Empty> : null}
        {(list.data ?? []).map((c) => (
          <div key={c.contract_id} className={`list-item ${selected === c.contract_id ? 'on' : ''}`}
               onClick={() => navigate(`contracts/${c.contract_id}`)}>
            <div className="row between"><b className="mono small">{c.contract_id}</b><StatusBadge status={c.status} /></div>
            <div className="small" style={{ marginTop: 4 }}>{c.supplier}</div>
            <div className="row between tiny muted" style={{ marginTop: 3 }}>
              <span>{fmtMoney(c.total_value, c.currency)}</span>
              <span>v{c.version}{c.versions > 1 ? ` · ${c.versions} versions` : ''}</span>
            </div>
          </div>
        ))}
      </Card>
      {selected ? <ContractDetail key={selected} id={selected} navigate={navigate} onChanged={list.reload} /> : <div />}
    </div>
  );
}

function ContractDetail({ id, navigate, onChanged }: { id: string; navigate: (p: string) => void; onChanged: () => void }) {
  const [version, setVersion] = useState<number | undefined>(undefined);
  const contract = useAsync(() => api.contract(id, version), [id, version]);
  const versions = useAsync(() => api.versions(id), [id]);
  const [tab, setTab] = useState<Tab>('overview');
  const c = contract.data;
  if (contract.error) return <ErrorBox error={contract.error} />;
  if (!c) return <Card><div className="muted">Loading…</div></Card>;
  const currency = c.commercial_terms.currency;
  const latest = versions.data ? Math.max(...versions.data.map((v) => v.version)) : c.version;

  return (
    <div className="stack" style={{ gap: 12 }}>
      <Card>
        <div className="row wrap" style={{ gap: 10 }}>
          <div className="stack" style={{ gap: 2 }}>
            <div className="h1">{c.title}</div>
            <div className="small muted mono">{c.contract_id} · v{c.version} · effective {c.effective_date} · {c.rfq.reference}</div>
          </div>
          <span className="spacer" />
          {versions.data && versions.data.length > 1 ? (
            <select className="select" style={{ width: 150 }} value={version ?? latest}
                    onChange={(e) => setVersion(Number(e.target.value))}>
              {versions.data.map((v) => <option key={v.version} value={v.version}>Version {v.version}{v.version === latest ? ' (latest)' : ''}</option>)}
            </select>
          ) : null}
          <StatusBadge status={c.status} />
          <a className="btn" href={api.pdfUrl(c.contract_id, c.version)} target="_blank" rel="noreferrer">⬇ PDF</a>
        </div>
        <div className="kpis" style={{ marginTop: 12 }}>
          <Kpi k="Contract value" v={fmtMoney(c.commercial_terms.total_value, currency)} s={`${c.commercial_terms.quantity.toLocaleString()} ${c.commercial_terms.quantity_unit}`} />
          <Kpi k="Unit price" v={fmtValue(issueSpec(c, 'unit_price'), c.terms.unit_price)} s={c.commercial_terms.incoterm} />
          <Kpi k="Delivery due" v={c.delivery.delivery_due_date} s={`${c.delivery.lead_time_days} days lead time`} />
          <Kpi k="LD cap" v={c.liquidated_damages.cap_amount ? fmtMoney(c.liquidated_damages.cap_amount, currency) : '-'} s={`${c.liquidated_damages.cap_pct ?? '-'}% of value`} />
        </div>
      </Card>
      <Card>
        <div className="tabs">
          {(['overview', 'clauses', 'sla', 'integrity', 'json'] as Tab[]).map((t) => (
            <button key={t} className={tab === t ? 'on' : ''} onClick={() => setTab(t)}>
              {{ overview: 'Overview', clauses: `Clauses (${c.clauses.length})`, sla: 'Executable SLA', integrity: 'Integrity & signatures', json: 'JSON' }[t]}
            </button>
          ))}
        </div>
        {tab === 'overview' ? <Overview c={c} versions={versions.data ?? []} navigate={navigate}
                                        onApproved={() => { contract.reload(); onChanged(); }} /> : null}
        {tab === 'clauses' ? <Clauses c={c} /> : null}
        {tab === 'sla' ? <SlaEngine c={c} /> : null}
        {tab === 'integrity' ? <Integrity c={c} /> : null}
        {tab === 'json' ? <Json value={c} /> : null}
      </Card>
    </div>
  );
}

function Overview({ c, versions, navigate, onApproved }: { c: Contract; versions: any[]; navigate: (p: string) => void;
  onApproved: () => void }) {
  const [approver, setApprover] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const neg = c.negotiation;
  const labels = Object.fromEntries(c.issues.map((i) => [i.key, i.label]));
  async function approve() {
    setBusy(true);
    setErr(null);
    try {
      await api.approve(c.contract_id, approver || 'Chief Financial Officer');
      onApproved();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="stack" style={{ gap: 14 }}>
      {c.status === 'pending_cfo_approval' ? (
        <div className="note" style={{ borderColor: 'rgba(255,209,102,0.4)' }}>
          <div className="row wrap" style={{ gap: 8 }}>
            <b style={{ color: '#ffd166' }}>CFO co-signature required</b>
            <span className="small">the contract value exceeds the negotiating agent's delegated authority.</span>
            <span className="spacer" />
            <input className="input" style={{ width: 200 }} placeholder="Approver name" value={approver} onChange={(e) => setApprover(e.target.value)} />
            <button className="btn primary sm" disabled={busy} onClick={approve}>✍ Approve & co-sign</button>
          </div>
          <ErrorBox error={err} />
        </div>
      ) : null}
      <div className="grid-2">
        <div>
          <Label>Parties</Label>
          <table className="table"><tbody>
            <tr><td className="muted">Buyer</td><td>{c.parties.buyer.name}<div className="tiny muted">{c.parties.buyer.signatory_name}, {c.parties.buyer.signatory_title}</div></td></tr>
            <tr><td className="muted">Supplier</td><td>{c.parties.supplier.name} {c.parties.supplier.is_msme ? <Badge tone="warn">MSME</Badge> : null}<div className="tiny muted">{c.parties.supplier.signatory_name}, {c.parties.supplier.signatory_title}</div></td></tr>
            <tr><td className="muted">Law</td><td className="small">{c.legal.governing_law} · {c.legal.dispute_resolution}</td></tr>
          </tbody></table>
        </div>
        <div>
          <Label>Agreed terms</Label>
          <table className="table"><tbody>
            {Object.entries(c.terms).map(([k, v]) => {
              const change = c.amendment?.changed_terms?.[k];
              return (
                <tr key={k}><td className="muted">{labels[k] ?? k}</td>
                  <td className="num">{change ? <span className="faint" style={{ textDecoration: 'line-through', marginRight: 6 }}>{fmtValue(issueSpec(c, k), change.from)}</span> : null}
                    {fmtValue(issueSpec(c, k), v)}</td></tr>
              );
            })}
            {Object.entries(c.standard_terms ?? {}).map(([k, v]) => (
              <tr key={k}><td className="muted">{pretty(k)} <Badge>standard T&C</Badge></td><td className="num">{v}</td></tr>
            ))}
          </tbody></table>
        </div>
      </div>
      {c.amendment ? (
        <div className="note">
          <b>Amendment v{c.version}</b> triggered by <b>{pretty(String(c.amendment.event.event_type))}</b> from {c.amendment.event.source}
          {' '}({c.amendment.event.description || c.amendment.event.region}). {c.amendment.assessment.reason}
          {c.amendment.ld_waiver ? <div className="small" style={{ marginTop: 4 }}>{c.amendment.ld_waiver}</div> : null}
          <div className="hash" style={{ marginTop: 4 }}>amends parent sha256 {c.parent_hash}</div>
        </div>
      ) : null}
      <div className="grid-2">
        <div>
          <Label>Negotiation record</Label>
          <table className="table"><tbody>
            <tr><td className="muted">Negotiation</td><td className="mono small">{neg.negotiation_id} · {pretty(String(neg.outcome))} in {neg.rounds} rounds</td></tr>
            <tr><td className="muted">Guardrails</td><td className="small">{neg.interventions} interventions · {neg.blocked_moves} blocked · {neg.redactions} redactions</td></tr>
            <tr><td className="muted">Brains</td><td className="small">{neg.llm_mode === 'lyzr' ? 'Lyzr Studio agents' : 'policy engine'}</td></tr>
            <tr><td className="muted">Audit head</td><td><a className="hash" href={`#/audit/${neg.negotiation_id}`}>{shortHash(neg.audit_head, 24)}</a></td></tr>
          </tbody></table>
          <div className="row wrap" style={{ marginTop: 10 }}>
            <button className="btn sm" onClick={() => navigate(`telemetry/${c.contract_id}`)}>⚡ Send telemetry event</button>
            <button className="btn sm" onClick={() => navigate(`audit/${neg.negotiation_id}`)}>🔗 Audit trail</button>
            {String(neg.negotiation_id).startsWith('NEG-') ? <button className="btn sm" onClick={() => navigate(`arena/${neg.negotiation_id}`)}>▶ Replay negotiation</button> : null}
          </div>
        </div>
        <div>
          <Label>Drafting (Lyzr Automata)</Label>
          <div className="small"><Badge tone="accent">{c.drafting.engine}</Badge> <Badge>{c.drafting.model}</Badge>{' '}
            {c.drafting.review.approved ? <Badge tone="ok">legal review passed</Badge> : <Badge tone="warn">review findings</Badge>}
            {c.drafting.substitutions.length ? <Badge tone="arbiter">{c.drafting.substitutions.length} clauses replaced</Badge> : null}</div>
          <div className="small muted" style={{ marginTop: 8 }}>{c.drafting.executive_summary}</div>
        </div>
      </div>
      {versions.length > 1 ? (
        <div>
          <Label>Version chain</Label>
          <table className="table">
            <thead><tr><th>v</th><th>Created</th><th>Changes</th><th>Hash</th><th>Parent</th></tr></thead>
            <tbody>{versions.map((v) => (
              <tr key={v.version}><td className="mono">{v.version}</td><td className="small">{fmtDateTime(v.created_at)}</td>
                <td className="small">{v.amendment ? Object.entries(v.amendment.changed_terms).map(([k, ch]: any) => `${labels[k] ?? k}: ${ch.from} → ${ch.to}`).join('; ') : 'original'}</td>
                <td className="hash">{shortHash(v.content_hash, 12)}</td><td className="hash">{shortHash(v.parent_hash, 12)}</td></tr>
            ))}</tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

function Clauses({ c }: { c: Contract }) {
  return (
    <div>
      {c.clauses.map((cl, i) => (
        <div key={cl.id} className="clause">
          <div className="ct"><span className="mono faint">{i + 1}.</span>{cl.title}
            {cl.source ? <Badge tone={cl.source === 'lyzr-agent' ? 'accent' : ''}>{cl.source}</Badge> : null}</div>
          <div className="cx">{cl.text}</div>
        </div>
      ))}
    </div>
  );
}

function SlaEngine({ c }: { c: Contract }) {
  const [daysLate, setDaysLate] = useState(5);
  const [shipment, setShipment] = useState(c.commercial_terms.total_value);
  const [fm, setFm] = useState(false);
  const [otif, setOtif] = useState(95);
  const [result, setResult] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const currency = c.commercial_terms.currency;
  async function run() {
    setErr(null);
    try {
      setResult(await api.sla(c.contract_id, { days_late: daysLate, shipment_value: shipment, force_majeure: fm,
        monthly_otif_pct: otif, monthly_invoice_value: shipment }));
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  useEffect(() => { run(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);
  return (
    <div className="stack" style={{ gap: 12 }}>
      <div className="small muted">The contract carries machine-executable SLA rules (Schedule A). Evaluate delivery events against them:</div>
      <table className="table"><thead><tr><th>Rule</th><th>Trigger</th><th>Formula</th></tr></thead><tbody>
        {c.executable.sla_rules.map((r) => <tr key={r.id}><td className="mono">{r.id}</td><td className="mono small">{r.trigger}</td><td className="mono small">{r.formula}</td></tr>)}
      </tbody></table>
      <div className="grid-3">
        <label className="field"><span>Days late</span><input className="input" type="number" min={0} value={daysLate} onChange={(e) => setDaysLate(Number(e.target.value))} /></label>
        <label className="field"><span>Shipment value ({currency})</span><input className="input" type="number" min={1} value={shipment} onChange={(e) => setShipment(Number(e.target.value))} /></label>
        <label className="field"><span>Monthly OTIF %</span><input className="input" type="number" min={0} max={100} step={0.1} value={otif} onChange={(e) => setOtif(Number(e.target.value))} /></label>
      </div>
      <div className="row"><label className="row small"><input type="checkbox" checked={fm} onChange={(e) => setFm(e.target.checked)} /> Delay caused by a force-majeure event</label>
        <span className="spacer" /><button className="btn primary sm" onClick={run}>Evaluate</button></div>
      <ErrorBox error={err} />
      {result ? (
        <div className="grid-2">
          <div className="kpi"><div className="k">LD-DELAY</div><div className="v">{fmtMoney(result.delay.amount, currency)}</div>
            <div className="s">{result.delay.explanation}{result.delay.excused ? ' · excused' : ''}</div></div>
          {result.otif ? <div className="kpi"><div className="k">OTIF-CREDIT</div><div className="v">{fmtMoney(result.otif.credit, currency)}</div>
            <div className="s">{result.otif.explanation}</div></div> : null}
        </div>
      ) : null}
    </div>
  );
}

function Integrity({ c }: { c: Contract }) {
  const [report, setReport] = useState<Verification | null>(null);
  const [tamperKey, setTamperKey] = useState(Object.keys(c.terms)[0]);
  const [tamperValue, setTamperValue] = useState<number>(c.terms[Object.keys(c.terms)[0]]);
  const [tamperReport, setTamperReport] = useState<Verification | null>(null);
  useEffect(() => { api.verify(c.contract_id, c.version).then(setReport); }, [c.contract_id, c.version, c.status]);
  async function tamper() {
    const doc = JSON.parse(JSON.stringify(c));
    doc.terms[tamperKey] = tamperValue;
    setTamperReport(await api.verifyDocument(doc));
  }
  return (
    <div className="stack" style={{ gap: 12 }}>
      {report ? (
        <div className={`verify-banner ${report.valid ? 'ok' : 'bad'}`}>
          {report.valid ? '✔ Authentic: content hash and every Ed25519 signature verify' : '✖ Verification failed'}
          {report.missing_signatures.length ? <Badge tone="warn">awaiting {report.missing_signatures.join(', ')}</Badge> : null}
        </div>
      ) : null}
      <div className="hash">sha256 {c.integrity.content_hash}</div>
      <table className="table">
        <thead><tr><th>Role</th><th>Signatory</th><th>Key fingerprint</th><th>Signed</th><th>Valid</th></tr></thead>
        <tbody>{c.integrity.signatures.map((s) => (
          <tr key={s.role}><td>{pretty(s.role)}</td><td className="small">{s.name}<div className="tiny muted">{s.title}</div></td>
            <td className="mono small">{s.fingerprint}</td><td className="small">{fmtDateTime(s.signed_at)}</td>
            <td>{report?.signatures.find((r) => r.role === s.role)?.valid ? <Badge tone="ok">✔ Ed25519</Badge> : <Badge>…</Badge>}</td></tr>
        ))}</tbody>
      </table>
      <div className="note">
        <b>Tamper test</b> - change one agreed term in a copy of this contract and re-verify it:
        <div className="row wrap" style={{ marginTop: 8 }}>
          <select className="select" style={{ width: 200 }} value={tamperKey}
                  onChange={(e) => { setTamperKey(e.target.value); setTamperValue(c.terms[e.target.value]); }}>
            {Object.keys(c.terms).map((k) => <option key={k} value={k}>{c.issues.find((i) => i.key === k)?.label ?? k}</option>)}
          </select>
          <input className="input" style={{ width: 120 }} type="number" value={tamperValue} onChange={(e) => setTamperValue(Number(e.target.value))} />
          <button className="btn sm danger" onClick={tamper}>Forge & verify</button>
        </div>
        {tamperReport ? (
          <div className={`verify-banner ${tamperReport.valid ? 'ok' : 'bad'}`} style={{ marginTop: 10 }}>
            {tamperReport.valid ? '✔ Still valid (value unchanged)' : `✖ Forgery detected: hash ${tamperReport.hash_valid ? 'ok' : 'mismatch'} · computed ${shortHash(tamperReport.computed_hash, 14)} ≠ recorded ${shortHash(tamperReport.recorded_hash, 14)}`}
          </div>
        ) : null}
      </div>
    </div>
  );
}
