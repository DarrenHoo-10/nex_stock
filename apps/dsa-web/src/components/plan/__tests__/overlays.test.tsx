import { act, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { usePlanToastStore } from '../../../stores/planToastStore';
import { CountUp } from '../motion/CountUp';
import { formatCount } from '../motion/formatCount';
import { SegmentedControl } from '../motion/SegmentedControl';
import { GlassDialog } from '../GlassDialog';
import { GlassDrawer } from '../GlassDrawer';
import { PlanToast } from '../PlanToast';

function Harness({ onCloseSpy }: { onCloseSpy?: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <UiLanguageProvider>
      <button type="button" onClick={() => setOpen(true)}>
        open
      </button>
      <GlassDialog
        open={open}
        title="运行状态"
        onClose={() => {
          onCloseSpy?.();
          setOpen(false);
        }}
      >
        <button type="button">inside</button>
      </GlassDialog>
    </UiLanguageProvider>
  );
}

afterEach(() => {
  vi.useRealTimers();
  act(() => usePlanToastStore.getState().dismiss());
});

describe('GlassDialog', () => {
  it('exposes a labelled modal dialog, focuses inside and returns focus on Escape', async () => {
    render(<Harness />);
    const opener = screen.getByRole('button', { name: 'open' });
    opener.focus();
    fireEvent.click(opener);

    const dialog = await screen.findByRole('dialog', { name: '运行状态' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(document, { key: 'Escape' });
    // The exit animation keeps the node briefly; focus returns once it unmounts.
    await vi.waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(document.activeElement).toBe(opener);
  });

  it('closes from the backdrop but not from clicks inside the sheet', async () => {
    const spy = vi.fn();
    render(<Harness onCloseSpy={spy} />);
    fireEvent.click(screen.getByRole('button', { name: 'open' }));
    const dialog = await screen.findByRole('dialog');

    fireEvent.mouseDown(screen.getByRole('button', { name: 'inside' }));
    expect(spy).not.toHaveBeenCalled();

    fireEvent.mouseDown(dialog.parentElement as HTMLElement);
    expect(spy).toHaveBeenCalledTimes(1);
  });
});

describe('GlassDrawer', () => {
  it('renders an aria-modal drawer with the given id and closes on Escape', async () => {
    const onClose = vi.fn();
    render(
      <UiLanguageProvider>
        <GlassDrawer open title="助手" onClose={onClose}>
          <p>body</p>
        </GlassDrawer>
      </UiLanguageProvider>,
    );
    const drawer = await screen.findByRole('dialog', { name: '助手' });
    expect(drawer).toHaveAttribute('id', 'assistant');
    expect(drawer).toHaveAttribute('aria-modal', 'true');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });
});

describe('PlanToast', () => {
  it('shows one toast at a time in a status region and clears it after the duration', () => {
    vi.useFakeTimers();
    render(<PlanToast />);
    const region = screen.getByRole('status');
    expect(region).toBeEmptyDOMElement();

    act(() => usePlanToastStore.getState().show('已保存', 2400));
    expect(region).toHaveTextContent('已保存');
    act(() => usePlanToastStore.getState().show('已更新', 2400));
    expect(usePlanToastStore.getState().toast?.message).toBe('已更新');

    act(() => {
      vi.advanceTimersByTime(2500);
    });
    expect(usePlanToastStore.getState().toast).toBeNull();
  });
});

describe('CountUp', () => {
  it('formats fixed decimals with sign and suffix and always renders the final value', () => {
    expect(formatCount(98700, 0)).toBe('98,700');
    expect(formatCount(1.5, 2, true)).toBe('+1.50');
    expect(formatCount(-9870, 0, true)).toBe('-9,870');

    const { container, rerender } = render(<CountUp value={47} decimals={2} suffix="元" />);
    expect(container).toHaveTextContent('47.00元');
    rerender(<CountUp value={null} />);
    expect(container).toHaveTextContent('—');
  });

  it('settles on the new value after an update', async () => {
    const { container, rerender } = render(<CountUp value={100} />);
    rerender(<CountUp value={2100} duration={0.05} />);
    await vi.waitFor(() => expect(container).toHaveTextContent('2,100'));
  });
});

describe('SegmentedControl', () => {
  it('marks the selected option and reports changes', () => {
    const onChange = vi.fn();
    render(
      <SegmentedControl
        ariaLabel="周期"
        value="short"
        onChange={onChange}
        options={[
          { value: 'short', label: '短线' },
          { value: 'swing', label: '波段' },
        ]}
      />,
    );
    expect(screen.getByRole('button', { name: '短线' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: '波段' }));
    expect(onChange).toHaveBeenCalledWith('swing');
  });
});
