import type { IssueSpec, Role, StreamEvent, Terms } from './types';

export interface TurnView {
  index: number;
  round: number;
  actor: Role;
  action: 'counter' | 'accept' | 'walk_away';
  offer: Terms | null;
  message: string;
  source: string;
  verdict_status: string;
  public_violations: { rule_id: string; message: string; citation?: string }[];
  interventions: number;
  ts: string;
  target_utility?: number | null;
  own_utility?: number | null;
  reservation_utility?: number | null;
  rationale?: Record<string, any>;
  u_buyer?: number | null;
  u_supplier?: number | null;
  gap?: number | null;
  prevOwnOffer?: Terms | null;
}

export type Tone = 'info' | 'block' | 'redact' | 'deadlock' | 'mediation' | 'success' | 'fail' | 'contract';

export interface SystemItem {
  kind: 'system';
  id: number;
  type: string;
  tone: Tone;
  title: string;
  body?: string;
  actor?: Role;
  round?: number;
  terms?: Terms;
  detail?: Record<string, any>;
  data: Record<string, any>;
}

export type TimelineItem = { kind: 'turn'; id: number; turn: TurnView } | SystemItem;

export interface Analysis {
  zopa_exists: boolean;
  reservations: { buyer: number; supplier: number };
  frontier: { u_buyer: number; u_supplier: number }[];
  nash: { u_buyer: number; u_supplier: number; terms: Terms } | null;
  weights?: Record<Role, Record<string, number>>;
}

export interface NegotiationState {
  negotiationId?: string;
  supplierId?: string | null;
  title?: string;
  rfq?: Record<string, any>;
  supplier?: Record<string, any>;
  issues: IssueSpec[];
  maxRounds: number;
  round: number;
  status: string;
  reason?: string;
  turns: TurnView[];
  timeline: TimelineItem[];
  policy: Partial<Record<Role, Record<string, any>>>;
  analysis?: Analysis;
  agreement?: { terms: Terms; outcome: string; accepted_by: string; round: number };
  agreementAnalysis?: Record<string, any>;
  mediation?: { terms: Terms };
  approvalRequired: boolean;
  contract?: { contract_id: string; version: number; status: string; content_hash: string; cfo_required: boolean;
    drafting_model: string; amendment: boolean };
  gapHistory: number[];
  blocked: number;
  redacted: number;
  llmMode?: string;
  redTeam?: boolean;
  rulesCatalogue: string[];
  telemetry?: Record<string, any>;
  finished: boolean;
  error?: string;
}

export function emptyState(): NegotiationState {
  return { issues: [], maxRounds: 0, round: 0, status: 'idle', turns: [], timeline: [], policy: {},
    approvalRequired: false, gapHistory: [], blocked: 0, redacted: 0, finished: false, rulesCatalogue: [] };
}

const OUTCOME_TITLE: Record<string, string> = {
  agreement: 'Agreement reached',
  mediated_agreement: 'Agreement reached through mediation',
  no_deal: 'No deal: both parties keep their BATNA',
  failed: 'Negotiation failed compliance review',
};

export function reduceEvents(events: StreamEvent[]): NegotiationState {
  const s = emptyState();
  const turnsByIndex = new Map<number, TurnView>();
  const lastOffer: Partial<Record<Role, Terms>> = {};
  const push = (item: SystemItem) => s.timeline.push(item);
  const lastSystem = (type: string, actor?: string, round?: number) => {
    for (let i = s.timeline.length - 1; i >= 0; i--) {
      const it = s.timeline[i];
      if (it.kind === 'system' && it.type === type && (actor === undefined || it.actor === actor)
          && (round === undefined || it.round === round)) return it;
    }
    return undefined;
  };

  for (const ev of events) {
    const d = ev.data ?? {};
    switch (ev.type) {
      case 'negotiation_started': {
        s.status = 'running';
        s.negotiationId = d.negotiation_id;
        s.supplierId = d.supplier_id;
        s.title = d.title;
        s.rfq = d.rfq;
        s.supplier = d.supplier;
        s.issues = d.issues ?? [];
        s.maxRounds = d.max_rounds ?? 0;
        s.llmMode = d.llm_mode;
        s.redTeam = d.red_team;
        s.rulesCatalogue = d.rules_catalogue ?? [];
        push({ kind: 'system', id: ev.id, type: ev.type, tone: 'info', data: d,
          title: `Negotiation opened · ${d.rfq?.reference ?? ''}`,
          body: `${d.rfq?.buyer?.name} ↔ ${d.supplier?.name} · ${s.issues.length} issues · ${s.maxRounds} rounds · `
            + `${d.rulebook?.jurisdiction} rulebook ${d.rulebook?.version}` });
        break;
      }
      case 'envelopes_sealed':
        push({ kind: 'system', id: ev.id, type: ev.type, tone: 'info', data: d, title: 'Policy envelopes sealed',
          body: 'Only hash commitments are shared - reservation values never leave the Legal Arbiter.' });
        break;
      case 'policy_setup':
        s.policy[d.role as Role] = d;
        break;
      case 'bargaining_analysis':
        s.analysis = d as Analysis;
        break;
      case 'turn': {
        const actor = d.actor as Role;
        const t: TurnView = {
          ...(turnsByIndex.get(d.index) ?? {}),
          index: d.index, round: d.round, actor, action: d.action, offer: d.offer ?? null, message: d.message,
          source: d.source, verdict_status: d.verdict_status, public_violations: d.public_violations ?? [],
          interventions: d.interventions ?? 0, ts: ev.ts, prevOwnOffer: lastOffer[actor] ?? null,
        };
        if (d.offer) lastOffer[actor] = d.offer;
        turnsByIndex.set(d.index, t);
        s.round = Math.max(s.round, d.round);
        s.timeline.push({ kind: 'turn', id: ev.id, turn: t });
        break;
      }
      case 'turn_private': {
        const t = turnsByIndex.get(d.index);
        if (t) Object.assign(t, { target_utility: d.target_utility, own_utility: d.own_utility,
          reservation_utility: d.reservation_utility, rationale: d.rationale });
        break;
      }
      case 'analytics': {
        const t = turnsByIndex.get(d.index);
        if (t) Object.assign(t, { u_buyer: d.u_buyer, u_supplier: d.u_supplier, gap: d.gap });
        break;
      }
      case 'round_summary':
        s.gapHistory = d.gap_history ?? s.gapHistory;
        break;
      case 'guardrail_block':
        s.blocked += 1;
        push({ kind: 'system', id: ev.id, type: ev.type, tone: 'block', actor: d.actor, round: d.round, data: d,
          title: `Legal Arbiter blocked the ${d.actor}'s ${d.attempted_action}`,
          body: d.public_violations?.length
            ? d.public_violations.map((v: any) => `${v.rule_id}: ${v.message}`).join(' · ')
            : 'Private mandate breach - details are visible only to the sender. The agent re-planned within its mandate.' });
        break;
      case 'guardrail_block_detail': {
        const item = lastSystem('guardrail_block', d.actor, d.round);
        if (item && item.kind === 'system') item.detail = d;
        break;
      }
      case 'message_redacted':
        s.redacted += 1;
        push({ kind: 'system', id: ev.id, type: ev.type, tone: 'redact', actor: d.actor, round: d.round, data: d,
          title: `Message from the ${d.actor} sanitised`, body: (d.rules ?? []).join(' · ') });
        break;
      case 'message_redacted_detail': {
        const item = lastSystem('message_redacted', d.actor, d.round);
        if (item && item.kind === 'system') item.detail = d;
        break;
      }
      case 'deadlock_detected':
        push({ kind: 'system', id: ev.id, type: ev.type, tone: 'deadlock', round: d.round, data: d,
          title: 'Deadlock detected', body: `${d.reason}. ${d.action}.` });
        break;
      case 'mediation_proposal':
        s.mediation = { terms: d.terms };
        push({ kind: 'system', id: ev.id, type: ev.type, tone: 'mediation', round: d.round, data: d, terms: d.terms,
          title: `Mediator proposal · ${d.method}`, body: d.message });
        break;
      case 'mediation_response':
        push({ kind: 'system', id: ev.id, type: ev.type, tone: d.accepted ? 'success' : 'fail', actor: d.actor,
          data: d, title: `${d.actor === 'buyer' ? 'Buyer' : 'Supplier'} ${d.accepted ? 'accepts' : 'rejects'} the mediated proposal`,
          body: d.message });
        break;
      case 'mediation_failed':
        push({ kind: 'system', id: ev.id, type: ev.type, tone: 'fail', data: d, title: 'Mediation failed', body: d.reason });
        break;
      case 'agreement':
        s.agreement = { terms: d.terms, outcome: d.outcome, accepted_by: d.accepted_by, round: d.round };
        push({ kind: 'system', id: ev.id, type: ev.type, tone: 'success', data: d, terms: d.terms,
          title: OUTCOME_TITLE[d.outcome] ?? 'Agreement', body: `Accepted by ${d.accepted_by} in round ${d.round}` });
        break;
      case 'approval_required':
        s.approvalRequired = true;
        push({ kind: 'system', id: ev.id, type: ev.type, tone: 'info', data: d, title: 'CFO co-signature required',
          body: d.reason });
        break;
      case 'agreement_analysis':
        s.agreementAnalysis = d;
        break;
      case 'negotiation_finished':
        s.status = d.status;
        s.reason = d.reason;
        if (d.status !== 'agreement' && d.status !== 'mediated_agreement') {
          push({ kind: 'system', id: ev.id, type: ev.type, tone: 'fail', data: d,
            title: OUTCOME_TITLE[d.status] ?? d.status, body: d.reason });
        }
        break;
      case 'contract_compiled':
      case 'amendment_compiled':
        s.contract = { contract_id: d.contract_id, version: d.version, status: d.status, content_hash: d.content_hash,
          cfo_required: d.cfo_required, drafting_model: d.drafting_model, amendment: ev.type === 'amendment_compiled' };
        push({ kind: 'system', id: ev.id, type: ev.type, tone: 'contract', data: d,
          title: ev.type === 'amendment_compiled' ? `Amendment v${d.version} compiled & signed` : 'Contract compiled & signed',
          body: `${d.contract_id} · ${d.status.replace(/_/g, ' ')} · sha256 ${d.content_hash.slice(0, 16)}… · drafted via ${d.drafting_model}` });
        break;
      case 'telemetry_event':
        s.telemetry = d;
        push({ kind: 'system', id: ev.id, type: ev.type, tone: 'info', data: d,
          title: `Telemetry: ${String(d.event?.event_type ?? '').replace(/_/g, ' ')} (${d.event?.source ?? ''})`,
          body: d.assessment?.reason });
        break;
      case 'renegotiation_failed':
        push({ kind: 'system', id: ev.id, type: ev.type, tone: 'fail', data: d, title: 'Renegotiation failed',
          body: `${d.reason}. ${d.fallback}` });
        break;
      case 'error':
        s.status = 'error';
        s.error = d.message;
        push({ kind: 'system', id: ev.id, type: ev.type, tone: 'fail', data: d, title: 'Error', body: d.message });
        break;
      case 'run_finished':
        s.finished = true;
        break;
      default:
        break;
    }
  }
  s.turns = [...turnsByIndex.values()].sort((a, b) => a.index - b.index);
  return s;
}

/** Utility of each side's own offers per round - available per role (turn_private) or god view (analytics). */
export function concessionSeries(s: NegotiationState) {
  const rows = new Map<number, Record<string, number | null>>();
  for (const t of s.turns) {
    if (t.action !== 'counter') continue;
    const row = rows.get(t.round) ?? { round: t.round };
    const own = t.actor === 'buyer' ? (t.u_buyer ?? t.own_utility) : (t.u_supplier ?? t.own_utility);
    if (own !== undefined && own !== null) row[t.actor] = own;
    if (t.target_utility !== undefined && t.target_utility !== null) row[`${t.actor}_target`] = t.target_utility;
    rows.set(t.round, row);
  }
  return [...rows.values()].sort((a, b) => (a.round as number) - (b.round as number));
}

export function bidSeries(s: NegotiationState, issueKey: string) {
  const rows = new Map<number, Record<string, number>>();
  for (const t of s.turns) {
    if (!t.offer) continue;
    const row = rows.get(t.round) ?? { round: t.round };
    row[t.actor] = t.offer[issueKey];
    rows.set(t.round, row);
  }
  return [...rows.values()].sort((a, b) => a.round - b.round);
}

export function dancePoints(s: NegotiationState, actor: Role) {
  return s.turns
    .filter((t) => t.actor === actor && t.offer && t.u_buyer != null && t.u_supplier != null)
    .map((t) => ({ u_buyer: t.u_buyer as number, u_supplier: t.u_supplier as number, round: t.round }));
}

/** Share of each issue's opening gap closed by each side (public data only - valid in every view). */
export function gapClosure(s: NegotiationState) {
  const b = s.turns.filter((t) => t.actor === 'buyer' && t.offer);
  const sp = s.turns.filter((t) => t.actor === 'supplier' && t.offer);
  if (!b.length || !sp.length) return [];
  const b0 = b[0].offer as Terms;
  const s0 = sp[0].offer as Terms;
  const bN = s.agreement?.terms ?? (b[b.length - 1].offer as Terms);
  const sN = s.agreement?.terms ?? (sp[sp.length - 1].offer as Terms);
  return s.issues.map((spec) => {
    const k = spec.key;
    const gap = Math.abs(s0[k] - b0[k]);
    if (gap < 1e-9) return { issue: spec.label, buyer: 0, supplier: 0, open: 0 };
    const buyer = Math.min(1, Math.abs(bN[k] - b0[k]) / gap);
    const supplier = Math.min(1 - buyer, Math.abs(sN[k] - s0[k]) / gap);
    return { issue: spec.label, buyer: Math.round(buyer * 100), supplier: Math.round(supplier * 100),
      open: Math.max(0, 100 - Math.round(buyer * 100) - Math.round(supplier * 100)) };
  });
}

/** Utility each side gave up per round (own-utility drop between consecutive own offers). */
export function concessionRate(s: NegotiationState) {
  const series = concessionSeries(s);
  return series.slice(1).map((row, i) => {
    const prev = series[i];
    const d = (k: 'buyer' | 'supplier') => (typeof prev[k] === 'number' && typeof row[k] === 'number'
      ? Math.max(0, (prev[k] as number) - (row[k] as number)) : null);
    return { round: row.round, buyer: d('buyer'), supplier: d('supplier') };
  });
}

/** Latest opponent-weight estimate each agent holds (from its private rationale). */
export function opponentEstimates(s: NegotiationState): Partial<Record<Role, Record<string, number>>> {
  const out: Partial<Record<Role, Record<string, number>>> = {};
  for (const t of s.turns) {
    const w = t.rationale?.opponent_weights;
    if (w) out[t.actor] = w;
  }
  return out;
}
