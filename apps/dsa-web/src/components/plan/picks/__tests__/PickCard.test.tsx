import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { targetFromCode } from '../../quote/quoteTarget';
import { PickCard } from '../PickCard';
import type { PickView } from '../pickMeta';

const pick = {
  code: '000001',
  name: '平安银行',
  price: 11.81,
  changePct: 1.46,
  horizons: [],
  reasons: ['本地后置评分: value_quality'],
  entryCondition: null,
  invalidCondition: null,
  risks: [],
  note: null,
} as unknown as PickView;

describe('PickCard', () => {
  it('opens the chart from the stock identity', () => {
    const onOpen = vi.fn();
    render(
      <ul>
        <PickCard pick={pick} index={0} inWatchlist={false} adding={false} onAdd={vi.fn()} onAsk={vi.fn()} onOpen={onOpen} />
      </ul>,
    );
    fireEvent.click(screen.getByRole('button', { name: '查看 平安银行 的 K 线和筹码' }));
    expect(onOpen).toHaveBeenCalledWith(pick);
  });

  it('maps plan codes to chart targets', () => {
    expect(targetFromCode('000001', '平安银行')).toMatchObject({
      code: '000001',
      watchCode: '000001',
      name: '平安银行',
      market: 'CN',
      assetType: 'stock',
    });
    expect(targetFromCode('512880', '512880')).toMatchObject({ name: null, assetType: 'etf' });
  });
});
