import { describe, expect, it } from 'vitest';
import deadlockGod from './test-fixtures/deadlock.god.json';
import redTeamGod from './test-fixtures/red-team.god.json';
import redTeamSupplier from './test-fixtures/red-team.supplier.json';
import {
  bidSeries, concessionRate, concessionSeries, dancePoints, emptyState, gapClosure, opponentEstimates, reduceEvents,
} from './state';
import type { StreamEvent } from './types';

// Real event streams recorded from the backend (scripts/export_ui_fixtures.py), so these tests also pin the contract
// between the server's event stream and the UI.
const god = reduceEvents(redTeamGod as StreamEvent[]);
const supplierView = reduceEvents(redTeamSupplier as StreamEvent[]);
const deadlock = reduceEvents(deadlockGod as StreamEvent[]);
const count = (events: unknown[], type: string) => (events as StreamEvent[]).filter((e) => e.type === type).length;

describe('reduceEvents on a red-team negotiation (arbiter view)', () => {
  it('rebuilds the run: header, every turn in order, outcome and contract', () => {
    expect(god.negotiationId).toMatch(/^NEG-/);
    expect(god.title).toBe('Automotive MCU spot PO');
    expect(god.issues).toHaveLength(7);
    expect(god.maxRounds).toBe(12);
    expect(god.redTeam).toBe(true);
    expect(god.turns).toHaveLength(count(redTeamGod, 'turn'));
    expect(god.turns.map((t) => t.index)).toEqual([...god.turns].map((t) => t.index).sort((a, b) => a - b));
    expect(god.status).toBe('agreement');
    expect(god.finished).toBe(true);
    expect(god.agreement?.terms.unit_price).toBeGreaterThan(0);
    expect(god.contract?.contract_id).toMatch(/^CTR-/);
    expect(god.timeline.some((i) => i.kind === 'system' && i.title === 'Contract compiled & signed')).toBe(true);
  });

  it('counts every block and redaction and attaches the private detail to the matching card', () => {
    expect(god.blocked).toBe(count(redTeamGod, 'guardrail_block'));
    expect(god.redacted).toBe(count(redTeamGod, 'message_redacted'));
    const blocks = god.timeline.filter((i) => i.kind === 'system' && i.type === 'guardrail_block');
    expect(blocks.length).toBeGreaterThan(0);
    for (const b of blocks) expect(b.kind === 'system' && b.detail?.violations?.length).toBeTruthy();
  });

  it('merges private rationale and arbiter analytics into each turn', () => {
    for (const t of god.turns) {
      expect(t.u_buyer).toEqual(expect.any(Number));
      expect(t.u_supplier).toEqual(expect.any(Number));
      if (t.action === 'counter') expect(t.target_utility).toEqual(expect.any(Number));
    }
    expect(god.analysis?.frontier.length).toBeGreaterThan(2);
    expect(god.analysis?.nash).not.toBeNull();
  });

  it('remembers each side’s previous offer so the UI can show what moved', () => {
    const buyerTurns = god.turns.filter((t) => t.actor === 'buyer' && t.offer);
    expect(buyerTurns[0].prevOwnOffer).toBeNull();
    expect(buyerTurns[1].prevOwnOffer).toEqual(buyerTurns[0].offer);
  });
});

describe('reduceEvents keeps the supplier’s view private', () => {
  it('shows the same public negotiation', () => {
    expect(supplierView.turns).toHaveLength(god.turns.length);
    expect(supplierView.status).toBe('agreement');
    expect(supplierView.blocked).toBe(god.blocked);
  });

  it('never exposes the buyer’s rationale, the joint analytics or the buyer’s block details', () => {
    for (const t of supplierView.turns) {
      if (t.actor === 'buyer') {
        expect(t.target_utility).toBeUndefined();
        expect(t.rationale).toBeUndefined();
      }
      expect(t.u_buyer).toBeUndefined();
    }
    expect(supplierView.analysis).toBeUndefined();
    expect(supplierView.policy.buyer).toBeUndefined();
    const buyerBlocks = supplierView.timeline.filter((i) => i.kind === 'system' && i.type === 'guardrail_block' && i.actor === 'buyer');
    for (const b of buyerBlocks) expect(b.kind === 'system' && b.detail).toBeFalsy();
  });

  it('still shows the supplier its own reasoning', () => {
    const own = supplierView.turns.filter((t) => t.actor === 'supplier' && t.action === 'counter');
    expect(own.length).toBeGreaterThan(0);
    for (const t of own) expect(t.target_utility).toEqual(expect.any(Number));
  });
});

describe('reduceEvents on a deadlock that ends in mediation', () => {
  it('shows the deadlock, the mediator’s proposal, both responses and the CFO step', () => {
    const types = deadlock.timeline.filter((i) => i.kind === 'system').map((i) => i.type);
    expect(types).toEqual(expect.arrayContaining(['deadlock_detected', 'mediation_proposal', 'mediation_response',
      'approval_required', 'contract_compiled']));
    expect(deadlock.mediation?.terms).toBeDefined();
    expect(deadlock.approvalRequired).toBe(true);
    expect(deadlock.status).toBe('mediated_agreement');
    const agreed = deadlock.timeline.find((i) => i.kind === 'system' && i.type === 'agreement');
    expect(agreed?.kind === 'system' && agreed.title).toBe('Agreement reached through mediation');
  });
});

describe('reduceEvents on hand-made edge cases', () => {
  const ev = (id: number, type: string, data: Record<string, unknown> = {}): StreamEvent =>
    ({ id, type, data, ts: '2026-01-01T00:00:00Z', visibility: ['public'] }) as unknown as StreamEvent;

  it('starts empty and ignores event types it does not know', () => {
    expect(reduceEvents([])).toEqual(emptyState());
    expect(reduceEvents([ev(1, 'something_new', { x: 1 })]).timeline).toEqual([]);
  });

  it('surfaces errors and non-agreements', () => {
    const failed = reduceEvents([ev(1, 'negotiation_finished', { status: 'no_deal', reason: 'No ZOPA' }),
      ev(2, 'error', { message: 'boom' })]);
    expect(failed.status).toBe('error');
    expect(failed.error).toBe('boom');
    expect(failed.timeline.map((i) => i.kind === 'system' && i.title)).toEqual(['No deal: both parties keep their BATNA', 'Error']);
  });

  it('attaches a block detail only to the card for the same actor and round', () => {
    const s = reduceEvents([
      ev(1, 'guardrail_block', { actor: 'buyer', round: 1, attempted_action: 'counter', public_violations: [] }),
      ev(2, 'guardrail_block', { actor: 'buyer', round: 2, attempted_action: 'counter',
        public_violations: [{ rule_id: 'IN-MSMED-S15', message: 'over 45 days' }] }),
      ev(3, 'guardrail_block_detail', { actor: 'buyer', round: 1, violations: [{ rule_id: 'POLICY-BUDGET' }] }),
    ]);
    const [first, second] = s.timeline;
    expect(first.kind === 'system' && first.detail?.violations[0].rule_id).toBe('POLICY-BUDGET');
    expect(second.kind === 'system' && second.detail).toBeUndefined();
    expect(second.kind === 'system' && second.body).toBe('IN-MSMED-S15: over 45 days');
  });

  it('shows a telemetry-triggered renegotiation, and a mediation or renegotiation that fails', () => {
    const s = reduceEvents([
      ev(1, 'telemetry_event', { event: { event_type: 'weather_delay', source: 'imd-feed' },
        assessment: { reason: 'Cyclone qualifies as force majeure' } }),
      ev(2, 'mediation_failed', { reason: 'Supplier rejected the Nash proposal' }),
      ev(3, 'renegotiation_failed', { reason: 'No compliant amendment', fallback: 'The contract stands as written' }),
    ]);
    expect(s.telemetry?.event.source).toBe('imd-feed');
    expect(s.timeline.map((i) => i.kind === 'system' && [i.title, i.body])).toEqual([
      ['Telemetry: weather delay (imd-feed)', 'Cyclone qualifies as force majeure'],
      ['Mediation failed', 'Supplier rejected the Nash proposal'],
      ['Renegotiation failed', 'No compliant amendment. The contract stands as written'],
    ]);
  });

  it('marks an amendment differently from a first contract', () => {
    const s = reduceEvents([ev(1, 'amendment_compiled', { contract_id: 'CTR-1', version: 2, status: 'executed',
      content_hash: 'a'.repeat(64), cfo_required: false, drafting_model: 'offline-template' })]);
    expect(s.contract?.amendment).toBe(true);
    expect(s.timeline[0].kind === 'system' && s.timeline[0].title).toBe('Amendment v2 compiled & signed');
  });
});

describe('analytics helpers', () => {
  it('concession series: one row per round, each side starting from its ideal', () => {
    const rows = concessionSeries(god);
    expect(rows.map((r) => r.round)).toEqual([...rows.map((r) => r.round)].sort((a, b) => (a as number) - (b as number)));
    expect(rows[0].buyer).toBeCloseTo(1, 5);
    expect(rows[0].supplier).toBeCloseTo(1, 5);
  });

  it('concession rate: utility given up per round is never negative', () => {
    const rate = concessionRate(god);
    expect(rate.length).toBe(concessionSeries(god).length - 1);
    for (const r of rate) for (const v of [r.buyer, r.supplier]) if (v !== null) expect(v).toBeGreaterThanOrEqual(0);
  });

  it('gap closure: after an agreement every issue’s gap is fully closed between the two sides', () => {
    const rows = gapClosure(god);
    expect(rows).toHaveLength(7);
    for (const r of rows) {
      expect(r.buyer + r.supplier + r.open).toBe(100);
      expect(r.open).toBeLessThanOrEqual(1);
    }
    expect(gapClosure(emptyState())).toEqual([]);
  });

  it('bid series and dance points follow each side’s offers', () => {
    const bids = bidSeries(god, 'unit_price');
    expect(bids.length).toBeGreaterThan(5);
    const first = bids[0] as Record<string, number>;
    expect(first.supplier).toBeGreaterThan(first.buyer);
    expect(dancePoints(god, 'buyer')).toHaveLength(god.turns.filter((t) => t.actor === 'buyer' && t.offer).length);
    expect(dancePoints(supplierView, 'buyer')).toEqual([]); // joint utilities are arbiter-only
  });

  it('opponent estimates: each side’s learned weights form a distribution', () => {
    const est = opponentEstimates(god);
    for (const role of ['buyer', 'supplier'] as const) {
      const total = Object.values(est[role] ?? {}).reduce((a, b) => a + b, 0);
      expect(total).toBeCloseTo(1, 2);
    }
  });
});
