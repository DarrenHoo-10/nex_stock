/**
 * Chip (cost) distribution — the CYQ model used by Chinese trading terminals.
 *
 * Every day the existing chips decay by that day's turnover, and the traded share (= turnover) is laid
 * onto the day's [low, high] range as a triangle peaking at the day's average price. The result is a
 * histogram of holding cost over a fixed price grid.
 */

export interface ChipInputBar {
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
  amount: number | null;
  /** Percent, e.g. 2.5 for 2.5%. */
  turnoverRate: number | null;
}

export interface ChipGrid {
  minPrice: number;
  step: number;
  size: number;
}

export interface ChipCostRange {
  low: number;
  high: number;
  /** (high - low) / (high + low); smaller is more concentrated. */
  concentration: number;
}

export interface ChipResult {
  grid: ChipGrid;
  /** Normalized share per bucket (sums to 1). */
  chips: number[];
  /** Close of the target day. */
  close: number;
  /** Share of chips with cost at or below the close (0..1). */
  profitRatio: number;
  avgCost: number;
  range90: ChipCostRange;
  range70: ChipCostRange;
}

export const DEFAULT_CHIP_BUCKETS = 160;

/** Price grid that covers every bar so distributions of different days are comparable. */
export function buildChipGrid(bars: ChipInputBar[], size = DEFAULT_CHIP_BUCKETS): ChipGrid | null {
  let min = Infinity;
  let max = -Infinity;
  for (const bar of bars) {
    if (bar.low > 0 && bar.low < min) {
      min = bar.low;
    }
    if (bar.high > max) {
      max = bar.high;
    }
  }
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= 0) {
    return null;
  }
  if (max - min < 1e-9) {
    max = min * 1.01 + 1e-6;
  }
  return { minPrice: min, step: (max - min) / (size - 1), size };
}

export function bucketPrice(grid: ChipGrid, index: number): number {
  return grid.minPrice + grid.step * index;
}

function bucketIndex(grid: ChipGrid, price: number): number {
  const index = Math.round((price - grid.minPrice) / grid.step);
  return Math.min(grid.size - 1, Math.max(0, index));
}

function averagePrice(bar: ChipInputBar): number {
  if (bar.amount && bar.volume && bar.volume > 0) {
    const avg = bar.amount / bar.volume;
    // Units of amount/volume differ between sources; only trust it inside the day's range.
    if (avg >= bar.low && avg <= bar.high) {
      return avg;
    }
  }
  return (bar.open + bar.high + bar.low + bar.close) / 4;
}

function addTriangle(chips: number[], grid: ChipGrid, bar: ChipInputBar, share: number): void {
  const lowIdx = bucketIndex(grid, bar.low);
  const highIdx = bucketIndex(grid, bar.high);
  if (highIdx <= lowIdx) {
    chips[lowIdx] += share;
    return;
  }
  const avg = averagePrice(bar);
  const low = bucketPrice(grid, lowIdx);
  const high = bucketPrice(grid, highIdx);
  const peak = Math.min(high, Math.max(low, avg));
  const weights: number[] = [];
  let total = 0;
  for (let i = lowIdx; i <= highIdx; i += 1) {
    const price = bucketPrice(grid, i);
    let weight: number;
    if (price <= peak) {
      weight = peak > low ? (price - low) / (peak - low) : 1;
    } else {
      weight = high > peak ? (high - price) / (high - peak) : 1;
    }
    // Keep the edges non-zero so one-tick days still contribute.
    weight = Math.max(weight, 0.02);
    weights.push(weight);
    total += weight;
  }
  for (let i = 0; i < weights.length; i += 1) {
    chips[lowIdx + i] += (weights[i] / total) * share;
  }
}

function costAtPercentile(chips: number[], grid: ChipGrid, target: number): number {
  let cumulative = 0;
  for (let i = 0; i < chips.length; i += 1) {
    cumulative += chips[i];
    if (cumulative >= target) {
      return bucketPrice(grid, i);
    }
  }
  return bucketPrice(grid, chips.length - 1);
}

function costRange(chips: number[], grid: ChipGrid, percent: number): ChipCostRange {
  const tail = (1 - percent) / 2;
  const low = costAtPercentile(chips, grid, tail);
  const high = costAtPercentile(chips, grid, 1 - tail);
  return { low, high, concentration: high + low > 0 ? (high - low) / (high + low) : 0 };
}

/**
 * Chip distribution as of `targetIndex` (inclusive). Bars without turnover are skipped.
 * Returns null when no bar up to the target carries turnover.
 */
export function computeChipDistribution(
  bars: ChipInputBar[],
  targetIndex: number,
  grid: ChipGrid,
): ChipResult | null {
  if (targetIndex < 0 || targetIndex >= bars.length) {
    return null;
  }
  const chips = new Array<number>(grid.size).fill(0);
  let seeded = false;
  for (let i = 0; i <= targetIndex; i += 1) {
    const bar = bars[i];
    const rate = bar.turnoverRate;
    if (rate === null || !(rate > 0) || !(bar.high > 0)) {
      continue;
    }
    // The first bar seeds the whole float so early history is not empty.
    const turnover = seeded ? Math.min(rate / 100, 1) : 1;
    if (seeded) {
      for (let k = 0; k < chips.length; k += 1) {
        chips[k] *= 1 - turnover;
      }
    }
    addTriangle(chips, grid, bar, turnover);
    seeded = true;
  }
  const total = chips.reduce((sum, value) => sum + value, 0);
  if (!seeded || total <= 0) {
    return null;
  }
  for (let k = 0; k < chips.length; k += 1) {
    chips[k] /= total;
  }

  const close = bars[targetIndex].close;
  let profit = 0;
  let weighted = 0;
  for (let k = 0; k < chips.length; k += 1) {
    const price = bucketPrice(grid, k);
    weighted += price * chips[k];
    if (price <= close) {
      profit += chips[k];
    }
  }
  return {
    grid,
    chips,
    close,
    profitRatio: Math.min(1, Math.max(0, profit)),
    avgCost: weighted,
    range90: costRange(chips, grid, 0.9),
    range70: costRange(chips, grid, 0.7),
  };
}
