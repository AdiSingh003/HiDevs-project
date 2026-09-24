import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import { ComplianceCard, ConcessionRateChart, GapClosureChart, OpponentModelChart } from '../components/Analytics';
import { BidChart, ConcessionChart, DanceChart } from '../components/Charts';
import { EnvelopeEditor, type EnvelopePatch } from '../components/EnvelopeEditor';
import { Timeline } from '../components/Timeline';
import { Badge, Card, Empty, ErrorBox, Kpi, Label, Seg, StatusBadge, Switch } from '../components/ui';
import { fmtMoney, fmtUtility, fmtValue } from '../format';
import { useAsync, useRunStream } from '../hooks';
import { reduceEvents } from '../state';
import type { PlatformStatus, View } from '../types';

function clean(p: EnvelopePatch): EnvelopePatch | undefined {
  const out: EnvelopePatch = {};
  if (p.mandate && Object.keys(p.mandate).length) out.mandate = p.mandate;
  if (p.strategy && Object.keys(p.strategy).length) out.strategy = p.strategy;
  for (const k of ['budget_cap', 'unit_cost', 'min_margin_pct'] as const) if (p[k] !== undefined) out[k] = p[k];
  return Object.keys(out).length ? out : undefined;
}

const VIEWS: { value: View; label: string; title: string }[] = [
  { value: 'god', label: 'God', title: 'Arbiter view: everything, incl. joint analytics' },
  { value: 'buyer', label: 'Buyer', title: 'What the buyer agent can see' },
  { value: 'supplier', label: 'Supplier', title: 'What the supplier agent can see' },
  { value: 'public', label: 'Public', title: 'Only public events' },
];

export function Arena({ status, runId, navigate }: { status: PlatformStatus | null; runId?: string;
  navigate: (p: string) => void }) {
  const scenarios = useAsync(() => api.scenarios(), []);
  const bilateral = (scenarios.data ?? []).filter((s) => s.mode === 'bilateral');
  const [selected, setSelected] = useState('semiconductor_spot_po');
  const scenario = useAsync(() => api.scenario(selected), [selected]);
  const [patches, setPatches] = useState<{ buyer: EnvelopePatch; supplier: EnvelopePatch }>({ buyer: {}, supplier: {} });
  const [llmMode, setLlmMode] = useState<'auto' | 'offline' | 'lyzr'>('auto');
  const [redTeam, setRedTeam] = useState(false);
  const [speed, setSpeed] = useState(450);
  const [view, setView] = useState<View>('god');
  const [bidIssue, setBidIssue] = useState('unit_price');
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setPatches({ buyer: {}, supplier: {} }), [selected]);

  const { events, live } = useRunStream(runId ?? null, view);
  const [cursor, setCursor] = useState<number | null>(null); // replay position (null = latest)
  const [playing, setPlaying] = useState(false);
  const [analyticsTab, setAnalyticsTab] = useState<'gap' | 'rate'>('gap');
  useEffect(() => { setCursor(null); setPlaying(false); }, [runId, view]);
  useEffect(() => {
    if (!playing) return;
    const id = window.setInterval(() => setCursor((c) => {
      const next = (c ?? 0) + 1;
      if (next >= events.length) { setPlaying(false); return null; }
      return next;
    }), 180);
    return () => window.clearInterval(id);
  }, [playing, events.length]);
  const state = useMemo(() => reduceEvents(cursor === null ? events : events.slice(0, cursor)), [events, cursor]);
  const lyzrReady = status?.llm_modes.includes('lyzr') ?? false;

  async function start() {
    setStarting(true);
    setError(null);
    try {
      const rec = await api.startNegotiation({
        scenario_id: selected, llm_mode: llmMode, red_team: redTeam, speed_ms: speed,
        buyer: clean(patches.buyer), supplier: clean(patches.supplier),
      });
      navigate(`arena/${rec.id}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStarting(false);
    }
  }

  const sc = scenario.data;
  const issues = state.issues.length ? state.issues : sc?.issues ?? [];
  const currency = (state.rfq?.currency as string) ?? sc?.context.currency ?? 'USD';
  const quantity = (state.rfq?.quantity as number) ?? sc?.context.quantity ?? 0;
  const progress = state.maxRounds ? Math.min(100, (state.round / state.maxRounds) * 100) : 0;
  const lastGap = state.gapHistory.length ? state.gapHistory[state.gapHistory.length - 1] : undefined;
  const aa = state.agreementAnalysis;
  const priceSpec = issues.find((i) => i.key === 'unit_price');

  return (
    <div className="arena">
      {/* ------------------------------------------------------------ setup */}
      <div className="col">
        <Card>
          <Label hint={`${bilateral.length} presets`}>01 · Scenario</Label>
          <div className="stack">
            {bilateral.map((s) => (
              <div key={s.id} className={`scenario-card ${selected === s.id ? 'on' : ''}`} onClick={() => setSelected(s.id)}>
                <div className="t">{s.title}</div>
                <div className="d">{s.summary.slice(0, 118)}…</div>
                <div className="row wrap" style={{ marginTop: 6, gap: 4 }}>
                  {s.tags.map((t) => <Badge key={t}>{t}</Badge>)}
                </div>
              </div>
            ))}
            <ErrorBox error={scenarios.error} />
          </div>
        </Card>
        {sc ? (
          <Card>
            <Label hint="private to each party">02 · Policy setup</Label>
            <EnvelopeEditor key={`b-${sc.id}`} role="buyer" org={sc.context.buyer.name} envelope={sc.buyer}
                            issues={sc.issues} currency={sc.context.currency} patch={patches.buyer}
                            onPatch={(p) => setPatches((x) => ({ ...x, buyer: p }))} />
            <EnvelopeEditor key={`s-${sc.id}`} role="supplier" org={sc.suppliers[0].party.name}
                            envelope={sc.suppliers[0].envelope} issues={sc.issues} currency={sc.context.currency}
                            patch={patches.supplier} onPatch={(p) => setPatches((x) => ({ ...x, supplier: p }))} />
            <div className="small faint" style={{ marginTop: 8 }}>The Legal Arbiter validates every envelope against the
              {' '}{sc.context.jurisdiction} rulebook at setup and seals it with a hash commitment.</div>
          </Card>
        ) : null}
        <Card>
          <Label>03 · Run</Label>
          <div className="stack" style={{ gap: 12 }}>
            <div className="field"><span>Agent brains</span>
              <Seg value={llmMode} onChange={setLlmMode} options={[
                { value: 'auto', label: 'Auto' },
                { value: 'offline', label: 'Policy engine' },
                { value: 'lyzr', label: 'Lyzr LLM', disabled: !lyzrReady,
                  title: lyzrReady ? 'Lyzr Studio agents' : 'Set LYZR_API_KEY and agent IDs to enable' },
              ]} />
            </div>
            <Switch checked={redTeam} onChange={setRedTeam}
                    label={<span>Red-team mode <span className="faint small">(rogue LLM attacks)</span></span>} />
            <div className="field"><span>Playback speed · {speed} ms/move</span>
              <input type="range" min={0} max={1500} step={50} value={speed} onChange={(e) => setSpeed(Number(e.target.value))} />
            </div>
            <button className="btn primary block" disabled={starting || !sc} onClick={start}>
              {starting ? 'Starting…' : '▶  Start negotiation'}
            </button>
            <ErrorBox error={error} />
          </div>
        </Card>
      </div>

      {/* ------------------------------------------------------------ stage */}
      <div className="col">
        <Card>
          <div className="stage-head">
            <div className="stack" style={{ gap: 2 }}>
              <div className="h2">{state.title ?? sc?.title ?? 'Negotiation arena'}</div>
              <div className="small muted mono">{state.negotiationId ?? 'no run yet'}{state.rfq ? ` · ${state.rfq.reference}` : ''}</div>
            </div>
            <span className="spacer" />
            <Seg value={view} onChange={setView} options={VIEWS} />
            <StatusBadge status={runId ? state.status : 'idle'} />
          </div>
          {runId ? (
            <>
              <div className="row" style={{ marginBottom: 12, gap: 10 }}>
                <span className="small mono muted">Round {state.round}/{state.maxRounds || '-'}</span>
                <div className="progress"><div style={{ width: `${progress}%` }} /></div>
                {live ? <Badge tone="accent"><span className="dot pulse" /> live</Badge> : <Badge>recorded</Badge>}
              </div>
              {!live && events.length > 0 ? (
                <div className="row" style={{ marginBottom: 12, gap: 8 }}>
                  <button className="btn sm" onClick={() => { setCursor(1); setPlaying(true); }} title="Replay from the start">⏮ Replay</button>
                  <button className="btn sm" disabled={cursor === null} onClick={() => setPlaying(!playing)}>{playing ? '⏸ Pause' : '▶ Play'}</button>
                  <input type="range" style={{ flex: 1 }} min={1} max={events.length} value={cursor ?? events.length}
                         onChange={(e) => { setPlaying(false); const v = Number(e.target.value); setCursor(v >= events.length ? null : v); }} />
                  <span className="small mono muted" style={{ minWidth: 92, textAlign: 'right' }}>
                    {cursor === null ? `all ${events.length} events` : `event ${cursor}/${events.length}`}</span>
                </div>
              ) : null}
              <div className="kpis" style={{ marginBottom: 12 }}>
                <Kpi k="Gap" v={lastGap !== undefined ? `${(lastGap * 100).toFixed(1)}%` : view === 'god' ? '-' : '🔒'}
                     s="distance between offers" />
                <Kpi k="Blocked" v={state.blocked} s="guardrail blocks" color={state.blocked ? '#ff6b6b' : undefined} />
                <Kpi k="Sanitised" v={state.redacted} s="messages redacted" color={state.redacted ? '#b69cff' : undefined} />
                <Kpi k="Brains" v={state.llmMode === 'lyzr' ? 'Lyzr' : 'Engine'} s={state.redTeam ? 'red-team ON' : 'bounded agents'} />
              </div>
              <Timeline items={state.timeline} issues={issues} />
            </>
          ) : (
            <Empty title="Configure the sealed envelopes and start a negotiation">
              <div className="small" style={{ maxWidth: 520, margin: '0 auto' }}>Bounded buyer and supplier agents exchange
                offers under the Legal Arbiter. Each move is checked against the law, the CFO mandate and the
                authorised concession pace before the counterpart sees it.</div>
            </Empty>
          )}
        </Card>

        {state.finished && (state.agreement || state.status === 'no_deal') ? (
          <Card>
            <Label hint={state.reason}>Outcome</Label>
            {state.agreement ? (
              <>
                <div className="kpis" style={{ marginBottom: 12 }}>
                  {priceSpec ? <Kpi k="Contract value" v={fmtMoney((state.agreement.terms.unit_price ?? 0) * quantity, currency)}
                                    s={`${fmtValue(priceSpec, state.agreement.terms.unit_price)} × ${quantity.toLocaleString()}`} /> : null}
                  {aa ? <Kpi k="u buyer / supplier" v={`${fmtUtility(aa.u_buyer)} / ${fmtUtility(aa.u_supplier)}`} s="MAUT utilities" /> : null}
                  {aa ? <Kpi k="Pareto gap" v={fmtUtility(aa.pareto_gap)} s="0 = on the frontier" color="#3ddc97" /> : null}
                  {aa?.joint_gain_vs_split != null ? <Kpi k="Value created" v={`+${aa.joint_gain_vs_split.toFixed(3)}`}
                                                           s="joint utility vs split-the-difference" color="#f5a524" /> : null}
                </div>
                <table className="table">
                  <thead><tr><th>Term</th><th className="num">Agreed</th></tr></thead>
                  <tbody>
                    {issues.map((s) => (
                      <tr key={s.key}><td>{s.label}</td><td className="num">{fmtValue(s, state.agreement!.terms[s.key])}</td></tr>
                    ))}
                  </tbody>
                </table>
                {state.contract ? (
                  <div className="row wrap" style={{ marginTop: 12 }}>
                    <button className="btn primary" onClick={() => navigate(`contracts/${state.contract!.contract_id}`)}>
                      📜 Open contract {state.contract.contract_id}
                    </button>
                    <a className="btn" href={api.pdfUrl(state.contract.contract_id)} target="_blank" rel="noreferrer">PDF</a>
                    <button className="btn" onClick={() => navigate(`telemetry/${state.contract!.contract_id}`)}>⚡ Send telemetry event</button>
                    {state.contract.cfo_required ? <Badge tone="warn">CFO co-signature pending</Badge> : <Badge tone="ok">executed</Badge>}
                  </div>
                ) : <div className="small muted" style={{ marginTop: 10 }}>Compiling contract via Lyzr Automata…</div>}
              </>
            ) : (
              <div className="error-box">{state.reason}</div>
            )}
          </Card>
        ) : null}
      </div>

      {/* ------------------------------------------------------------ charts */}
      <div className="col charts">
        <div className="charts-grid">
          <Card className="chart-card">
            <Label hint="dashed = BATNA">Concession curves</Label>
            <ConcessionChart state={state} view={view} />
          </Card>
          <Card className="chart-card">
            <Label hint={
              <select className="select" style={{ width: 150, padding: '3px 6px', fontSize: 11.5 }} value={bidIssue}
                      onChange={(e) => setBidIssue(e.target.value)}>
                {issues.map((i) => <option key={i.key} value={i.key}>{i.label}</option>)}
              </select>}>Bid curves</Label>
            <BidChart state={state} issue={issues.find((i) => i.key === bidIssue) ?? issues[0]} />
          </Card>
          <Card className="chart-card">
            <Label hint="utility space">Negotiation dance</Label>
            <DanceChart state={state} view={view} />
            {view === 'god' && state.analysis ? (
              <div className="small faint" style={{ marginTop: 4 }}>
                ZOPA {state.analysis.zopa_exists ? 'exists' : 'does not exist'} · BATNA floors
                b={fmtUtility(state.analysis.reservations.buyer)} s={fmtUtility(state.analysis.reservations.supplier)}
                {state.analysis.nash ? ` · Nash (${fmtUtility(state.analysis.nash.u_buyer)}, ${fmtUtility(state.analysis.nash.u_supplier)})` : ''}
              </div>
            ) : null}
          </Card>
          <Card className="chart-card">
            <Label hint={<Seg value={analyticsTab} onChange={setAnalyticsTab} options={[
              { value: 'gap', label: 'Gap closure' }, { value: 'rate', label: 'Per round' }]} />}>Concession analytics</Label>
            {analyticsTab === 'gap' ? <GapClosureChart state={state} /> : <ConcessionRateChart state={state} />}
          </Card>
          <Card className="chart-card">
            <Label hint="learned vs true priorities">Opponent model</Label>
            <OpponentModelChart state={state} view={view} />
          </Card>
          <Card>
            <Label>Guardrail compliance</Label>
            <ComplianceCard state={state} />
          </Card>
        </div>
      </div>
    </div>
  );
}
