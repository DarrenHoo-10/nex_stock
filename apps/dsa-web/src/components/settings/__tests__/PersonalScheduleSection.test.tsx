import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import type { PersonalChannelsState } from '../../../hooks/usePersonalChannels';
import {
  channelsResponse,
  feishuChannel,
  scheduleResponse,
} from '../../../test-utils/personalChannelFixtures';
import type { PersonalSettingField, PersonalSettingsResponse } from '../../../types/me';
import { PersonalScheduleSection } from '../personal/PersonalScheduleSection';

const api = vi.hoisted(() => ({
  getSchedule: vi.fn(),
  updateSettings: vi.fn(),
  resetSetting: vi.fn(),
}));

vi.mock('../../../api/me', () => ({ meApi: api }));

function field(overrides: Partial<PersonalSettingField> & Pick<PersonalSettingField, 'key'>): PersonalSettingField {
  return {
    title: overrides.key,
    description: '',
    group: 'push',
    dataType: 'string',
    uiControl: 'text',
    options: [],
    isSecret: false,
    value: '',
    source: 'system_default',
    inherits: true,
    configured: true,
    editable: true,
    updatedAt: null,
    ...overrides,
  };
}

const defaultFields = () => ({
  DAILY_PLAN_ENABLED: field({ key: 'DAILY_PLAN_ENABLED', dataType: 'boolean', uiControl: 'switch' }),
  DAILY_PLAN_TIME: field({ key: 'DAILY_PLAN_TIME' }),
  PREOPEN_PUSH_ENABLED: field({ key: 'PREOPEN_PUSH_ENABLED', dataType: 'boolean', uiControl: 'switch' }),
  PREOPEN_PUSH_TIME: field({ key: 'PREOPEN_PUSH_TIME' }),
});

const settingsResponse = (): PersonalSettingsResponse => ({
  userId: 'u-alice',
  isPrimary: false,
  managedInSystemSettings: false,
  groups: [],
});

function channelState(data: ReturnType<typeof channelsResponse> | null, overrides: Partial<PersonalChannelsState> = {}): PersonalChannelsState {
  return {
    data,
    isLoading: false,
    loadError: null,
    reload: vi.fn().mockResolvedValue(undefined),
    replaceAll: vi.fn(),
    upsert: vi.fn(),
    ...overrides,
  };
}

function renderSection(options: {
  fields?: Record<string, PersonalSettingField | undefined>;
  channels?: PersonalChannelsState;
  readOnly?: boolean;
  onAddChannel?: () => void;
} = {}) {
  const onSettingsChanged = vi.fn();
  const view = render(
    <MemoryRouter>
      <UiLanguageProvider>
        <PersonalScheduleSection
          title="推送与定时"
          description="说明"
          fields={options.fields ?? defaultFields()}
          channels={options.channels ?? channelState(channelsResponse())}
          readOnly={options.readOnly ?? false}
          onSettingsChanged={onSettingsChanged}
          onAddChannel={options.onAddChannel}
        />
      </UiLanguageProvider>
    </MemoryRouter>,
  );
  return { onSettingsChanged, ...view };
}

const rejection = (status: number, data: unknown) => ({ response: { status, data } });
const planTime = () => document.getElementById('schedule-DAILY_PLAN_TIME') as HTMLInputElement;
const preopenTime = () => document.getElementById('schedule-PREOPEN_PUSH_TIME') as HTMLInputElement;

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem('dsa.uiLanguage', 'zh');
  vi.clearAllMocks();
  api.getSchedule.mockResolvedValue(scheduleResponse());
});

describe('PersonalScheduleSection', () => {
  it('shows the effective schedule and the allowed range as hints', async () => {
    renderSection();

    expect(await screen.findByTestId('schedule-summary-plan')).toHaveTextContent('次日计划：每个交易日 17:30 自动生成');
    expect(screen.getByTestId('schedule-summary-preopen')).toHaveTextContent('盘前推送：未开启');
    expect(screen.getByText(/最早可选 17:30/)).toBeInTheDocument();
    expect(screen.getByText(/最晚可选 09:00/)).toBeInTheDocument();
    expect(screen.getByTestId('schedule-switch-DAILY_PLAN_ENABLED')).toBeChecked();
    expect(screen.getByTestId('schedule-switch-PREOPEN_PUSH_ENABLED')).not.toBeChecked();
    // The system default applies while the user has no own time: the input is empty and the effective time is shown.
    expect(planTime().value).toBe('');
    expect(screen.getByTestId('schedule-effective-DAILY_PLAN_TIME')).toHaveTextContent('当前生效：17:30');
    expect(screen.getByTestId('schedule-effective-PREOPEN_PUSH_TIME')).toHaveTextContent('当前生效：08:15');
  });

  it('prefills the own time of the user', async () => {
    const fields = defaultFields();
    fields.DAILY_PLAN_TIME = field({ key: 'DAILY_PLAN_TIME', value: '18:30', source: 'user' });
    api.getSchedule.mockResolvedValue(scheduleResponse({
      dailyPlan: { enabled: true, times: ['18:30'], defaultTime: '18:00', earliestTime: '17:30' },
    }));
    renderSection({ fields });

    await screen.findByTestId('schedule-summary-plan');
    expect(planTime().value).toBe('18:30');
    expect(screen.getByTestId('schedule-summary-plan')).toHaveTextContent('18:30');
  });

  describe('no channel notice', () => {
    it('tells the user that scheduled results stay on the web when no channel is enabled', async () => {
      const onAddChannel = vi.fn();
      renderSection({
        channels: channelState(channelsResponse({ channels: [feishuChannel({ enabled: false })] })),
        onAddChannel,
      });

      expect(await screen.findByText('未配置推送渠道，定时结果只在网页查看')).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: '去添加渠道' }));
      expect(onAddChannel).toHaveBeenCalledTimes(1);
    });

    it('also counts a channel whose secrets cannot be read as not usable', async () => {
      renderSection({
        channels: channelState(channelsResponse({ channels: [feishuChannel({ readable: false, config: {} })] })),
      });

      expect(await screen.findByText('未配置推送渠道，定时结果只在网页查看')).toBeInTheDocument();
    });

    it('is not shown while a channel is enabled, while loading, or when the channels cannot be loaded', async () => {
      const { unmount } = renderSection();
      await screen.findByTestId('schedule-summary');
      expect(screen.queryByText('未配置推送渠道，定时结果只在网页查看')).toBeNull();
      unmount();

      const loading = renderSection({ channels: channelState(null, { isLoading: true }) });
      await screen.findByTestId('schedule-summary');
      expect(screen.queryByText('未配置推送渠道，定时结果只在网页查看')).toBeNull();
      loading.unmount();

      renderSection({ channels: channelState(null, { loadError: rejection(500, { error: 'internal_error' }) }) });
      await screen.findByTestId('schedule-summary');
      expect(screen.queryByText('未配置推送渠道，定时结果只在网页查看')).toBeNull();
    });
  });

  describe('saving', () => {
    it('keeps save disabled until something changed and sends only the changed keys', async () => {
      api.updateSettings.mockResolvedValue(settingsResponse());
      const { onSettingsChanged } = renderSection();
      await screen.findByTestId('schedule-summary');
      const save = screen.getByRole('button', { name: '保存推送与定时' });
      expect(save).toBeDisabled();

      fireEvent.click(screen.getByTestId('schedule-switch-PREOPEN_PUSH_ENABLED'));
      fireEvent.change(preopenTime(), { target: { value: '08:40' } });
      fireEvent.change(planTime(), { target: { value: '18:45' } });
      expect(save).toBeEnabled();
      fireEvent.click(save);

      await waitFor(() => expect(api.updateSettings).toHaveBeenCalledTimes(1));
      expect(api.updateSettings).toHaveBeenCalledWith([
        { key: 'DAILY_PLAN_TIME', value: '18:45' },
        { key: 'PREOPEN_PUSH_ENABLED', value: 'true' },
        { key: 'PREOPEN_PUSH_TIME', value: '08:40' },
      ]);
      expect(await screen.findByText('推送与定时已保存。')).toBeInTheDocument();
      expect(onSettingsChanged).toHaveBeenCalledTimes(1);
      // The effective schedule is read again after the save.
      expect(api.getSchedule).toHaveBeenCalledTimes(2);
    });

    it('does not send a toggle that was flipped back', async () => {
      renderSection();
      await screen.findByTestId('schedule-summary');

      fireEvent.click(screen.getByTestId('schedule-switch-DAILY_PLAN_ENABLED'));
      fireEvent.click(screen.getByTestId('schedule-switch-DAILY_PLAN_ENABLED'));

      expect(screen.getByRole('button', { name: '保存推送与定时' })).toBeDisabled();
    });

    it('turns the plan off with an explicit false', async () => {
      api.updateSettings.mockResolvedValue(settingsResponse());
      renderSection();
      await screen.findByTestId('schedule-summary');

      fireEvent.click(screen.getByTestId('schedule-switch-DAILY_PLAN_ENABLED'));
      fireEvent.click(screen.getByRole('button', { name: '保存推送与定时' }));

      await waitFor(() => expect(api.updateSettings).toHaveBeenCalledWith([{ key: 'DAILY_PLAN_ENABLED', value: 'false' }]));
    });
  });

  describe('time validation', () => {
    it('explains plan_time_too_early with the earliest time from the server and stores nothing', async () => {
      api.updateSettings.mockRejectedValue(
        rejection(400, {
          error: 'plan_time_too_early',
          message: 'The next-day plan needs the day\'s board data: the earliest time is 17:45',
          detail: { issues: [{ key: 'DAILY_PLAN_TIME', code: 'plan_time_too_early', message: 'x', severity: 'error', earliest_time: '17:45' }] },
        }),
      );
      const { onSettingsChanged } = renderSection();
      await screen.findByTestId('schedule-summary');

      fireEvent.change(planTime(), { target: { value: '16:00' } });
      fireEvent.click(screen.getByRole('button', { name: '保存推送与定时' }));

      const alert = await within(screen.getByTestId('schedule-plan')).findByRole('alert');
      expect(alert).toHaveTextContent('生成时间太早');
      expect(alert).toHaveTextContent('最早可选 17:45');
      expect(planTime()).toHaveAttribute('aria-invalid', 'true');
      expect(onSettingsChanged).not.toHaveBeenCalled();
      // The draft stays editable; correcting the field clears the message.
      expect(planTime().value).toBe('16:00');
      fireEvent.change(planTime(), { target: { value: '17:50' } });
      expect(within(screen.getByTestId('schedule-plan')).queryByRole('alert')).toBeNull();
    });

    it('explains preopen_time_too_late with the latest time from the server', async () => {
      api.updateSettings.mockRejectedValue(
        rejection(400, {
          error: 'preopen_time_too_late',
          message: 'late',
          detail: { issues: [{ key: 'PREOPEN_PUSH_TIME', code: 'preopen_time_too_late', message: 'x', severity: 'error', latest_time: '08:50' }] },
        }),
      );
      renderSection();
      await screen.findByTestId('schedule-summary');

      fireEvent.change(preopenTime(), { target: { value: '09:30' } });
      fireEvent.click(screen.getByRole('button', { name: '保存推送与定时' }));

      const alert = await within(screen.getByTestId('schedule-preopen')).findByRole('alert');
      expect(alert).toHaveTextContent('盘前推送时间太晚');
      expect(alert).toHaveTextContent('最晚可选 08:50');
    });

    it('explains preopen_time_too_early with the earliest time from the server', async () => {
      api.updateSettings.mockRejectedValue(
        rejection(400, {
          error: 'preopen_time_too_early',
          message: 'early',
          detail: { issues: [{ key: 'PREOPEN_PUSH_TIME', code: 'preopen_time_too_early', message: 'x', severity: 'error', earliest_time: '05:30' }] },
        }),
      );
      renderSection();
      await screen.findByTestId('schedule-summary');

      fireEvent.change(preopenTime(), { target: { value: '00:05' } });
      fireEvent.click(screen.getByRole('button', { name: '保存推送与定时' }));

      const alert = await within(screen.getByTestId('schedule-preopen')).findByRole('alert');
      expect(alert).toHaveTextContent('盘前推送时间太早');
      expect(alert).toHaveTextContent('最早可选 05:30');
    });

    it('falls back to the range of the schedule when the error carries no issue', async () => {
      api.updateSettings.mockRejectedValue(rejection(400, { error: 'plan_time_too_early', message: 'early' }));
      renderSection();
      await screen.findByTestId('schedule-summary');

      fireEvent.change(planTime(), { target: { value: '10:00' } });
      fireEvent.click(screen.getByRole('button', { name: '保存推送与定时' }));

      expect(await within(screen.getByTestId('schedule-plan')).findByRole('alert')).toHaveTextContent('最早可选 17:30');
    });

    it('shows a plain message for a malformed time reported as invalid_value', async () => {
      api.updateSettings.mockRejectedValue(
        rejection(400, {
          error: 'invalid_value',
          message: 'bad',
          detail: { issues: [{ key: 'PREOPEN_PUSH_TIME', code: 'invalid_value', message: 'x', severity: 'error' }] },
        }),
      );
      renderSection();
      await screen.findByTestId('schedule-summary');

      fireEvent.change(preopenTime(), { target: { value: '08:10' } });
      fireEvent.click(screen.getByRole('button', { name: '保存推送与定时' }));

      expect(await within(screen.getByTestId('schedule-preopen')).findByText('请填写 HH:MM 格式的时间，例如 17:30。')).toBeInTheDocument();
    });

    it('shows other failures as a general alert', async () => {
      api.updateSettings.mockRejectedValue(rejection(500, { error: 'internal_error', message: 'boom' }));
      renderSection();
      await screen.findByTestId('schedule-summary');

      fireEvent.click(screen.getByTestId('schedule-switch-DAILY_PLAN_ENABLED'));
      fireEvent.click(screen.getByRole('button', { name: '保存推送与定时' }));

      expect(await screen.findByText('boom')).toBeInTheDocument();
    });
  });

  it('restores the system default of an own time through DELETE', async () => {
    const fields = defaultFields();
    fields.DAILY_PLAN_TIME = field({ key: 'DAILY_PLAN_TIME', value: '18:30', source: 'user' });
    api.resetSetting.mockResolvedValue(settingsResponse());
    const { onSettingsChanged } = renderSection({ fields });
    await screen.findByTestId('schedule-summary');

    // Only an own value can be restored.
    expect(screen.getAllByRole('button', { name: /恢复默认/ })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '恢复默认：生成时间' }));

    await waitFor(() => expect(api.resetSetting).toHaveBeenCalledWith('DAILY_PLAN_TIME'));
    expect(onSettingsChanged).toHaveBeenCalledTimes(1);
  });

  it('offers a retry when the schedule cannot be loaded', async () => {
    api.getSchedule.mockRejectedValueOnce(rejection(500, { error: 'internal_error', message: 'boom' }));
    renderSection();

    fireEvent.click(await screen.findByRole('button', { name: '重试' }));

    expect(await screen.findByTestId('schedule-summary')).toBeInTheDocument();
  });

  describe('primary user', () => {
    it('shows the effective schedule read-only without any control', async () => {
      api.getSchedule.mockResolvedValue(scheduleResponse({
        isPrimary: true,
        managedInSystemSettings: true,
        dailyPlan: { enabled: true, times: ['16:00', '17:40'], defaultTime: '18:00', earliestTime: '17:30' },
      }));
      renderSection({ readOnly: true });

      expect(await screen.findByTestId('schedule-summary-plan')).toHaveTextContent('16:00、17:40');
      expect(screen.getByText(/定时在系统设置中管理/)).toBeInTheDocument();
      expect(screen.queryByRole('switch')).toBeNull();
      expect(document.getElementById('schedule-DAILY_PLAN_TIME')).toBeNull();
      expect(screen.queryByRole('button', { name: '保存推送与定时' })).toBeNull();
    });
  });

  it('renders in English too', async () => {
    window.localStorage.setItem('dsa.uiLanguage', 'en');
    renderSection();

    expect(await screen.findByTestId('schedule-summary-plan')).toHaveTextContent('Next-day plan: generated every trading day at 17:30');
    expect(screen.getByText(/Earliest 17:30/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save push & schedule' })).toBeInTheDocument();
  });
});
