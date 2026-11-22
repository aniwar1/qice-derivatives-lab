import { useCallback, useEffect, useState } from "react";

export function useVisibleResource<T>(
  read: (signal: AbortSignal) => Promise<T>,
  enabled: boolean,
  interval = 60_000,
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);
  const refresh = useCallback(() => setVersion((n) => n + 1), []);
  useEffect(() => {
    if (!enabled) return;
    let controller: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const stop = () => {
      clearTimeout(timer);
      controller?.abort();
      controller = undefined;
    };
    async function load() {
      stop();
      if (document.hidden) return;
      const request = new AbortController();
      controller = request;
      setBusy(true);
      try {
        const next = await read(request.signal);
        if (!request.signal.aborted) {
          setData(next);
          setError("");
        }
      } catch (e) {
        if (!request.signal.aborted)
          setError(e instanceof Error ? e.message : "读取失败");
      } finally {
        if (!request.signal.aborted) {
          setBusy(false);
          if (interval > 0) timer = setTimeout(load, interval);
        }
      }
    }
    void load();
    document.addEventListener("visibilitychange", load);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", load);
    };
  }, [read, enabled, interval, version]);
  return { data, error, busy, refresh };
}
