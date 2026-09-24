import { useEffect, useRef } from 'react';
import { fmtTime, fmtUtility, fmtValue, moveKind } from '../format';
import type { SystemItem, TimelineItem, TurnView } from '../state';
import type { IssueSpec, Terms } from '../types';
import { Badge } from './ui';

const REDACTION_SPLIT = /(\[redacted by Legal Arbiter[^\]]*\]|\[neutralised by Legal Arbiter[^\]]*\]|\[contact details redacted\]|\[withheld by Lyzr Safe AI policy\]|\*\*\*)/;
const isRedaction = (p: string) =>
  p === '***' || p.startsWith('[redacted by') || p.startsWith('[neutralised by') || p.startsWith('[withheld by')
  || p === '[contact details redacted]';

function Message({ text }: { text: string }) {
  const parts = text.split(REDACTION_SPLIT);
  return (
    <div className="bubble-msg">
      {parts.map((p, i) => (isRedaction(p)
        ? <span key={i} className="redacted" title="Sanitised by the Legal Arbiter">{p}</span>
        : <span key={i}>{p}</span>))}
    </div>
  );
}

export function TermChips({ offer, prev, issues, actor }: { offer: Terms; prev?: Terms | null; issues: IssueSpec[];
  actor: 'buyer' | 'supplier' }) {
  return (
    <div className="chips">
      {issues.map((spec) => {
        const kind = moveKind(spec, actor, prev?.[spec.key], offer[spec.key]);
        const arrow = kind === 'hold' ? '' : kind === 'concede' ? ' ↘' : ' ↗';
        return (
          <span key={spec.key} className={`chip ${kind}`} title={kind === 'concede' ? 'concession' : kind === 'harden' ? 'hardened' : 'unchanged'}>
            {spec.label}: <b>{fmtValue(spec, offer[spec.key])}</b>{arrow}
          </span>
        );
      })}
    </div>
  );
}

const SOURCE_TONE: Record<string, 'arbiter' | 'bad' | 'accent' | ''> = { llm: 'accent', fallback: 'arbiter', rogue: 'bad' };

function TurnBubble({ t, issues }: { t: TurnView; issues: IssueSpec[] }) {
  const who = t.actor === 'buyer' ? 'Buyer agent' : 'Supplier agent';
  return (
    <div className={`bubble-row ${t.actor}`}>
      <div className={`bubble ${t.actor} ${t.action === 'accept' ? 'accept' : ''}`}>
        <div className="bubble-head">
          <span className={`who ${t.actor}`}>{who}</span>
          <Badge>R{t.round}</Badge>
          <Badge tone={t.action === 'accept' ? 'ok' : t.action === 'walk_away' ? 'bad' : ''}>{t.action.replace('_', ' ')}</Badge>
          {t.source !== 'engine' ? <Badge tone={SOURCE_TONE[t.source] ?? ''} title="Who produced this move">{t.source}</Badge> : null}
          {t.verdict_status !== 'approved' ? <Badge tone="arbiter">{t.verdict_status}</Badge> : null}
          {t.interventions > 0 ? <Badge tone="warn" title="Guardrail interventions on this move">⛨ {t.interventions}</Badge> : null}
        </div>
        <Message text={t.message} />
        {t.offer ? <TermChips offer={t.offer} prev={t.prevOwnOffer} issues={issues} actor={t.actor} /> : null}
        <div className="bubble-foot">
          <span>{fmtTime(t.ts)}</span>
          {t.target_utility != null ? <span title="Private: target utility from the concession schedule">target {fmtUtility(t.target_utility)}</span> : null}
          {t.own_utility != null ? <span title="Private: own utility of this offer">own u {fmtUtility(t.own_utility)}</span> : null}
          {t.u_buyer != null && t.u_supplier != null
            ? <span title="Arbiter view: utility for each party">u<sub>b</sub> {fmtUtility(t.u_buyer)} · u<sub>s</sub> {fmtUtility(t.u_supplier)}</span> : null}
          {t.gap != null ? <span title="Normalised distance to the counterpart's standing offer">gap {(t.gap * 100).toFixed(1)}%</span> : null}
        </div>
      </div>
    </div>
  );
}

function SystemCard({ item, issues }: { item: SystemItem; issues: IssueSpec[] }) {
  const icon: Record<string, string> = { block: '⛔', redact: '✂', deadlock: '⏸', mediation: '⚖', success: '✔',
    fail: '✖', contract: '📜', info: '◆' };
  const detail = item.detail;
  return (
    <div className={`sys ${item.tone}`}>
      <div className="t"><span>{icon[item.tone]}</span>{item.title}</div>
      {item.body ? <div className="b">{item.body}</div> : null}
      {item.terms ? (
        <div className="chips">
          {issues.map((s) => <span key={s.key} className="chip">{s.label}: <b>{fmtValue(s, item.terms![s.key])}</b></span>)}
        </div>
      ) : null}
      {detail && item.type === 'guardrail_block' ? (
        <details>
          <summary>Private detail (visible to the {item.actor} and the arbiter)</summary>
          {detail.violations?.map((v: any, i: number) => (
            <div key={i} className="small" style={{ marginTop: 4 }}><b className="mono">{v.rule_id}</b> {v.message}</div>
          ))}
          {detail.attempted?.message ? <div className="small faint" style={{ marginTop: 4 }}>Attempted message: “{detail.attempted.message}”</div> : null}
          <div className="small faint">Source: {detail.source}</div>
        </details>
      ) : null}
      {detail && item.type === 'message_redacted' ? (
        <details>
          <summary>What was removed (visible to the {item.actor} and the arbiter)</summary>
          {detail.redactions?.map((r: any, i: number) => (
            <div key={i} className="redaction"><b>{r.rule_id}</b> · {r.reason}<br /><del>{r.original}</del> → <ins>{r.replacement}</ins></div>
          ))}
        </details>
      ) : null}
    </div>
  );
}

export function Timeline({ items, issues, compact = false, autoScroll = true }: {
  items: TimelineItem[]; issues: IssueSpec[]; compact?: boolean; autoScroll?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (autoScroll && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [items.length, autoScroll]);
  return (
    <div className={`timeline ${compact ? 'compact' : ''}`} ref={ref}>
      {items.map((it) => (it.kind === 'turn'
        ? <TurnBubble key={`t${it.id}`} t={it.turn} issues={issues} />
        : <SystemCard key={`s${it.id}`} item={it} issues={issues} />))}
    </div>
  );
}
