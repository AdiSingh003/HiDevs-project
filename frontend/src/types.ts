export type Role = 'buyer' | 'supplier';
export type View = 'god' | 'buyer' | 'supplier' | 'public';
export type Terms = Record<string, number>;

export interface IssueSpec {
  key: string;
  label: string;
  unit: string;
  buyer_prefers: 'lower' | 'higher';
  step: number;
  decimals: number;
}

export interface Party {
  name: string;
  address?: string;
  signatory_name: string;
  signatory_title: string;
  is_msme?: boolean;
  persona?: string;
}

export interface Mandate {
  ideal: number;
  limit: number;
  weight: number;
}

export interface Strategy {
  tactic: 'boulware' | 'linear' | 'conceder';
  beta?: number | null;
  reciprocity: number;
  tradeoff_sharpness: number;
  opening_utility: number;
  pace_tolerance: number;
  aspiration_floor: number;
}

export interface Envelope {
  role: Role;
  mandate: Record<string, Mandate>;
  batna_description: string;
  batna_terms?: Terms | null;
  batna_utility?: number | null;
  budget_cap?: number | null;
  auto_approve_limit?: number | null;
  unit_cost?: number | null;
  min_margin_pct?: number | null;
  strategy: Strategy;
  notes?: string;
}

export interface ScenarioSummary {
  id: string;
  title: string;
  summary: string;
  mode: 'bilateral' | 'rfq';
  tags: string[];
  max_rounds: number;
  jurisdiction: string;
  currency: string;
  buyer: string;
  suppliers: { id: string; name: string }[];
  issues: IssueSpec[];
}

export interface Scenario {
  id: string;
  title: string;
  summary: string;
  mode: 'bilateral' | 'rfq';
  tags: string[];
  max_rounds: number;
  context: {
    reference: string;
    title: string;
    item: string;
    quantity: number;
    quantity_unit: string;
    currency: string;
    incoterm: string;
    jurisdiction: string;
    buyer: Party;
  };
  issues: IssueSpec[];
  buyer: Envelope;
  suppliers: { id: string; party: Party; envelope: Envelope }[];
}

export interface StreamEvent {
  id: number;
  type: string;
  ts: string;
  visibility: string[];
  data: Record<string, any>;
}

export interface RunRecord {
  id: string;
  kind: 'negotiation' | 'rfq' | 'renegotiation';
  scenario_id: string;
  title: string;
  status: string;
  created_at: string;
  finished_at?: string | null;
  options: Record<string, any>;
  result?: Record<string, any> | null;
  contract_id?: string | null;
  contract_version?: number | null;
  parent_contract_id?: string | null;
  error?: string | null;
}

export interface PlatformStatus {
  lyzr: { configured: boolean; negotiator_agents: boolean; automata_agents: boolean; rai_policy: boolean;
    rai_per_party?: boolean; opa_policies: boolean; aims_mode: string };
  llm_modes: string[];
  safe_ai: { local_rules: boolean; lyzr_rai: boolean; lyzr_opa: boolean };
  automata: { engine: string; model: string };
  aims: { mode: string; local_ledger: boolean };
  counts: { runs: number; contracts: number; active_tasks: number };
  version: string;
}

export interface ContractSummary {
  contract_id: string;
  version: number;
  versions: number;
  status: string;
  title: string;
  buyer: string;
  supplier: string;
  currency: string;
  total_value: number;
  created_at: string;
  negotiation_id?: string;
  scenario_id: string;
}

export interface Signature {
  role: string;
  name: string;
  title: string;
  fingerprint: string;
  signature: string;
  signed_at: string;
  public_key: string;
}

export interface Contract {
  contract_id: string;
  version: number;
  parent_hash: string | null;
  title: string;
  status: string;
  created_at: string;
  effective_date: string;
  parties: { buyer: Party & { role: string }; supplier: Party & { role: string; supplier_id: string } };
  rfq: { reference: string; title: string; scenario_id: string };
  commercial_terms: { item: string; quantity: number; quantity_unit: string; currency: string; unit_price: number;
    total_value: number; incoterm: string; delivery_location: string; payment_terms_days?: number };
  delivery: { lead_time_days: number; delivery_due_date: string };
  liquidated_damages: { rate_pct_per_day?: number; cap_pct?: number; cap_amount?: number };
  terms: Terms;
  standard_terms?: Terms;
  issues: { key: string; label: string; unit: string; decimals: number }[];
  legal: { jurisdiction: string; jurisdiction_name: string; governing_law: string; dispute_resolution: string;
    rulebook_version: string; rules_applied: string[]; force_majeure_events: string[] };
  clauses: { id: string; title: string; text: string; source?: string }[];
  executable: { sla_rules: { id: string; trigger: string; formula: string; params: Record<string, any> }[] };
  negotiation: Record<string, any>;
  drafting: { engine: string; model: string; review: { approved: boolean; findings: any[] };
    substitutions: any[]; executive_summary: string };
  approvals: { required: string[]; cfo_required: boolean };
  amendment?: { event: Record<string, any>; assessment: Record<string, any>;
    changed_terms: Record<string, { from: number; to: number }>; ld_waiver?: string | null } | null;
  integrity: { hash_algorithm: string; content_hash: string; signatures: Signature[] };
}

export interface Verification {
  valid: boolean;
  fully_executed: boolean;
  hash_valid: boolean;
  computed_hash: string;
  recorded_hash: string;
  all_signatures_valid: boolean;
  missing_signatures: string[];
  signatures: { role: string; name: string; valid: boolean; fingerprint_matches: boolean }[];
}

export interface AuditEntry {
  seq: number;
  ts: string;
  stream: string;
  event_type: string;
  actor: string;
  visibility: string[];
  payload: Record<string, any>;
  prev_hash: string;
  hash: string;
  aims: string;
}

export interface ChainReport {
  valid: boolean;
  broken_at: number | null;
  reason: string;
  checked: number;
  head?: string;
  simulated_tamper?: number | null;
}
