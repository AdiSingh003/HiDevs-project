import type { IssueSpec, Terms } from './types';

const CURRENCIES = new Set(['USD', 'EUR', 'INR', 'GBP']);

export function fmtValue(spec: Pick<IssueSpec, 'unit' | 'decimals'> | undefined, value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '-';
  const decimals = spec?.decimals ?? 2;
  const number = value.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  const unit = spec?.unit ?? '';
  if (CURRENCIES.has(unit)) return `${unit} ${number}`;
  if (unit === '%') return `${number}%`;
  return `${number} ${unit}`.trim();
}

export function fmtMoney(value: number | null | undefined, currency = 'USD'): string {
  if (value === null || value === undefined) return '-';
  return `${currency} ${value.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`;
}

export function fmtUtility(u: number | null | undefined): string {
  return u === null || u === undefined ? '-' : u.toFixed(3);
}

export function fmtTime(ts?: string | null): string {
  if (!ts) return '-';
  const d = new Date(ts);
  return d.toLocaleTimeString('en-GB', { hour12: false });
}

export function fmtDateTime(ts?: string | null): string {
  if (!ts) return '-';
  const d = new Date(ts);
  return `${d.toLocaleDateString('en-GB')} ${d.toLocaleTimeString('en-GB', { hour12: false })}`;
}

export function shortHash(h?: string | null, n = 10): string {
  return h ? `${h.slice(0, n)}…` : '-';
}

export function pretty(s: string): string {
  return s.replace(/_/g, ' ');
}

/** Direction of a move on one issue from the perspective of the role that prefers `prefers`. */
export function moveKind(spec: IssueSpec, role: 'buyer' | 'supplier', prev: number | undefined, next: number):
  'concede' | 'harden' | 'hold' {
  if (prev === undefined || Math.abs(prev - next) < 1e-9) return 'hold';
  const buyerLower = spec.buyer_prefers === 'lower';
  const roleLower = role === 'buyer' ? buyerLower : !buyerLower;
  const movedUp = next > prev;
  return (roleLower ? movedUp : !movedUp) ? 'concede' : 'harden';
}

export function termsEqual(a?: Terms | null, b?: Terms | null): boolean {
  if (!a || !b) return false;
  return Object.keys(a).every((k) => Math.abs((a[k] ?? 0) - (b[k] ?? 0)) < 1e-9);
}
