import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buildConfigView } from '../usePreferencesConfig';
import { FeishuSettings } from '../FeishuSettings';

const { startFeishuRegistration, pollFeishuRegistration, toast } = vi.hoisted(() => ({
  startFeishuRegistration: vi.fn(),
  pollFeishuRegistration: vi.fn(),
  toast: vi.fn(),
}));

vi.mock('../../../../api/systemConfig', () => ({
  systemConfigApi: { startFeishuRegistration, pollFeishuRegistration },
}));
vi.mock('../../../../stores/planToastStore', () => ({ showPlanToast: toast }));

const flush = async (ms: number) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

describe('FeishuSettings scan-to-connect', () => {
  const onReload = vi.fn();
  const view = buildConfigView([], 'v1');

  beforeEach(() => {
    vi.useFakeTimers();
    startFeishuRegistration.mockResolvedValue({
      sessionId: 's1',
      verificationUrl: 'https://open.feishu.cn/page/launcher?user_code=AB',
      expiresIn: 600,
      interval: 2,
    });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  const renderIt = () => render(<FeishuSettings config={view} onSave={vi.fn()} onReload={onReload} />);

  it('shows the QR code, polls until authorised, then reloads the config', async () => {
    pollFeishuRegistration
      .mockResolvedValueOnce({ status: 'pending', interval: 2, restartRequired: false })
      .mockResolvedValueOnce({ status: 'success', restartRequired: false });
    renderIt();
    fireEvent.click(screen.getByTestId('feishu-scan-start'));
    await flush(0);
    expect(screen.getByTestId('feishu-qr').querySelector('svg')).not.toBeNull();

    await flush(2000);
    expect(onReload).not.toHaveBeenCalled();
    await flush(2000);
    expect(onReload).toHaveBeenCalledTimes(1);
    expect(toast).toHaveBeenCalledWith('飞书机器人已连接');
    expect(screen.getByTestId('feishu-scan-done').textContent).toContain('长连接已启动');
  });

  it('tells the user to restart when the long connection could not start', async () => {
    pollFeishuRegistration.mockResolvedValue({ status: 'success', restartRequired: true });
    renderIt();
    fireEvent.click(screen.getByTestId('feishu-scan-start'));
    await flush(2000);
    expect(screen.getByTestId('feishu-scan-done').textContent).toContain('重启服务');
  });

  it('reports a denied scan and allows retrying', async () => {
    pollFeishuRegistration.mockResolvedValue({ status: 'denied', restartRequired: false });
    renderIt();
    fireEvent.click(screen.getByTestId('feishu-scan-start'));
    await flush(2000);
    expect(screen.getByTestId('feishu-scan-error').textContent).toContain('取消了授权');
    expect(onReload).not.toHaveBeenCalled();
    expect(screen.getByTestId('feishu-scan-start')).toBeTruthy();
  });

  it('stops polling when the scan is cancelled', async () => {
    pollFeishuRegistration.mockResolvedValue({ status: 'pending', interval: 2, restartRequired: false });
    renderIt();
    fireEvent.click(screen.getByTestId('feishu-scan-start'));
    await flush(0);
    fireEvent.click(screen.getByText('取消扫码'));
    await flush(10000);
    expect(pollFeishuRegistration).not.toHaveBeenCalled();
  });
});
