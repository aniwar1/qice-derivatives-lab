export function ResourceStatus({ error, reload, pending }: {
  error: string; reload: () => void; pending: boolean;
}) {
  if (error) return <div className="notice" role="alert">{error}<button onClick={reload}>重试</button></div>;
  return pending ? <p className="quiet" role="status">正在读取数据…</p> : null;
}
