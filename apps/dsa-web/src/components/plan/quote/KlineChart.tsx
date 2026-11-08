import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { bucketPrice } from './chipDistribution';
import type { ChipResult } from './chipDistribution';
import { PRICE_MA_WINDOWS, VOLUME_MA_WINDOWS, formatVolume, movingAverage } from './klineMath';
import type { ChartBar, KlinePeriod } from './klineMath';
import { CHART_COLORS } from './chartTheme';
import { KlineBarTip } from './KlineBarTip';


const AXIS_W = 62;
const DATE_H = 22;
const PANE_GAP = 10;
const MIN_VISIBLE = 20;
const DEFAULT_VISIBLE = 120;

/** Remount (change `key`) when the series changes; the viewport is initialized once per mount. */
export interface KlineChartProps {
  bars: ChartBar[];
  period: KlinePeriod;
  digits: number;
  /** Chip distribution for the bar under the crosshair (or the last bar). */
  chip: ChipResult | null;
  /** Hovered bar index (into `bars`), null when the pointer is outside. */
  onHoverChange: (index: number | null) => void;
  height?: number;
  /** Double-click on a candle (index into `bars`); omitted keeps the chart passive. */
  onBarDoubleClick?: (index: number) => void;
  ariaLabel: string;
}

interface View {
  count: number;
  end: number;
}

interface Layout {
  width: number;
  height: number;
  plotW: number;
  chipW: number;
  mainTop: number;
  mainH: number;
  volTop: number;
  volH: number;
}

function computeLayout(width: number, height: number): Layout {
  const chipW = width >= 720 ? Math.min(180, Math.round(width * 0.2)) : 0;
  const plotW = Math.max(80, width - AXIS_W - chipW);
  const body = height - DATE_H;
  const mainH = Math.round(body * 0.72);
  return {
    width,
    height,
    plotW,
    chipW,
    mainTop: 8,
    mainH: mainH - 8,
    volTop: mainH + PANE_GAP,
    volH: body - mainH - PANE_GAP,
  };
}

function clampView(view: View, total: number): View {
  const count = Math.max(Math.min(MIN_VISIBLE, total), Math.min(total, Math.round(view.count)));
  const end = Math.min(total - 1, Math.max(count - 1, Math.round(view.end)));
  return { count, end };
}

function formatDate(date: string, period: KlinePeriod): string {
  return period === 'monthly' ? date.slice(0, 7) : date.slice(0, 10);
}

export const KlineChart: React.FC<KlineChartProps> = ({
  bars,
  period,
  digits,
  chip,
  onHoverChange,
  height = 460,
  onBarDoubleClick,
  ariaLabel,
}) => {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(0);
  const total = bars.length;
  const [view, setView] = useState<View>(() => ({ count: DEFAULT_VISIBLE, end: total - 1 }));
  const [hover, setHover] = useState<{ index: number; y: number | null } | null>(null);
  const dragRef = useRef<{ x: number; end: number; moved: boolean } | null>(null);

  const safeView = clampView(view, total);
  const start = safeView.end - safeView.count + 1;
  const layout = useMemo(() => computeLayout(width, height), [width, height]);

  const priceMa = useMemo(() => {
    const closes = bars.map((bar) => bar.close);
    return PRICE_MA_WINDOWS.map((window) => movingAverage(closes, window));
  }, [bars]);
  const volumeMa = useMemo(() => {
    const volumes = bars.map((bar) => bar.volume);
    return VOLUME_MA_WINDOWS.map((window) => movingAverage(volumes, window));
  }, [bars]);

  useEffect(() => {
    const element = wrapRef.current;
    if (!element) {
      return undefined;
    }
    setWidth(element.clientWidth);
    if (typeof ResizeObserver === 'undefined') {
      return undefined;
    }
    const observer = new ResizeObserver((entries) => {
      setWidth(Math.round(entries[0].contentRect.width));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const hoverIndex = hover?.index ?? null;
  useEffect(() => {
    onHoverChange(hoverIndex);
  }, [hoverIndex, onHoverChange]);

  // ---- drawing -------------------------------------------------------------------------------
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || layout.width <= 0 || total === 0) {
      return;
    }
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(layout.width * dpr);
    canvas.height = Math.round(layout.height * dpr);
    const ctx = canvas.getContext('2d');
    if (!ctx) {
      return;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, layout.width, layout.height);
    ctx.font = '11px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif';
    ctx.textBaseline = 'middle';

    const { plotW, mainTop, mainH, volTop, volH, chipW } = layout;
    const barW = plotW / safeView.count;
    const xOf = (index: number) => (index - start + 0.5) * barW;

    // Price range over the visible bars and their moving averages.
    let hi = -Infinity;
    let lo = Infinity;
    let hiIdx = start;
    let loIdx = start;
    let maxVol = 0;
    for (let i = start; i <= safeView.end; i += 1) {
      const bar = bars[i];
      if (bar.high > hi) {
        hi = bar.high;
        hiIdx = i;
      }
      if (bar.low < lo) {
        lo = bar.low;
        loIdx = i;
      }
      maxVol = Math.max(maxVol, bar.volume ?? 0);
    }
    let top = hi;
    let bottom = lo;
    for (const series of priceMa) {
      for (let i = start; i <= safeView.end; i += 1) {
        const value = series[i];
        if (value !== null) {
          top = Math.max(top, value);
          bottom = Math.min(bottom, value);
        }
      }
    }
    const pad = (top - bottom) * 0.06 || top * 0.01 || 1;
    top += pad;
    bottom -= pad;
    const yOf = (price: number) => mainTop + ((top - price) / (top - bottom)) * mainH;
    const priceAt = (y: number) => top - ((y - mainTop) / mainH) * (top - bottom);
    const volY = (volume: number) => volTop + volH - (maxVol > 0 ? (volume / maxVol) * volH : 0);

    // Grid + price axis.
    ctx.strokeStyle = CHART_COLORS.grid;
    ctx.lineWidth = 1;
    ctx.fillStyle = CHART_COLORS.text;
    ctx.textAlign = 'left';
    for (let k = 0; k <= 4; k += 1) {
      const y = Math.round(mainTop + (mainH * k) / 4) + 0.5;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(plotW, y);
      ctx.stroke();
      ctx.fillText(priceAt(y).toFixed(digits), plotW + 6, y);
    }
    ctx.beginPath();
    ctx.moveTo(0, volTop - 0.5);
    ctx.lineTo(plotW, volTop - 0.5);
    ctx.moveTo(0, volTop + volH + 0.5);
    ctx.lineTo(plotW, volTop + volH + 0.5);
    ctx.stroke();
    ctx.fillStyle = CHART_COLORS.faint;
    ctx.fillText(formatVolume(maxVol), plotW + 6, volTop + 6);

    // Candles and volume.
    const bodyW = Math.max(1, barW * 0.68);
    for (let i = start; i <= safeView.end; i += 1) {
      const bar = bars[i];
      const up = bar.close >= bar.open;
      const color = up ? CHART_COLORS.up : CHART_COLORS.down;
      const x = xOf(i);
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, yOf(bar.high));
      ctx.lineTo(Math.round(x) + 0.5, yOf(bar.low));
      ctx.stroke();
      const yOpen = yOf(bar.open);
      const yClose = yOf(bar.close);
      const bodyTop = Math.min(yOpen, yClose);
      const bodyH = Math.max(1, Math.abs(yOpen - yClose));
      if (up && bodyW > 3) {
        // Hollow red body for up days, like the classic terminals.
        ctx.fillStyle = 'rgba(30,36,74,.92)';
        ctx.fillRect(x - bodyW / 2, bodyTop, bodyW, bodyH);
        ctx.strokeRect(x - bodyW / 2 + 0.5, bodyTop + 0.5, bodyW - 1, Math.max(0, bodyH - 1));
      } else {
        ctx.fillRect(x - bodyW / 2, bodyTop, bodyW, bodyH);
      }
      if (bar.volume) {
        ctx.fillStyle = up ? 'rgba(255,116,104,.75)' : 'rgba(60,195,180,.75)';
        const y = volY(bar.volume);
        ctx.fillRect(x - bodyW / 2, y, bodyW, volTop + volH - y);
      }
    }

    const drawLine = (series: Array<number | null>, color: string, mapY: (value: number) => number) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      let started = false;
      for (let i = start; i <= safeView.end; i += 1) {
        const value = series[i];
        if (value === null) {
          started = false;
          continue;
        }
        const x = xOf(i);
        const y = mapY(value);
        if (started) {
          ctx.lineTo(x, y);
        } else {
          ctx.moveTo(x, y);
          started = true;
        }
      }
      ctx.stroke();
      ctx.lineWidth = 1;
    };
    priceMa.forEach((series, k) => drawLine(series, CHART_COLORS.ma[k], yOf));
    volumeMa.forEach((series, k) => drawLine(series, CHART_COLORS.volMa[k], volY));

    // Highest / lowest markers.
    const marker = (index: number, price: number, above: boolean) => {
      const x = xOf(index);
      const y = yOf(price) + (above ? -7 : 7);
      const label = price.toFixed(digits);
      const leftSide = x > plotW * 0.7;
      ctx.fillStyle = CHART_COLORS.text;
      ctx.strokeStyle = CHART_COLORS.text;
      ctx.textAlign = leftSide ? 'right' : 'left';
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x + (leftSide ? -14 : 14), y);
      ctx.stroke();
      ctx.fillText(label, x + (leftSide ? -17 : 17), y);
      ctx.textAlign = 'left';
    };
    marker(hiIdx, hi, true);
    marker(loIdx, lo, false);

    // Date axis.
    ctx.fillStyle = CHART_COLORS.faint;
    ctx.textAlign = 'center';
    const every = Math.max(1, Math.ceil(110 / barW));
    for (let i = start; i <= safeView.end; i += every) {
      const x = xOf(i);
      if (x < 34 || x > plotW - 34) {
        continue;
      }
      ctx.fillText(formatDate(bars[i].date, period), x, layout.height - DATE_H / 2);
    }

    // Chip distribution panel, sharing the main pane's price scale.
    if (chipW > 0 && chip) {
      const left = plotW + AXIS_W;
      const maxChip = Math.max(...chip.chips);
      const rowH = Math.max(1, (chip.grid.step / (top - bottom)) * mainH);
      ctx.save();
      ctx.beginPath();
      ctx.rect(left, mainTop, chipW, mainH);
      ctx.clip();
      for (let k = 0; k < chip.chips.length; k += 1) {
        const value = chip.chips[k];
        if (value <= 0 || maxChip <= 0) {
          continue;
        }
        const price = bucketPrice(chip.grid, k);
        const y = yOf(price);
        if (y < mainTop - rowH || y > mainTop + mainH + rowH) {
          continue;
        }
        const length = (value / maxChip) * (chipW - 8);
        ctx.fillStyle = price <= chip.close ? CHART_COLORS.chipProfit : CHART_COLORS.chipTrapped;
        ctx.fillRect(left + 4, y - Math.max(0.5, rowH * 0.42), length, Math.max(1, rowH * 0.84));
      }
      const avgY = yOf(chip.avgCost);
      ctx.strokeStyle = CHART_COLORS.avgCost;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(left, avgY);
      ctx.lineTo(left + chipW, avgY);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();
      ctx.strokeStyle = CHART_COLORS.grid;
      ctx.strokeRect(left + 0.5, mainTop + 0.5, chipW - 1, mainH - 1);
    }

    // Crosshair: the vertical line snaps to the hovered candle, the horizontal line follows the pointer
    // (or sits on the close when driven by the keyboard) and carries a price / volume label on both edges.
    if (hover && hover.index >= start && hover.index <= safeView.end) {
      const fullW = plotW + AXIS_W + chipW;
      const x = Math.round(xOf(hover.index)) + 0.5;
      ctx.setLineDash([]);
      ctx.lineWidth = 1;
      ctx.strokeStyle = CHART_COLORS.crossLine;
      ctx.beginPath();
      ctx.moveTo(x, mainTop);
      ctx.lineTo(x, volTop + volH);
      ctx.stroke();

      const drawTag = (text: string, centerX: number, centerY: number, align: 'left' | 'right' | 'center') => {
        const w = ctx.measureText(text).width + 12;
        const h = 18;
        let left = centerX - w / 2;
        if (align === 'left') {
          left = centerX;
        } else if (align === 'right') {
          left = centerX - w;
        }
        left = Math.max(0, Math.min(fullW - w, left));
        ctx.fillStyle = CHART_COLORS.crossLabel;
        ctx.fillRect(left, centerY - h / 2, w, h);
        ctx.fillStyle = '#fff';
        ctx.textAlign = 'left';
        ctx.fillText(text, left + 6, centerY);
      };

      const bar = bars[hover.index];
      const dateText = formatDate(bar.date, period);
      drawTag(dateText, x, layout.height - DATE_H / 2, 'center');

      let y: number | null = hover.y;
      if (y === null) {
        y = yOf(bar.close);
      }
      const inMain = y >= mainTop && y <= mainTop + mainH;
      const inVol = y >= volTop && y <= volTop + volH;
      if (inMain || inVol) {
        const lineY = Math.round(y) + 0.5;
        ctx.strokeStyle = CHART_COLORS.crossLine;
        ctx.beginPath();
        ctx.moveTo(0, lineY);
        ctx.lineTo(fullW, lineY);
        ctx.stroke();
        const value = inMain
          ? priceAt(y).toFixed(digits)
          : formatVolume((maxVol * (volTop + volH - y)) / Math.max(1, volH));
        drawTag(value, 0, lineY, 'left');
        drawTag(value, plotW + 1, lineY, 'left');
      }
    }
  }, [bars, chip, digits, hover, layout, period, priceMa, safeView.count, safeView.end, start, total, volumeMa]);

  // ---- interaction ---------------------------------------------------------------------------
  const indexAt = useCallback(
    (clientX: number): number | null => {
      const canvas = canvasRef.current;
      if (!canvas || total === 0) {
        return null;
      }
      const x = clientX - canvas.getBoundingClientRect().left;
      if (x < 0 || x > layout.plotW) {
        return null;
      }
      const barW = layout.plotW / safeView.count;
      return Math.min(safeView.end, Math.max(start, start + Math.floor(x / barW)));
    },
    [layout.plotW, safeView.count, safeView.end, start, total],
  );

  const handlePointerMove = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }
    const drag = dragRef.current;
    if (drag) {
      const barW = layout.plotW / safeView.count;
      const shift = Math.round((event.clientX - drag.x) / barW);
      if (shift !== 0) {
        drag.moved = true;
      }
      setView((current) => clampView({ count: current.count, end: drag.end - shift }, total));
    }
    const index = indexAt(event.clientX);
    const y = event.clientY - canvas.getBoundingClientRect().top;
    setHover(index === null ? null : { index, y });
  };

  const handlePointerDown = (event: React.PointerEvent<HTMLCanvasElement>) => {
    if (event.button !== 0) {
      return;
    }
    dragRef.current = { x: event.clientX, end: safeView.end, moved: false };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const endDrag = (event: React.PointerEvent<HTMLCanvasElement>) => {
    dragRef.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  };

  // Wheel zoom needs a non-passive listener to stop the dialog from scrolling.
  const zoom = useCallback(
    (deltaY: number, clientX: number | null) => {
    const anchor = clientX === null ? null : indexAt(clientX);
    setView((current) => {
      const clamped = clampView(current, total);
      const factor = deltaY > 0 ? 1.18 : 1 / 1.18;
      const count = Math.max(Math.min(MIN_VISIBLE, total), Math.min(total, Math.round(clamped.count * factor)));
      if (anchor === null) {
        return clampView({ count, end: clamped.end }, total);
      }
      const ratio = (clamped.end - anchor) / clamped.count;
      return clampView({ count, end: anchor + Math.round(ratio * count) }, total);
    });
    },
    [indexAt, total],
  );
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return undefined;
    }
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      zoom(event.deltaY, event.clientX);
    };
    canvas.addEventListener('wheel', onWheel, { passive: false });
    return () => canvas.removeEventListener('wheel', onWheel);
  }, [zoom]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLCanvasElement>) => {
    if (total === 0) {
      return;
    }
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault();
      zoom(event.key === 'ArrowUp' ? -1 : 1, null);
      return;
    }
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') {
      return;
    }
    event.preventDefault();
    const current = hover?.index ?? safeView.end;
    const next = Math.min(total - 1, Math.max(0, current + (event.key === 'ArrowLeft' ? -1 : 1)));
    if (next < start) {
      setView({ count: safeView.count, end: next + safeView.count - 1 });
    } else if (next > safeView.end) {
      setView({ count: safeView.count, end: next });
    }
    setHover({ index: next, y: null });
  };

  // The info panel sits on the side away from the cursor so it never covers the hovered candle.
  const tipBar = hover && hover.index >= start && hover.index <= safeView.end ? bars[hover.index] : null;
  const tipOnRight = hover ? (hover.index - start + 0.5) * (layout.plotW / safeView.count) < layout.plotW / 2 : false;

  return (
    <div ref={wrapRef} className="kc-wrap" style={{ height }}>
      <canvas
        ref={canvasRef}
        className="kc-canvas"
        style={{ width: '100%', height }}
        role="img"
        aria-label={ariaLabel}
        tabIndex={0}
        onPointerMove={handlePointerMove}
        onPointerDown={handlePointerDown}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onDoubleClick={(event) => {
          const index = indexAt(event.clientX);
          if (index !== null) {
            onBarDoubleClick?.(index);
          }
        }}
        onPointerLeave={() => {
          if (!dragRef.current) {
            setHover(null);
          }
        }}
        onKeyDown={handleKeyDown}
        onBlur={() => setHover(null)}
      />
      {tipBar ? (
        <KlineBarTip
          bar={tipBar}
          prevClose={hover && hover.index > 0 ? bars[hover.index - 1].close : null}
          digits={digits}
          period={period}
          style={tipOnRight ? { right: AXIS_W + layout.chipW + 8, top: 8 } : { left: 8, top: 8 }}
        />
      ) : null}
    </div>
  );
};
