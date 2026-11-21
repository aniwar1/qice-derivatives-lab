interface AsyncStateProps {
  kind: "loading" | "empty" | "error";
  title?: string;
  message?: string;
  onAction?: () => void;
  busy?: boolean;
}

export function AsyncState({ kind, title, message, onAction, busy }: AsyncStateProps) {
  if (kind === "loading") {
    return (
      <main className="async-state" aria-busy="true" aria-label="正在载入最新策略">
        <div className="loading-line loading-line--short" />
        <div className="loading-line loading-line--title" />
        <div className="loading-grid">
          <div className="loading-block" />
          <div className="loading-block" />
          <div className="loading-block" />
        </div>
      </main>
    );
  }

  return (
    <main className={`async-state async-state--${kind}`}>
      <span className="state-glyph" aria-hidden="true">{kind === "error" ? "!" : "○"}</span>
      <h1>{title || (kind === "error" ? "暂时无法读取策略" : "还没有最新策略")}</h1>
      <p>{message || (kind === "error" ? "检查本地服务后重试。" : "运行一次全市场更新来生成第一版结论。")}</p>
      {onAction && (
        <button className="primary-action" type="button" onClick={onAction} disabled={busy}>
          {busy ? "策略更新中…" : kind === "error" ? "重新连接" : "更新最新策略"}
        </button>
      )}
    </main>
  );
}
