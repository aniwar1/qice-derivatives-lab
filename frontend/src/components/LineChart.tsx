import { useEffect, useRef, useState } from "react";

export interface ChartSeries {
  label: string;
  color: string;
  values: number[];
}

const PAD = { left: 63, right: 21, top: 17, bottom: 32 };

export function LineChart({
  labels, series, format, baseline, title,
}: {
  labels: string[];
  series: ChartSeries[];
  format: (value: number) => string;
  baseline?: number;
  title: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const highlighted = Math.min(focus ?? labels.length - 1, labels.length - 1);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !series.length || !labels.length) return;
    const draw = () => {
      const { width, height } = canvas.getBoundingClientRect();
      if (!width || !height) return;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      const ratio = window.devicePixelRatio || 1;
      canvas.width = width * ratio;
      canvas.height = height * ratio;
      ctx.scale(ratio, ratio);
      const values = series.flatMap((item) => item.values);
      if (baseline !== undefined) values.push(baseline);
      const min = Math.min(...values);
      const max = Math.max(...values);
      const cushion = (max - min || Math.abs(max) * .02 || 1) * .09;
      const low = min - cushion;
      const high = max + cushion;
      const chartWidth = width - PAD.left - PAD.right;
      const chartHeight = height - PAD.top - PAD.bottom;
      const x = (index: number) => PAD.left + chartWidth * index / Math.max(1, labels.length - 1);
      const y = (value: number) => PAD.top + (high - value) / (high - low) * chartHeight;
      ctx.font = '11px "SFMono-Regular", Menlo, monospace';
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (let i = 0; i < 5; i++) {
        const value = high - i * (high - low) / 4;
        const row = y(value);
        ctx.strokeStyle = "#e4e9ef";
        ctx.beginPath(); ctx.moveTo(PAD.left, row); ctx.lineTo(width - PAD.right, row); ctx.stroke();
        ctx.fillStyle = "#586679"; ctx.fillText(format(value), PAD.left - 9, row);
      }
      if (baseline !== undefined) {
        ctx.strokeStyle = "#8595a6";
        ctx.setLineDash([5, 5]);
        ctx.beginPath(); ctx.moveTo(PAD.left, y(baseline)); ctx.lineTo(width - PAD.right, y(baseline)); ctx.stroke();
        ctx.setLineDash([]);
      }
      const ticks = Math.min(labels.length, width < 500 ? 4 : 6);
      ctx.textAlign = "center";
      for (let i = 0; i < ticks; i++) {
        const index = Math.round(i * (labels.length - 1) / Math.max(1, ticks - 1));
        ctx.fillText(labels[index].replace(/^\d{4}-/, ""), x(index), height - 14);
      }
      ctx.strokeStyle = "#b4c1cf";
      ctx.beginPath(); ctx.moveTo(x(highlighted), PAD.top); ctx.lineTo(x(highlighted), height - PAD.bottom); ctx.stroke();
      ctx.lineJoin = "round";
      for (const item of series) {
        ctx.strokeStyle = item.color; ctx.lineWidth = 2.4;
        ctx.beginPath();
        item.values.forEach((value, index) => {
          if (index === 0) ctx.moveTo(x(index), y(value));
          else ctx.lineTo(x(index), y(value));
        });
        ctx.stroke();
        ctx.fillStyle = item.color;
        ctx.beginPath(); ctx.arc(x(highlighted), y(item.values[highlighted]), 4.4, 0, Math.PI * 2); ctx.fill();
      }
    };
    draw();
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(draw) : null;
    observer?.observe(canvas);
    return () => observer?.disconnect();
  }, [labels, series, format, baseline, highlighted]);

  if (!series.length || !labels.length) return <p className="empty-note">暂无可绘制的数据。</p>;
  return (
    <figure className="line-chart" aria-label={title}>
      <canvas ref={canvasRef} tabIndex={0} aria-label={`${title}，左右方向键切换交易日`}
        onPointerMove={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          const value = (event.clientX - rect.left - PAD.left) / Math.max(1, rect.width - PAD.left - PAD.right);
          setFocus(Math.min(labels.length - 1, Math.max(0, Math.round(value * (labels.length - 1)))));
        }}
        onKeyDown={(event) => {
          if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
          event.preventDefault();
          setFocus(Math.min(labels.length - 1, Math.max(0, highlighted + (event.key === "ArrowRight" ? 1 : -1))));
        }} />
      <figcaption><time>{labels[highlighted]}</time>{series.map((item) => (
        <span key={item.label}><i style={{ background: item.color }} />{item.label} <strong>{format(item.values[highlighted])}</strong></span>
      ))}</figcaption>
    </figure>
  );
}
