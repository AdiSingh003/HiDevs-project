import { describe, expect, it } from 'vitest';
import { fmtDateTime, fmtMoney, fmtTime, fmtUtility, fmtValue, moveKind, pretty, shortHash, termsEqual } from './format';
import type { IssueSpec } from './types';

const price = { key: 'unit_price', label: 'Unit price', unit: 'USD', decimals: 2, buyer_prefers: 'lower' } as IssueSpec;
const otif = { key: 'sla_on_time_pct', label: 'OTIF', unit: '%', decimals: 1, buyer_prefers: 'higher' } as IssueSpec;

describe('fmtValue', () => {
  it('formats currencies, percentages and plain units', () => {
    expect(fmtValue(price, 3.9)).toBe('USD 3.90');
    expect(fmtValue({ unit: 'INR', decimals: 0 }, 45600000)).toBe('INR 45,600,000');
    expect(fmtValue(otif, 98.5)).toBe('98.5%');
    expect(fmtValue({ unit: 'days', decimals: 0 }, 21)).toBe('21 days');
    expect(fmtValue(undefined, 1.234)).toBe('1.23');
  });

  it('shows a dash for missing values', () => {
    expect(fmtValue(price, null)).toBe('-');
    expect(fmtValue(price, undefined)).toBe('-');
    expect(fmtValue(price, Number.NaN)).toBe('-');
  });
});

describe('small formatters', () => {
  it('money, utilities, hashes and labels', () => {
    expect(fmtMoney(186000, 'USD')).toBe('USD 186,000');
    expect(fmtMoney(1234.5)).toBe('USD 1,234.5');
    expect(fmtMoney(null)).toBe('-');
    expect(fmtUtility(0.50041)).toBe('0.500');
    expect(fmtUtility(undefined)).toBe('-');
    expect(shortHash('abcdef0123456789')).toBe('abcdef0123…');
    expect(shortHash('abcdef', 3)).toBe('abc…');
    expect(shortHash(null)).toBe('-');
    expect(pretty('pending_cfo_approval')).toBe('pending cfo approval');
  });

  it('times and dates, with a dash when absent', () => {
    expect(fmtTime(null)).toBe('-');
    expect(fmtDateTime(undefined)).toBe('-');
    expect(fmtTime('2026-09-24T09:28:15Z')).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(fmtDateTime('2026-09-24T09:28:15Z')).toMatch(/^\d{2}\/\d{2}\/2026 \d{2}:\d{2}:\d{2}$/);
  });
});

describe('moveKind: did a side give ground on an issue?', () => {
  it('a buyer raising its price bid concedes; lowering it hardens', () => {
    expect(moveKind(price, 'buyer', 3.4, 3.45)).toBe('concede');
    expect(moveKind(price, 'buyer', 3.45, 3.4)).toBe('harden');
  });

  it('a supplier lowering its price ask concedes', () => {
    expect(moveKind(price, 'supplier', 4.6, 4.49)).toBe('concede');
    expect(moveKind(price, 'supplier', 4.49, 4.6)).toBe('harden');
  });

  it('on an issue the buyer wants higher, the directions flip', () => {
    expect(moveKind(otif, 'buyer', 99, 98.5)).toBe('concede');
    expect(moveKind(otif, 'supplier', 92, 94.2)).toBe('concede');
  });

  it('no previous offer, or no change, is a hold', () => {
    expect(moveKind(price, 'buyer', undefined, 3.4)).toBe('hold');
    expect(moveKind(price, 'buyer', 3.4, 3.4)).toBe('hold');
  });
});

describe('termsEqual', () => {
  it('compares offers issue by issue, tolerating float noise', () => {
    expect(termsEqual({ unit_price: 3.72, delivery_days: 21 }, { unit_price: 3.7200000001, delivery_days: 21 })).toBe(true);
    expect(termsEqual({ unit_price: 3.72 }, { unit_price: 3.84 })).toBe(false);
    expect(termsEqual(null, { unit_price: 1 })).toBe(false);
  });
});
