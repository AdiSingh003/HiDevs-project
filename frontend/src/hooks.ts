import { useCallback, useEffect, useRef, useState } from 'react';
import { streamRun } from './api';
import type { StreamEvent, View } from './types';

/** Live events of a run (replays history first). Re-subscribes when the run or the view changes. */
export function useRunStream(runId: string | null, view: View) {
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [live, setLive] = useState(false);
  const buffer = useRef<StreamEvent[]>([]);
  const frame = useRef<number | null>(null);

  useEffect(() => {
    setEvents([]);
    buffer.current = [];
    if (!runId) {
      setLive(false);
      return;
    }
    setLive(true);
    const flush = () => {
      frame.current = null;
      setEvents([...buffer.current]);
    };
    const stop = streamRun(runId, view, (ev) => {
      buffer.current.push(ev);
      if (frame.current === null) frame.current = window.requestAnimationFrame(flush);
    }, () => {
      setLive(false);
      if (frame.current !== null) window.cancelAnimationFrame(frame.current);
      flush();
    });
    return () => {
      stop();
      if (frame.current !== null) window.cancelAnimationFrame(frame.current);
      frame.current = null;
    };
  }, [runId, view]);

  return { events, live };
}

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(fn, deps);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    run().then((d) => { if (alive) { setData(d); setError(null); } })
      .catch((e: Error) => { if (alive) setError(e.message); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [run, tick]);
  return { data, error, loading, reload: () => setTick((t) => t + 1), setData };
}

export function useHashRoute(): [string[], (path: string) => void] {
  const parse = () => window.location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  const [parts, setParts] = useState<string[]>(parse());
  useEffect(() => {
    const onChange = () => setParts(parse());
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  return [parts, (path: string) => { window.location.hash = `/${path}`; }];
}
