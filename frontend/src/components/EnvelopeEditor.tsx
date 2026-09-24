import { useState } from 'react';
import { fmtMoney } from '../format';
import type { Envelope, IssueSpec, Role } from '../types';
import { Badge } from './ui';

export type EnvelopePatch = {
  mandate?: Record<string, Partial<{ ideal: number; limit: number; weight: number }>>;
  strategy?: Record<string, number | string>;
  budget_cap?: number;
  unit_cost?: number;
  min_margin_pct?: number;
};

function num(v: string): number | undefined {
  const n = Number(v);
  return v.trim() === '' || Number.isNaN(n) ? undefined : n;
}

export function EnvelopeEditor({ role, org, envelope, issues, currency, patch, onPatch, defaultOpen = false }: {
  role: Role; org: string; envelope: Envelope; issues: IssueSpec[]; currency: string; patch: EnvelopePatch;
  onPatch: (p: EnvelopePatch) => void; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const [version, setVersion] = useState(0); // remounts the uncontrolled inputs after a reset
  const mandate =(key: string) => ({ ...envelope.mandate[key], ...(patch.mandate?.[key] ?? {}) });
  const strategy = { ...envelope.strategy, ...(patch.strategy ?? {}) } as Envelope['strategy'];
  const setMandate = (key: string, field: 'ideal' | 'limit' | 'weight', value: string) => {
    const v = num(value);
    const next = { ...(patch.mandate ?? {}) };
    next[key] = { ...(next[key] ?? {}), [field]: v };
    if (v === undefined) delete next[key][field];
    onPatch({ ...patch, mandate: next });
  };
  const setStrategy = (field: string, value: number | string) => onPatch({ ...patch, strategy: { ...(patch.strategy ?? {}), [field]: value } });
  const edited = Object.keys(patch.mandate ?? {}).length + Object.keys(patch.strategy ?? {}).length
    + (patch.budget_cap !== undefined ? 1 : 0) + (patch.unit_cost !== undefined ? 1 : 0);

  return (
    <div className="envelope">
      <div className="envelope-head" onClick={() => setOpen(!open)}>
        <Badge tone={role}>{role}</Badge>
        <span className="name" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{org}</span>
        <span className="spacer" />
        {edited ? <Badge tone="accent">{edited} edited</Badge> : null}
        <span className="lock">🔒 sealed {open ? '▾' : '▸'}</span>
      </div>
      {open ? (
        <div className="envelope-body" key={version}>
          <table>
            <thead><tr><th>Issue</th><th>Ideal</th><th>Limit</th><th>Wt</th></tr></thead>
            <tbody>
              {issues.map((s) => {
                const m = mandate(s.key);
                return (
                  <tr key={s.key}>
                    <td title={`${role} prefers ${role === 'buyer' ? s.buyer_prefers : (s.buyer_prefers === 'lower' ? 'higher' : 'lower')} values`}>{s.label}</td>
                    <td><input className="input num" type="number" step={s.step} defaultValue={m.ideal}
                               onBlur={(e) => setMandate(s.key, 'ideal', e.target.value)} /></td>
                    <td><input className="input num" type="number" step={s.step} defaultValue={m.limit}
                               onBlur={(e) => setMandate(s.key, 'limit', e.target.value)} /></td>
                    <td><input className="input num" style={{ width: 52 }} type="number" step={0.01} defaultValue={m.weight}
                               onBlur={(e) => setMandate(s.key, 'weight', e.target.value)} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="divider" />
          <div className="grid-2" style={{ gap: 8 }}>
            <label className="field"><span>Tactic</span>
              <select className="select" value={strategy.tactic} onChange={(e) => setStrategy('tactic', e.target.value)}>
                <option value="boulware">Boulware (firm)</option>
                <option value="linear">Linear</option>
                <option value="conceder">Conceder</option>
              </select>
            </label>
            <label className="field"><span>Reciprocity (tit-for-tat)</span>
              <input className="input num" type="number" min={0} max={1} step={0.05} defaultValue={strategy.reciprocity}
                     onBlur={(e) => { const v = num(e.target.value); if (v !== undefined) setStrategy('reciprocity', v); }} />
            </label>
            {role === 'buyer' ? (
              <label className="field"><span>CFO budget cap ({currency})</span>
                <input className="input num" type="number" defaultValue={patch.budget_cap ?? envelope.budget_cap ?? ''}
                       onBlur={(e) => onPatch({ ...patch, budget_cap: num(e.target.value) })} />
              </label>
            ) : (
              <label className="field"><span>Unit cost ({currency})</span>
                <input className="input num" type="number" defaultValue={patch.unit_cost ?? envelope.unit_cost ?? ''}
                       onBlur={(e) => onPatch({ ...patch, unit_cost: num(e.target.value) })} />
              </label>
            )}
            <label className="field"><span>Aspiration floor (posturing)</span>
              <input className="input num" type="number" min={0} max={0.95} step={0.05} defaultValue={strategy.aspiration_floor}
                     onBlur={(e) => { const v = num(e.target.value); if (v !== undefined) setStrategy('aspiration_floor', v); }} />
            </label>
          </div>
          <div className="small faint" style={{ marginTop: 8 }}>
            BATNA: {envelope.batna_description || '-'}
            {role === 'buyer' && envelope.auto_approve_limit ? <> · agent authority {fmtMoney(envelope.auto_approve_limit, currency)}</> : null}
            {role === 'supplier' && envelope.min_margin_pct != null ? <> · min margin {envelope.min_margin_pct}%</> : null}
          </div>
          {edited ? <button className="btn sm ghost" style={{ marginTop: 8 }}
                            onClick={() => { onPatch({}); setVersion((v) => v + 1); }}>Reset to scenario defaults</button> : null}
        </div>
      ) : null}
    </div>
  );
}
