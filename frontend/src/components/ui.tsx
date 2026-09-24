import type { ReactNode } from 'react';

export function Card({ children, className = '', style }: { children: ReactNode; className?: string;
  style?: React.CSSProperties }) {
  return <div className={`card ${className}`} style={style}>{children}</div>;
}

export function Label({ children, hint }: { children: ReactNode; hint?: ReactNode }) {
  return <div className="label">{children}{hint ? <span className="hint">{hint}</span> : null}</div>;
}

type Tone = 'ok' | 'bad' | 'warn' | 'accent' | 'buyer' | 'supplier' | 'arbiter' | '';

export function Badge({ children, tone = '', title }: { children: ReactNode; tone?: Tone; title?: string }) {
  return <span className={`badge ${tone}`} title={title}>{children}</span>;
}

const STATUS_TONE: Record<string, Tone> = {
  running: 'accent', agreement: 'ok', mediated_agreement: 'ok', awarded: 'ok', executed: 'ok',
  pending_cfo_approval: 'warn', no_deal: 'bad', no_award: 'bad', failed: 'bad', error: 'bad',
  interrupted: 'warn', idle: '',
};

export function StatusBadge({ status }: { status: string }) {
  const tone = STATUS_TONE[status] ?? '';
  return (
    <Badge tone={tone}>
      {status === 'running' ? <span className="dot pulse" /> : null}
      {status.replace(/_/g, ' ')}
    </Badge>
  );
}

export function Kpi({ k, v, s, color }: { k: string; v: ReactNode; s?: ReactNode; color?: string }) {
  return (
    <div className="kpi">
      <div className="k">{k}</div>
      <div className="v" style={color ? { color } : undefined}>{v}</div>
      {s ? <div className="s">{s}</div> : null}
    </div>
  );
}

export function Switch({ checked, onChange, label, accent = false, disabled = false }: {
  checked: boolean; onChange: (v: boolean) => void; label: ReactNode; accent?: boolean; disabled?: boolean }) {
  return (
    <label className="switch" style={disabled ? { opacity: 0.45, cursor: 'not-allowed' } : undefined}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className={`knob ${accent ? 'accent' : ''}`} />
      <span>{label}</span>
    </label>
  );
}

export function Seg<T extends string>({ value, options, onChange }: {
  value: T; options: { value: T; label: ReactNode; disabled?: boolean; title?: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button key={o.value} className={o.value === value ? 'on' : ''} disabled={o.disabled} title={o.title}
                onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><div className="h2">{title}</div>{children}</div>;
}

export function ErrorBox({ error }: { error: string | null | undefined }) {
  return error ? <div className="error-box">{error}</div> : null;
}

export function Json({ value, maxHeight }: { value: unknown; maxHeight?: number }) {
  return <pre className="json" style={maxHeight ? { maxHeight } : undefined}>{JSON.stringify(value, null, 2)}</pre>;
}
