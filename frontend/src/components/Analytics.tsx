import { useState } from 'react';
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { concessionRate, gapClosure, opponentEstimates, type NegotiationState } from '../state';
import type { Role, View } from '../types';
import { Kpi, Seg } from './ui';

const C = { buyer: '#4cc2ff', supplier: '#ff9e57', open: '#2c3540', grid: '#222932', muted: '#8f98a6', ok: '#3ddc97' };
const tip = { background: '#1d232b', border: '1px solid #2c3540', borderRadius: 8, fontSize: 12 };
const tick = { fill: C.muted, fontSize: 10.5 };

/** Who closed how much of each issue's opening gap (public offers only). */
export function GapClosureChart({ state }: { state: NegotiationState }) {
  const data = gapClosure(state);
  if (!data.length) return <div className="empty small">Appears once both sides have tabled offers.</div>;
  return (
    <ResponsiveContainer width="100%" height={Math.max(150, data.length * 26 + 40)}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 10, bottom: 0, left: 8 }} barSize={13}>
        <CartesianGrid stroke={C.grid} strokeDasharray="3 3" horizontal={false} />
        <XAxis type="number" domain={[0, 100]} tick={tick} stroke={C.grid} unit="%" />
        <YAxis type="category" dataKey="issue" tick={tick} stroke={C.grid} width={140} />
        <Tooltip contentStyle={tip} formatter={(v) => `${v}%`} />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        <Bar dataKey="buyer" name="Buyer conceded" stackId="g" fill={C.buyer} isAnimationActive={false} />
        <Bar dataKey="supplier" name="Supplier conceded" stackId="g" fill={C.supplier} isAnimationActive={false} />
        <Bar dataKey="open" name="Still open" stackId="g" fill={C.open} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Own utility given up per round - the slope of each concession curve. */
export function ConcessionRateChart({ state }: { state: NegotiationState }) {
  const data = concessionRate(state);
  if (!data.length) return <div className="empty small">Needs at least two own offers (visible per role or in God view).</div>;
  return (
    <ResponsiveContainer width="100%" height={170}>
      <BarChart data={data} margin={{ top: 4, right: 10, bottom: 0, left: -14 }} barGap={2}>
        <CartesianGrid stroke={C.grid} strokeDasharray="3 3" />
        <XAxis dataKey="round" tick={tick} stroke={C.grid} />
        <YAxis tick={tick} stroke={C.grid} tickFormatter={(v: number) => v.toFixed(2)} />
        <Tooltip contentStyle={tip} formatter={(v) => (typeof v === 'number' ? v.toFixed(3) : String(v))} />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        <Bar dataKey="buyer" name="Buyer Δu" fill={C.buyer} isAnimationActive={false} />
        <Bar dataKey="supplier" name="Supplier Δu" fill={C.supplier} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Each agent's learned estimate of its opponent's priorities vs the truth (arbiter-only). */
export function OpponentModelChart({ state, view }: { state: NegotiationState; view: View }) {
  const [who, setWho] = useState<Role>('buyer');
  const truth = state.analysis?.weights;
  const estimates = opponentEstimates(state);
  if (view !== 'god' || !truth) {
    return <div className="note">Scoring an opponent model needs both parties' true priorities, which only the
      arbiter holds. Switch to <b>God view</b>.</div>;
  }
  const opponent: Role = who === 'buyer' ? 'supplier' : 'buyer';
  const est = estimates[who];
  if (!est) return <div className="empty small">Estimates appear after the opponent's second offer.</div>;
  const data = state.issues.map((s) => ({ issue: s.label, truth: Math.round((truth[opponent][s.key] ?? 0) * 100),
    estimate: Math.round((est[s.key] ?? 0) * 100) }));
  const l1 = data.reduce((acc, d) => acc + Math.abs(d.truth - d.estimate), 0) / 100;
  const accuracy = Math.max(0, 1 - l1 / 2);
  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="row between">
        <Seg value={who} onChange={setWho} options={[{ value: 'buyer', label: "Buyer's model" },
          { value: 'supplier', label: "Supplier's model" }]} />
        <span className="small mono" style={{ color: accuracy > 0.75 ? C.ok : C.muted }}>
          accuracy {(accuracy * 100).toFixed(0)}%</span>
      </div>
      <ResponsiveContainer width="100%" height={Math.max(150, data.length * 24 + 40)}>
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 10, bottom: 0, left: 8 }} barGap={1} barSize={8}>
          <CartesianGrid stroke={C.grid} strokeDasharray="3 3" horizontal={false} />
          <XAxis type="number" tick={tick} stroke={C.grid} unit="%" />
          <YAxis type="category" dataKey="issue" tick={tick} stroke={C.grid} width={140} />
          <Tooltip contentStyle={tip} formatter={(v) => `${v}%`} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <Bar dataKey="truth" name={`${opponent} true weight`} fill={opponent === 'buyer' ? C.buyer : C.supplier}
               isAnimationActive={false} />
          <Bar dataKey="estimate" name={`${who}'s estimate`} fill="#b69cff" isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Guardrail compliance at a glance: everything that reached the counterpart passed every rule. */
export function ComplianceCard({ state }: { state: NegotiationState }) {
  const committed = state.turns.length;
  const attempts = committed + state.blocked;
  const rules = state.rulesCatalogue.length;
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="kpis">
        <Kpi k="Moves reviewed" v={attempts} s={`${rules} rules each`} />
        <Kpi k="Blocked" v={state.blocked} s="replaced by compliant move" color={state.blocked ? '#ff6b6b' : undefined} />
        <Kpi k="Delivered in bounds" v={committed ? '100%' : '-'} s={`${committed} committed moves`} color={C.ok} />
      </div>
      <div className="small faint">Every move is checked for schema, law ({state.rulesCatalogue.filter((r) => !r.startsWith('POLICY')
        && !r.startsWith('SAFE') && r !== 'SCHEMA').length} jurisdiction rules), the sender's sealed mandate, budget, cost
        floor, BATNA and concession pace <i>before</i> the counterpart sees it, so a breach can never be delivered.</div>
    </div>
  );
}
