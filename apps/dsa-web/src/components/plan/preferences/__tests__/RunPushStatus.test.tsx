import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { RunPush } from '../../../../types/dailyPlan';
import { RunPushStatus } from '../RunPushStatus';

function push(overrides: Partial<RunPush> = {}): RunPush {
  return { channel: 'feishu', status: 'sent', sentAt: '2026-11-06T09:05:00', error: null, configured: true, ...overrides };
}

const status = () => screen.getByTestId('push-status');

describe('RunPushStatus', () => {
  describe('primary user / local mode (system Feishu route, unchanged)', () => {
    it('keeps the Feishu wording for a sent push', () => {
      render(<RunPushStatus push={push()} />);

      expect(status()).toHaveTextContent(/^飞书推送：已发送（\d\d:\d\d）$/);
      expect(status()).not.toHaveAttribute('data-channel');
      expect(screen.queryByTestId('push-channels')).toBeNull();
    });

    it('treats a run without the channel field (older runs) as Feishu', () => {
      const legacy = { status: 'sent', sentAt: null, error: null, configured: true } as unknown as RunPush;
      render(<RunPushStatus push={legacy} />);

      expect(status()).toHaveTextContent('飞书推送：已发送');
    });

    it('says nothing was sent when Feishu is not configured, and shows a failure reason', () => {
      const { rerender } = render(<RunPushStatus push={push({ configured: false, status: 'skipped', sentAt: null })} />);
      expect(status()).toHaveTextContent('飞书推送：未配置，没有发送');

      rerender(<RunPushStatus push={push({ status: 'failed', sentAt: null, error: 'webhook 400' })} />);
      expect(status()).toHaveTextContent('飞书推送：发送失败：webhook 400');
    });
  });

  describe('personal channels', () => {
    const personal = (overrides: Partial<RunPush> = {}) => push({ channel: 'personal', ...overrides });

    it('shows the per-type results of a sent push', () => {
      render(
        <RunPushStatus
          push={personal({
            channels: [
              { channel: 'feishu', success: true },
              { channel: 'email', success: false },
              { channel: 'custom', success: true },
            ],
          })}
        />,
      );

      expect(status()).toHaveAttribute('data-channel', 'personal');
      expect(status()).toHaveTextContent('个人渠道推送：已发送');
      const channels = screen.getByTestId('push-channels');
      expect(within(channels).getByText('飞书：已送达')).toBeInTheDocument();
      expect(within(channels).getByText('邮件：发送失败')).toBeInTheDocument();
      expect(within(channels).getByText('自定义 Webhook：已送达')).toBeInTheDocument();
    });

    it('shows a total failure with its reason and the per-type results', () => {
      render(
        <RunPushStatus
          push={personal({
            status: 'failed',
            sentAt: null,
            error: '个人推送渠道发送失败，请检查渠道配置',
            channels: [{ channel: 'telegram', success: false }],
          })}
        />,
      );

      expect(status()).toHaveTextContent('个人渠道推送：发送失败：个人推送渠道发送失败，请检查渠道配置');
      expect(within(screen.getByTestId('push-channels')).getByText('Telegram：发送失败')).toBeInTheDocument();
    });

    it('names a channel type it does not know as it came', () => {
      render(<RunPushStatus push={personal({ channels: [{ channel: 'matrix', success: true }] })} />);

      expect(within(screen.getByTestId('push-channels')).getByText('matrix：已送达')).toBeInTheDocument();
    });

    it('explains that the plan is only on the web when the user has no usable channel', () => {
      render(<RunPushStatus push={personal({ configured: false, status: 'skipped', sentAt: null })} />);

      expect(status()).toHaveTextContent('没有可用的推送渠道，计划只在网页里查看');
      expect(status()).not.toHaveTextContent('飞书');
    });

    it.each([true, false])('explains the missing model (configured=%s) instead of a channel result', (configured) => {
      render(<RunPushStatus push={personal({ configured, status: 'skipped', sentAt: null, reason: 'no_model' })} />);

      expect(status()).toHaveTextContent('未配置模型');
      expect(status()).toHaveTextContent('按规则生成并保存');
      expect(status()).not.toHaveTextContent('没有可用的推送渠道');
    });
  });
});
