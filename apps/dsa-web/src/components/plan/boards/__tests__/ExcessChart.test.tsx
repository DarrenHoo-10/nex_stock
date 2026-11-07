import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { BoardSeriesPoint } from '../../../../types/boards';
import { ExcessChart } from '../ExcessChart';

const SERIES: BoardSeriesPoint[] = [
  { tradeDate: '2026-11-03', excessPct: -0.1 },
  { tradeDate: '2026-11-04', excessPct: 0.5 },
  { tradeDate: '2026-11-05', excessPct: null },
  { tradeDate: '2026-11-06', excessPct: 3.48 },
];

describe('ExcessChart', () => {
  it('describes the series, draws a dot per known day and breaks the line at a missing day', () => {
    const { container } = render(<ExcessChart series={SERIES} />);
    const chart = screen.getByTestId('excess-chart');
    expect(chart).toHaveAttribute('aria-label', expect.stringContaining('11-06 +3.48%'));
    expect(chart).toHaveAttribute('aria-label', expect.stringContaining('11-05 —'));
    expect(container.querySelectorAll('circle.excess-dot')).toHaveLength(3);
    // -0.1 -> 0.5 is one segment; 3.48 alone has no line.
    expect(container.querySelectorAll('polyline.excess-line')).toHaveLength(1);
    expect(container.querySelectorAll('.zero-line')).toHaveLength(1);
    expect(screen.getByText('无')).toBeInTheDocument();
  });

  it('shows a tooltip with the keyboard and hides it with Escape', () => {
    render(<ExcessChart series={SERIES} />);
    const chart = screen.getByTestId('excess-chart');
    expect(screen.queryByTestId('excess-tooltip')).toBeNull();
    fireEvent.keyDown(chart, { key: 'ArrowLeft' });
    expect(screen.getByTestId('excess-tooltip')).toHaveTextContent('2026-11-06');
    expect(screen.getByTestId('excess-tooltip')).toHaveTextContent('+3.48%');
    fireEvent.keyDown(chart, { key: 'ArrowLeft' });
    expect(screen.getByTestId('excess-tooltip')).toHaveTextContent('2026-11-05');
    expect(screen.getByTestId('excess-tooltip')).toHaveTextContent('无数据');
    fireEvent.keyDown(chart, { key: 'Escape' });
    expect(screen.queryByTestId('excess-tooltip')).toBeNull();
  });

  it('renders a note instead of an empty chart', () => {
    render(<ExcessChart series={[]} />);
    expect(screen.getByText('没有近期的超额收益序列。')).toBeInTheDocument();
  });
});
