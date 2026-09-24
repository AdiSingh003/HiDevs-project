import { useState } from 'react';
import { api } from '../api';
import { Badge, Card, Empty, ErrorBox, Json, Label } from '../components/ui';
import { fmtTime, pretty, shortHash } from '../format';
import { useAsync } from '../hooks';
import type { AuditEntry, ChainReport } from '../types';

export function Audit({ stream, navigate }: { stream?: string; navigate: (p: string) => void }) {
  const streams = useAsync(() => api.auditStreams(), []);
  const selected = stream ?? streams.data?.[0]?.stream;
  return (
    <div className="split">
      <Card>
        <Label hint={`${streams.data?.length ?? 0} ledgers`}>Audit ledgers</Label>
        <ErrorBox error={streams.error} />
        {streams.data?.length === 0 ? <Empty title="No audit ledgers yet" /> : null}
        {(streams.data ?? []).map((s) => (
          <div key={s.stream} className={`list-item ${selected === s.stream ? 'on' : ''}`} onClick={() => navigate(`audit/${s.stream}`)}>
            <div className="row between"><b className="mono small">{s.stream}</b>
              {s.valid ? <Badge tone="ok">✔ intact</Badge> : <Badge tone="bad">✖ broken</Badge>}</div>
            <div className="row between tiny muted" style={{ marginTop: 3 }}><span>{s.entries} entries</span><span className="mono">{shortHash(s.head, 12)}</span></div>
          </div>
        ))}
      </Card>
      {selected ? <Ledger key={selected} stream={selected} /> : <div />}
    </div>
  );
}

function Ledger({ stream }: { stream: string }) {
  const ledger = useAsync(() => api.audit(stream), [stream]);
  const [open, setOpen] = useState<AuditEntry | null>(null);
  const [tamperSeq, setTamperSeq] = useState(5);
  const [tamper, setTamper] = useState<ChainReport | null>(null);
  const [aims, setAims] = useState<any>(null);
  const [aimsBusy, setAimsBusy] = useState(false);
  const [aimsErr, setAimsErr] = useState<string | null>(null);
  async function reconcile(rewriteSeq?: number) {
    setAimsBusy(true);
    setAimsErr(null);
    try { setAims(await api.aims(stream, rewriteSeq)); } catch (e) { setAimsErr((e as Error).message); }
    finally { setAimsBusy(false); }
  }
  const data = ledger.data;
  if (ledger.error) return <ErrorBox error={ledger.error} />;
  if (!data) return <Card><div className="muted">Loading…</div></Card>;
  const v = data.verification;
  return (
    <div className="stack" style={{ gap: 12 }}>
      <Card>
        <div className="row wrap" style={{ gap: 10 }}>
          <div className="stack" style={{ gap: 2 }}>
            <div className="h1">Ledger {stream}</div>
            <div className="small muted">SHA-256 hash chain · each entry commits to its predecessor · mirrored to Lyzr AIMS
              ({data.aims_mode === 'local' ? 'local mode - configure LYZR_API_KEY + AIMS_MODE' : data.aims_mode})</div>
          </div>
          <span className="spacer" />
          <button className="btn sm" onClick={() => { setTamper(null); ledger.reload(); }}>↻ Re-verify</button>
        </div>
        <div className={`verify-banner ${v.valid ? 'ok' : 'bad'}`} style={{ marginTop: 12 }}>
          {v.valid ? `✔ Chain intact - ${v.checked} entries verified` : `✖ Chain broken at #${v.broken_at}: ${v.reason}`}
          <span className="spacer" /><span className="hash">head {shortHash(data.head, 20)}</span>
        </div>
        <div className="note" style={{ marginTop: 10 }}>
          <div className="row wrap">
            <b>Tamper simulation</b><span className="small">alter one entry in a copy of this ledger and verify it:</span>
            <span className="spacer" />
            <input className="input" style={{ width: 90 }} type="number" min={1} max={data.entries.length} value={tamperSeq}
                   onChange={(e) => setTamperSeq(Number(e.target.value))} />
            <button className="btn sm danger" onClick={async () => setTamper(await api.verifyAudit(stream, tamperSeq))}>Tamper & verify</button>
          </div>
          {tamper ? (
            <div className={`verify-banner ${tamper.valid ? 'ok' : 'bad'}`} style={{ marginTop: 8 }}>
              {tamper.valid ? '✔ still valid' : `✖ Detected: entry #${tamper.broken_at} - ${tamper.reason}; every later hash is now invalid`}
            </div>
          ) : null}
        </div>
      </Card>
      <Card>
        <Label hint="independent witness">Lyzr AIMS anchors</Label>
        <div className="small muted" style={{ marginBottom: 10 }}>At each checkpoint (negotiation finished, contract
          compiled / approved / amended) the chain head is anchored in the Audit Scribe's AIMS session, where Lyzr stores it
          with its own timestamp. A local hash chain can be completely re-hashed by someone with write access; the AIMS
          anchor cannot, so reconciliation exposes even that.</div>
        <div className="row wrap">
          <button className="btn sm primary" disabled={aimsBusy} onClick={() => reconcile()}>⇄ Reconcile with Lyzr AIMS</button>
          <button className="btn sm danger" disabled={aimsBusy} onClick={() => reconcile(Math.min(5, data.entries.length))}>
            Simulate full re-hashed rewrite</button>
          {aimsBusy ? <span className="small muted">contacting Lyzr…</span> : null}
        </div>
        <ErrorBox error={aimsErr} />
        {aims && !aims.available ? <div className="note" style={{ marginTop: 10 }}>{aims.reason}</div> : null}
        {aims && aims.available ? (
          <div className="stack" style={{ marginTop: 10, gap: 8 }}>
            <div className="row wrap" style={{ gap: 8 }}>
              <div className={`verify-banner ${aims.local_chain.valid ? 'ok' : 'bad'}`} style={{ flex: 1 }}>
                {aims.simulated_rewrite ? 'Forged copy - ' : ''}local chain {aims.local_chain.valid ? '✔ verifies' : '✖ broken'}
              </div>
              <div className={`verify-banner ${aims.all_match ? 'ok' : 'bad'}`} style={{ flex: 1 }}>
                {!aims.anchored ? 'No anchors yet' : aims.all_match ? `✔ matches all ${aims.anchors.length} AIMS anchors`
                  : '✖ diverges from the AIMS anchors - tampering detected'}
              </div>
            </div>
            {aims.anchors.length ? (
              <table className="table">
                <thead><tr><th>Checkpoint</th><th className="num">Entries</th><th>Anchored head</th><th>Lyzr timestamp</th><th>Match</th></tr></thead>
                <tbody>{aims.anchors.map((a: any, i: number) => (
                  <tr key={i}><td>{String(a.reason).replace(/_/g, ' ')}</td><td className="num">{a.entries}</td>
                    <td className="hash">{shortHash(a.head, 16)}</td><td className="small mono">{a.lyzr_created_at ?? '-'}</td>
                    <td>{a.match ? <Badge tone="ok">✔</Badge> : <Badge tone="bad">✖</Badge>}</td></tr>
                ))}</tbody>
              </table>
            ) : null}
          </div>
        ) : null}
      </Card>
      <Card>
        <Label hint="click a row for the payload">Entries</Label>
        <div style={{ overflowX: 'auto' }}>
          <table className="table">
            <thead><tr><th>#</th><th>Time</th><th>Event</th><th>Actor</th><th>Visibility</th><th>Hash</th><th>Prev</th><th>AIMS</th></tr></thead>
            <tbody>
              {data.entries.map((e) => (
                <tr key={e.seq} className={`clickable ${open?.seq === e.seq ? 'selected' : ''}`} onClick={() => setOpen(open?.seq === e.seq ? null : e)}>
                  <td className="mono">{e.seq}</td>
                  <td className="mono small">{fmtTime(e.ts)}</td>
                  <td>{pretty(e.event_type)}</td>
                  <td className="small">{e.actor}</td>
                  <td>{e.visibility.map((x) => <Badge key={x} tone={x === 'buyer' ? 'buyer' : x === 'supplier' ? 'supplier' : x === 'arbiter' ? 'arbiter' : ''}>{x}</Badge>)}</td>
                  <td className="hash">{shortHash(e.hash, 10)}</td>
                  <td className="hash">{shortHash(e.prev_hash, 10)}</td>
                  <td><Badge tone={e.aims === 'synced' ? 'ok' : e.aims === 'failed' ? 'bad' : ''}>{e.aims}</Badge></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {open ? <div style={{ marginTop: 10 }}><Json value={open} maxHeight={360} /></div> : null}
      </Card>
    </div>
  );
}
