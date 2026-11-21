import { useId } from "react";

interface HelpTipProps {
  label: string;
  children: React.ReactNode;
}

export function HelpTip({ label, children }: HelpTipProps) {
  const tooltipId = useId();
  return (
    <span className="help-tip">
      <button
        className="help-tip__trigger"
        type="button"
        aria-label={label}
        aria-describedby={tooltipId}
      >
        ?
      </button>
      <span className="help-tip__bubble" id={tooltipId} role="tooltip">
        {children}
      </span>
    </span>
  );
}
