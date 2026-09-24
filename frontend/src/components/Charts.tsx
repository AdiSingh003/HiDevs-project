import {
  CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis,
  YAxis,
} from 'recharts';
import { fmtValue } from '../format';
import { bidSeries, concessionSeries, dancePoints, type NegotiationState } from '../state';
import type { IssueSpec, View } from '../types';

const C = { buyer: '#4cc2ff', supplier: '#ff9e57', arbiter: '#b69cff', ok: '#3ddc97', grid: '#222932', muted: '#8f98a6',
  accent: '#f5a524' };
const tooltipStyle = { background: '#1d232b', border: '1px solid #2c3540', borderRadius: 8, fontSize: 12 };
const axisTick = { fill: C.muted, fontSize: 10.5 };

export function ConcessionChart({ state, view }: { state: NegotiationState; view: View }) {
  const data = concessionSeries(state);
  const res = view === 'god' ? state.analysis?.reservations : {
    buyer: state.policy.buyer?.reservation_utility, supplier: state.policy.supplier?.reservation_utility };
  if (!data.length) return <div className="empty small">Concession curves appear once offers are exchanged.</div>;
  return (
    <ResponsiveContainer width="100%" height={210}>
      <LineChart data={data} margin={{ top: 8, right: 10, bottom: 0, left: -18 }}>
        <CartesianGrid stroke={C.grid} strokeDasharray="3 3" />
        <XAxis dataKey="round" tick={axisTick} stroke={C.grid} />
        <YAxis domain={[0, 1]} tick={axisTick} stroke={C.grid} />
        <Tooltip contentStyle={tooltipStyle} formatter={(v) => (typeof v === 'number' ? v.toFixed(3) : String(v))} />
        <Legend wrapperStyle={{ fontSize: 11.5 }} />
        {res?.buyer != null ? <ReferenceLine y={res.buyer} stroke={C.buyer} strokeDasharray="4 4" strokeOpacity={0.6} /> : null}
        {res?.supplier != null ? <ReferenceLine y={res.supplier} stroke={C.supplier} strokeDasharray="4 4" strokeOpacity={0.6} /> : null}
        <Line name="Buyer (own utility)" type="monotone" dataKey="buyer" stroke={C.buyer} strokeWidth={2} dot={{ r: 2.5 }} connectNulls isAnimationActive={false} />
        <Line name="Supplier (own utility)" type="monotone" dataKey="supplier" stroke={C.supplier} strokeWidth={2} dot={{ r: 2.5 }} connectNulls isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function BidChart({ state, issue }: { state: NegotiationState; issue: IssueSpec | undefined }) {
  if (!issue) return null;
  const data = bidSeries(state, issue.key);
  if (!data.length) return <div className="empty small">Bid curves appear once offers are exchanged.</div>;
  const agreed = state.agreement?.terms?.[issue.key];
  return (
    <ResponsiveContainer width="100%" height={200}>
      <LineChart data={data} margin={{ top: 8, right: 10, bottom: 0, left: 0 }}>
        <CartesianGrid stroke={C.grid} strokeDasharray="3 3" />
        <XAxis dataKey="round" tick={axisTick} stroke={C.grid} />
        <YAxis domain={['auto', 'auto']} tick={axisTick} stroke={C.grid} width={62}
               tickFormatter={(v: number) => v.toLocaleString('en-US', { maximumFractionDigits: issue.decimals })} />
        <Tooltip contentStyle={tooltipStyle} formatter={(v) => (typeof v === 'number' ? fmtValue(issue, v) : String(v))} />
        <Legend wrapperStyle={{ fontSize: 11.5 }} />
        {agreed != null ? <ReferenceLine y={agreed} stroke={C.ok} strokeDasharray="5 3" label={{ value: 'agreed', fill: C.ok, fontSize: 10, position: 'insideTopRight' }} /> : null}
        <Line name="Buyer bid" type="stepAfter" dataKey="buyer" stroke={C.buyer} strokeWidth={2} dot={{ r: 2.5 }} connectNulls isAnimationActive={false} />
        <Line name="Supplier ask" type="stepAfter" dataKey="supplier" stroke={C.supplier} strokeWidth={2} dot={{ r: 2.5 }} connectNulls isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

const dot = (color: string, r = 3.5) => (p: any) => (
  <circle cx={p.cx} cy={p.cy} r={r} fill={color} stroke="#0a0c0f" strokeWidth={1} />
);
const hidden = () => <g />;

export function DanceChart({ state, view }: { state: NegotiationState; view: View }) {
  if (view !== 'god') {
    return <div className="note">The joint utility space needs both sealed envelopes, so it is only visible in the
      arbiter's <b>God view</b>. Each party can see only its own concession curve.</div>;
  }
  const analysis = state.analysis;
  const buyer = dancePoints(state, 'buyer');
  const supplier = dancePoints(state, 'supplier');
  if (!analysis || (!buyer.length && !supplier.length)) {
    return <div className="empty small">The negotiation dance is plotted against the Pareto frontier once play starts.</div>;
  }
  const all = [...buyer, ...supplier, ...analysis.frontier];
  const lo = Math.min(-0.1, ...all.map((p) => Math.min(p.u_buyer, p.u_supplier)));
  const domain: [number, number] = [Math.floor(lo * 10) / 10, 1.05];
  const agreement = state.agreementAnalysis
    ? [{ u_buyer: state.agreementAnalysis.u_buyer, u_supplier: state.agreementAnalysis.u_supplier }] : [];
  const nash = analysis.nash ? [{ u_buyer: analysis.nash.u_buyer, u_supplier: analysis.nash.u_supplier }] : [];
  return (
    <ResponsiveContainer width="100%" height={290}>
      <ScatterChart margin={{ top: 8, right: 12, bottom: 18, left: -12 }}>
        <CartesianGrid stroke={C.grid} strokeDasharray="3 3" />
        <XAxis type="number" dataKey="u_buyer" name="Buyer utility" domain={domain} tick={axisTick} stroke={C.grid}
               tickFormatter={(v: number) => v.toFixed(1)}
               label={{ value: 'buyer utility →', position: 'insideBottom', offset: -10, fill: C.muted, fontSize: 10.5 }} />
        <YAxis type="number" dataKey="u_supplier" name="Supplier utility" domain={domain} tick={axisTick} stroke={C.grid}
               tickFormatter={(v: number) => v.toFixed(1)} />
        <Tooltip contentStyle={tooltipStyle} formatter={(v) => (typeof v === 'number' ? v.toFixed(3) : String(v))} />
        <Legend wrapperStyle={{ fontSize: 11 }} verticalAlign="top" height={26} />
        <ReferenceLine x={analysis.reservations.buyer} stroke={C.buyer} strokeDasharray="4 4" strokeOpacity={0.5} />
        <ReferenceLine y={analysis.reservations.supplier} stroke={C.supplier} strokeDasharray="4 4" strokeOpacity={0.5} />
        <Scatter name="Pareto frontier" data={analysis.frontier} fill={C.arbiter} line={{ stroke: C.arbiter, strokeWidth: 2 }}
                 shape={hidden} isAnimationActive={false} />
        <Scatter name="Buyer offers" data={buyer} fill={C.buyer} line={{ stroke: C.buyer, strokeOpacity: 0.45 }}
                 shape={dot(C.buyer)} isAnimationActive={false} />
        <Scatter name="Supplier offers" data={supplier} fill={C.supplier} line={{ stroke: C.supplier, strokeOpacity: 0.45 }}
                 shape={dot(C.supplier)} isAnimationActive={false} />
        {nash.length ? <Scatter name="Nash solution" data={nash} fill={C.accent} shape="star" isAnimationActive={false} /> : null}
        {agreement.length ? <Scatter name="Agreement" data={agreement} fill={C.ok} shape={dot(C.ok, 7)} isAnimationActive={false} /> : null}
      </ScatterChart>
    </ResponsiveContainer>
  );
}

export function Sparkline({ values, color }: { values: number[]; color: string }) {
  if (values.length < 2) return <div style={{ height: 36 }} />;
  return (
    <ResponsiveContainer width="100%" height={36}>
      <LineChart data={values.map((v, i) => ({ i, v }))} margin={{ top: 4, right: 2, bottom: 2, left: 2 }}>
        <YAxis hide domain={['auto', 'auto']} />
        <Line type="monotone" dataKey="v" stroke={color} strokeWidth={2} dot={false} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}
