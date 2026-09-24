import type {
  AuditEntry, ChainReport, Contract, ContractSummary, PlatformStatus, RunRecord, Scenario, ScenarioSummary,
  StreamEvent, Verification, View,
} from './types';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  const type = res.headers.get('content-type') ?? '';
  return (type.includes('application/json') ? res.json() : res.text()) as Promise<T>;
}

const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  status: () => request<PlatformStatus>('/api/status'),
  scenarios: () => request<ScenarioSummary[]>('/api/scenarios'),
  scenario: (id: string) => request<Scenario>(`/api/scenarios/${id}`),
  rego: (id: string, role: string, supplierId?: string) =>
    request<string>(`/api/scenarios/${id}/rego?role=${role}${supplierId ? `&supplier_id=${supplierId}` : ''}`),
  startNegotiation: (body: Record<string, unknown>) => post<RunRecord>('/api/negotiations', body),
  startRfq: (body: Record<string, unknown>) => post<RunRecord>('/api/rfq', body),
  runs: (kind?: string) => request<RunRecord[]>(`/api/runs${kind ? `?kind=${kind}` : ''}`),
  run: (id: string) => request<RunRecord>(`/api/runs/${id}`),
  contracts: () => request<ContractSummary[]>('/api/contracts'),
  contract: (id: string, version?: number) =>
    request<Contract>(`/api/contracts/${id}${version ? `?version=${version}` : ''}`),
  versions: (id: string) => request<any[]>(`/api/contracts/${id}/versions`),
  verify: (id: string, version?: number) =>
    post<Verification>(`/api/contracts/${id}/verify${version ? `?version=${version}` : ''}`),
  verifyDocument: (doc: unknown) => post<Verification>('/api/contracts/verify-document', doc),
  approve: (id: string, approverName: string) =>
    post<{ status: string; verification: Verification }>(`/api/contracts/${id}/approve`, { approver_name: approverName }),
  sla: (id: string, body: Record<string, unknown>) => post<any>(`/api/contracts/${id}/sla`, body),
  pdfUrl: (id: string, version?: number) => `/api/contracts/${id}/pdf${version ? `?version=${version}` : ''}`,
  simulateTelemetry: (body: Record<string, unknown>) => post<any>('/api/telemetry/simulate', body),
  auditStreams: () => request<{ stream: string; entries: number; head: string; valid: boolean; aims: Record<string, number> }[]>('/api/audit'),
  audit: (stream: string) =>
    request<{ stream: string; head: string; verification: ChainReport; aims_mode: string; entries: AuditEntry[] }>(`/api/audit/${stream}`),
  aims: (stream: string, rewriteSeq?: number) =>
    request<any>(`/api/audit/${stream}/aims${rewriteSeq ? `?rewrite_seq=${rewriteSeq}` : ''}`),
  verifyAudit: (stream: string, tamperSeq?: number) =>
    request<ChainReport>(`/api/audit/${stream}/verify${tamperSeq ? `?tamper_seq=${tamperSeq}` : ''}`),
};

/**
 * Subscribe to a run's Server-Sent Events. Uses fetch + a stream reader (instead of EventSource) so
 * every custom event type is delivered. Returns an unsubscribe function.
 */
export function streamRun(runId: string, view: View, onEvent: (e: StreamEvent) => void,
                          onEnd: () => void): () => void {
  const ctrl = new AbortController();
  (async () => {
    try {
      const res = await fetch(`/api/runs/${runId}/events?view=${view}`, {
        signal: ctrl.signal, headers: { Accept: 'text/event-stream' },
      });
      if (!res.ok || !res.body) throw new Error(`stream failed: ${res.status}`);
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value;
        let split: number;
        while ((split = buffer.indexOf('\n\n')) >= 0) {
          const chunk = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          let type = 'message';
          let data = '';
          for (const line of chunk.split('\n')) {
            if (line.startsWith('event: ')) type = line.slice(7);
            else if (line.startsWith('data: ')) data += line.slice(6);
          }
          if (type === 'end') {
            onEnd();
            return;
          }
          if (data) onEvent(JSON.parse(data) as StreamEvent);
        }
      }
      onEnd();
    } catch (err) {
      if (!ctrl.signal.aborted) {
        console.error(err);
        onEnd();
      }
    }
  })();
  return () => ctrl.abort();
}
