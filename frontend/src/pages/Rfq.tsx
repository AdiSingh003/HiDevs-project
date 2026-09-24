import { useMemo, useState } from 'react';
import { CartesianGrid, LabelList, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis } from 'recharts';
import { api } from '../api';
import { Sparkline } from '../components/Charts';
import { Badge, Card, Empty, ErrorBox, Label, StatusBadge, Switch } from '../components/ui';
import { fmtMoney, fmtUtility, fmtValue } from '../format';
import { useAsync, useRunStream } from '../hooks';
import { reduceEvents } from '../state';
import type { IssueSpec, PlatformStatus, StreamEvent } from '../types';

const LANE_COLORS = ['#ff9e57', '#b69cff', '#3ddc97', '#4cc2ff'];

export function Rfq({ status, runId, navigate }: { status: PlatformStatus | null; runId?: string;
  navigate: (p: string) => void }) {
  const scenarios = useAsync(() => api.scenarios(), []);
  const rfqScenarios = (scenarios.data ?? []).filter((s) => s.mode === 'rfq');
  const scenarioId = rfqScenarios[0]?.id ?? 'steel_rfq';
  const [leverage, setLeverage] = useState(true);
  const [redTeam, setRedTeam] = useState(false);
  const [speed, setSpeed] = useState(350);
  const [error, setError] = useState<string | null>(null);
  const { events, live } = useRunStream(runId ?? null, 'god');

  const rfqEvents = events.filter((e) => e.type.startsWith('rfq_'));
  const started = rfqEvents.find((e) => e.type === 'rfq_started')?.data;
  const award = rfqEvents.find((e) => e.type === 'rfq_award')?.data;
  const rounds = rfqEvents.filter((e) => e.type === 'rfq_round');
  const leverageEvents = rfqEvents.filter((e) => e.type === 'rfq_leverage');
  const contract = events.find((e) => e.type === 'contract_compiled')?.data;
  const finished = events.some((e) => e.type === 'run_finished');

  const lanes = useMemo(() => {
    const byLane = new Map<string, StreamEvent[]>();
    for (const e of events) {
      const sid = e.data?.supplier_id;
      if (sid && !e.type.startsWith('rfq_')) byLane.set(sid, [...(byLane.get(sid) ?? []), e]);
    }
    return [...byLane.entries()].map(([sid, evs]) => ({ sid, state: reduceEvents(evs) }));
  }, [events]);

  const issues: IssueSpec[] = lanes[0]?.state.issues ?? rfqScenarios[0]?.issues ?? [];
  const spec = (k: string) => issues.find((i) => i.key === k);
  const currency = (lanes[0]?.state.rfq?.currency as string) ?? 'INR';

  async function start() {
    setError(null);
    try {
      const rec = await api.startRfq({ scenario_id: scenarioId, leverage, red_team: redTeam, speed_ms: speed,
        llm_mode: 'auto' });
      navigate(`rfq/${rec.id}`);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const utilitySeries = (sid: string) => rounds.map((r) => r.data.lanes.find((l: any) => l.supplier_id === sid)?.u_buyer)
    .filter((v: number | null | undefined): v is number => typeof v === 'number');
  const candidates: any[] = award?.candidates ?? [];
  const points = candidates.filter((c) => c.terms).map((c) => ({
    x: c.total_value, y: c.terms.delivery_days, name: c.supplier_name, short: c.supplier_name.split(' ')[0],
    winner: c.supplier_id === award?.winner,
  }));

  return (
    <div className="stack" style={{ gap: 14 }}>
      <Card>
        <div className="row wrap" style={{ gap: 14 }}>
          <div className="stack" style={{ gap: 2, minWidth: 260 }}>
            <div className="h1">Multi-vendor RFQ</div>
            <div className="small muted">{rfqScenarios[0]?.title ?? 'One buyer, three suppliers, one Pareto-efficient award'}</div>
          </div>
          <span className="spacer" />
          <Switch checked={leverage} onChange={setLeverage} accent
                  label={<span>Competitive leverage <span className="faint small">(rival quotes raise the buyer's BATNA)</span></span>} />
          <Switch checked={redTeam} onChange={setRedTeam} label="Red-team" />
          <label className="field" style={{ width: 160 }}><span>Speed · {speed} ms/round</span>
            <input type="range" min={0} max={1200} step={50} value={speed} onChange={(e) => setSpeed(Number(e.target.value))} />
          </label>
          <button className="btn primary" onClick={start}>▶ Run sourcing event</button>
          {runId ? <StatusBadge status={finished ? (award?.winner ? 'awarded' : 'no_award') : 'running'} /> : null}
          {live ? <Badge tone="accent"><span className="dot pulse" /> live</Badge> : null}
        </div>
        <ErrorBox error={error} />
        <div className="small faint" style={{ marginTop: 8 }}>
          Agent brains: {status?.llm_modes.includes('lyzr') ? 'Lyzr Studio agents (bounded by the Legal Arbiter)'
            : 'deterministic policy engine (configure Lyzr keys to enable LLM agents)'} · every lane has its own Legal
          Arbiter, sealed envelopes and audit ledger.
        </div>
      </Card>

      {!runId ? (
        <Empty title="Run a competitive sourcing event">
          <div className="small">The buyer negotiates with all three mills in lockstep. After every round, the best acceptable
            rival quote becomes the buyer's live outside option (minus a switching margin) without revealing rival numbers.
            Binding quotes are filtered for Pareto efficiency and the buyer-optimal one is awarded.</div>
        </Empty>
      ) : (
        <>
          <div className="lanes">
            {(started?.lanes ?? lanes.map((l) => ({ supplier_id: l.sid, name: l.sid }))).map((lane: any, i: number) => {
              const st = lanes.find((l) => l.sid === lane.supplier_id)?.state;
              const lev = [...leverageEvents].reverse().find((e) => e.data.supplier_id === lane.supplier_id)?.data;
              const won = award?.winner === lane.supplier_id;
              return (
                <Card key={lane.supplier_id} className={`lane ${won ? 'winner' : ''}`}>
                  <div className="row between">
                    <div className="stack" style={{ gap: 1 }}>
                      <div className="h2" style={{ color: LANE_COLORS[i] }}>{lane.name}</div>
                      <div className="tiny muted">{st?.supplier?.persona ?? ''}</div>
                    </div>
                    <div className="stack" style={{ alignItems: 'flex-end', gap: 4 }}>
                      {won ? <Badge tone="ok">🏆 awarded</Badge> : null}
                      <StatusBadge status={st?.status ?? 'running'} />
                    </div>
                  </div>
                  <div className="row small muted" style={{ gap: 6 }}>
                    <span>Buyer utility of standing quote</span>
                    {lev ? <Badge tone="buyer" title="Buyer's live outside option in this lane">BATNA↑ {fmtUtility(lev.buyer_outside_option)}</Badge> : null}
                    {st?.supplier?.is_msme ? <Badge tone="warn">MSME · 45-day cap</Badge> : null}
                  </div>
                  <Sparkline values={utilitySeries(lane.supplier_id)} color={LANE_COLORS[i]} />
                  <div className="lane-turns">
                    {(st?.timeline ?? []).slice(-14).map((it) => (it.kind === 'turn' ? (
                      <div key={it.id} className={`lane-turn ${it.turn.actor}`} title={it.turn.message}>
                        <div className="row between">
                          <b className="small">R{it.turn.round} · {it.turn.actor}</b>
                          <span className="tiny mono muted">{it.turn.action}{it.turn.source !== 'engine' ? ` · ${it.turn.source}` : ''}</span>
                        </div>
                        {it.turn.offer ? (
                          <div className="tiny mono">
                            {fmtValue(spec('unit_price'), it.turn.offer.unit_price)} · {it.turn.offer.delivery_days}d ·
                            Net {it.turn.offer.payment_terms_days} · {it.turn.offer.sla_on_time_pct}%
                          </div>
                        ) : <div className="tiny muted">{it.turn.message.slice(0, 90)}</div>}
                      </div>
                    ) : (
                      <div key={it.id} className="lane-turn sys tiny"><b>{it.title}</b>{it.body ? ` · ${it.body.slice(0, 110)}` : ''}</div>
                    )))}
                  </div>
                </Card>
              );
            })}
          </div>

          {award ? (
            <div className="grid-2">
              <Card>
                <Label>Award decision</Label>
                <div className={`verify-banner ${award.winner ? 'ok' : 'bad'}`} style={{ marginBottom: 10 }}>
                  {award.winner ? `🏆 ${candidates.find((c) => c.supplier_id === award.winner)?.supplier_name}` : 'No award'}
                </div>
                <div className="small" style={{ marginBottom: 10 }}>{award.rationale}</div>
                <div style={{ overflowX: 'auto' }}>
                  <table className="table">
                    <thead><tr><th>#</th><th>Supplier</th><th className="num">Price</th><th className="num">Lead</th>
                      <th className="num">Net</th><th className="num">OTIF</th><th className="num">u buyer</th><th>Pareto</th></tr></thead>
                    <tbody>
                      {candidates.map((c) => (
                        <tr key={c.supplier_id} className={c.supplier_id === award.winner ? 'selected' : ''}>
                          <td className="mono">{c.rank ?? '-'}</td>
                          <td>{c.supplier_name}<div className="tiny muted">{c.status.replace(/_/g, ' ')}</div></td>
                          <td className="num">{c.terms ? fmtValue(spec('unit_price'), c.terms.unit_price) : '-'}</td>
                          <td className="num">{c.terms ? `${c.terms.delivery_days} d` : '-'}</td>
                          <td className="num">{c.terms ? c.terms.payment_terms_days : '-'}</td>
                          <td className="num">{c.terms ? `${c.terms.sla_on_time_pct}%` : '-'}</td>
                          <td className="num">{fmtUtility(c.u_buyer)}</td>
                          <td>{!c.terms ? <Badge tone="bad">no quote</Badge> : c.pareto_efficient
                            ? <Badge tone="ok">efficient</Badge> : <Badge tone="warn">dominated by {c.dominated_by.join(', ')}</Badge>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {contract ? (
                  <div className="row wrap" style={{ marginTop: 12 }}>
                    <button className="btn primary" onClick={() => navigate(`contracts/${contract.contract_id}`)}>📜 Winner contract {contract.contract_id}</button>
                    <a className="btn" href={api.pdfUrl(contract.contract_id)} target="_blank" rel="noreferrer">PDF</a>
                    {contract.cfo_required ? <Badge tone="warn">CFO co-signature pending</Badge> : null}
                  </div>
                ) : null}
              </Card>
              <Card className="chart-card">
                <Label hint="lower-left is better">Total cost vs lead time</Label>
                {points.length ? (
                  <ResponsiveContainer width="100%" height={280}>
                    <ScatterChart margin={{ top: 10, right: 16, bottom: 18, left: 10 }}>
                      <CartesianGrid stroke="#222932" strokeDasharray="3 3" />
                      <XAxis type="number" dataKey="x" name="Total value"
                             domain={[(lo: number) => lo * 0.985, (hi: number) => hi * 1.015]}
                             tick={{ fill: '#8f98a6', fontSize: 10.5 }} stroke="#222932"
                             tickFormatter={(v: number) => `${(v / 1e6).toFixed(1)}M`}
                             label={{ value: `total contract value (${currency})`, position: 'insideBottom', offset: -10, fill: '#8f98a6', fontSize: 10.5 }} />
                      <YAxis type="number" dataKey="y" name="Lead time (days)"
                             domain={[(lo: number) => Math.max(0, Math.floor(lo - 3)), (hi: number) => Math.ceil(hi + 3)]}
                             tick={{ fill: '#8f98a6', fontSize: 10.5 }} stroke="#222932" />
                      <ZAxis range={[160, 160]} />
                      <Tooltip contentStyle={{ background: '#1d232b', border: '1px solid #2c3540', borderRadius: 8, fontSize: 12 }}
                               formatter={(v, n) => (n === 'Total value' ? fmtMoney(Number(v), currency) : String(v))} />
                      <Scatter name="Quotes" data={points.filter((p) => !p.winner)} fill="#8f98a6">
                        <LabelList dataKey="short" position="top" fill="#8f98a6" fontSize={11} />
                      </Scatter>
                      <Scatter name="Winner" data={points.filter((p) => p.winner)} fill="#3ddc97" shape="star">
                        <LabelList dataKey="short" position="top" fill="#3ddc97" fontSize={11} />
                      </Scatter>
                    </ScatterChart>
                  </ResponsiveContainer>
                ) : <Empty title="No binding quotes" />}
                <div className="small faint">Pareto filtering uses all seven terms (buyer-side dominance); the award then
                  maximises the buyer's multi-attribute utility.</div>
              </Card>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
