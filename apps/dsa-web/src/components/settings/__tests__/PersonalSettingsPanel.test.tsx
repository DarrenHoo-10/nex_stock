import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { CurrentUserProvider } from '../../../test-utils/currentUser';
import { regularUser } from '../../../test-utils/currentUserFixtures';
import {
  channelsResponse,
  primaryChannelsResponse,
  scheduleResponse,
} from '../../../test-utils/personalChannelFixtures';
import type { PersonalSettingField, PersonalSettingsResponse } from '../../../types/me';
import { PersonalSettingsPanel } from '../PersonalSettingsPanel';

const { getSettings, updateSettings, resetSetting, testModel, listNotificationChannels, getSchedule } = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
  resetSetting: vi.fn(),
  testModel: vi.fn(),
  listNotificationChannels: vi.fn(),
  getSchedule: vi.fn(),
}));

vi.mock('../../../api/me', () => ({
  meApi: { getSettings, updateSettings, resetSetting, testModel, listNotificationChannels, getSchedule },
}));

/** Legacy per-channel credential keys the personal settings API still returns (docs/design/multi-user.md 21.1). */
const LEGACY_CHANNEL_KEYS = [
  'FEISHU_WEBHOOK_URL',
  'TELEGRAM_BOT_TOKEN',
  'TELEGRAM_CHAT_ID',
  'EMAIL_SENDER',
  'EMAIL_PASSWORD',
  'PUSHPLUS_TOKEN',
  'CUSTOM_WEBHOOK_URLS',
];

function field(overrides: Partial<PersonalSettingField> & Pick<PersonalSettingField, 'key'>): PersonalSettingField {
  return {
    title: overrides.key,
    description: '',
    group: 'model',
    dataType: 'string',
    uiControl: 'text',
    options: [],
    isSecret: false,
    value: '',
    source: 'unset',
    inherits: true,
    configured: false,
    editable: true,
    updatedAt: null,
    ...overrides,
  };
}

function response(overrides: Partial<PersonalSettingsResponse> = {}): PersonalSettingsResponse {
  return {
    userId: 'u-alice',
    isPrimary: false,
    managedInSystemSettings: false,
    groups: [
      {
        id: 'model',
        title: '模型',
        fields: [
          field({ key: 'LITELLM_MODEL', title: '主模型', value: 'openai/gpt-x', source: 'user', configured: true }),
          field({
            key: 'OPENAI_API_KEY',
            title: 'OpenAI API Key',
            isSecret: true,
            uiControl: 'password',
            value: '******',
            source: 'user',
            inherits: false,
            configured: true,
          }),
          field({ key: 'OPENAI_BASE_URL', title: 'OpenAI Base URL', source: 'unset', inherits: false }),
          field({ key: 'LLM_TEMPERATURE', title: '温度', value: '', source: 'system_default', configured: true }),
        ],
      },
      {
        id: 'push',
        title: '推送',
        fields: [
          field({ key: 'DAILY_PLAN_ENABLED', group: 'push', title: '次日计划', uiControl: 'switch', dataType: 'boolean', value: 'true', source: 'system_default', configured: true }),
          field({ key: 'DAILY_PLAN_TIME', group: 'push', title: '次日计划时间' }),
          field({ key: 'PREOPEN_PUSH_ENABLED', group: 'push', title: '盘前推送', uiControl: 'switch', dataType: 'boolean' }),
          field({ key: 'PREOPEN_PUSH_TIME', group: 'push', title: '盘前推送时间' }),
          field({ key: 'NOTIFICATION_QUIET_HOURS', group: 'push', title: '静默时段', value: '22:00-07:00', source: 'user', configured: true }),
          ...LEGACY_CHANNEL_KEYS.map((key) =>
            field({ key, group: 'push', title: `旧渠道 ${key}`, isSecret: true, uiControl: 'password', inherits: false }),
          ),
        ],
      },
      {
        id: 'position',
        title: '仓位规则',
        fields: [field({ key: 'POSITION_MAX_HOLDINGS', group: 'position', title: '最大持仓数', value: '8', source: 'user', configured: true })],
      },
    ],
    ...overrides,
  };
}

function renderPanel(refreshCurrentUser = vi.fn().mockResolvedValue(undefined)) {
  return {
    refreshCurrentUser,
    ...render(
      <MemoryRouter>
        <UiLanguageProvider>
          <CurrentUserProvider user={regularUser} value={{ refreshCurrentUser }}>
            <PersonalSettingsPanel />
          </CurrentUserProvider>
        </UiLanguageProvider>
      </MemoryRouter>,
    ),
  };
}

const rejection = (status: number, data: unknown) => ({ response: { status, data } });

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem('dsa.uiLanguage', 'zh');
  vi.clearAllMocks();
  getSettings.mockResolvedValue(response());
  listNotificationChannels.mockResolvedValue(channelsResponse());
  getSchedule.mockResolvedValue(scheduleResponse());
});

describe('PersonalSettingsPanel', () => {
  it('renders the groups with a source badge on every field and masks configured secrets', async () => {
    renderPanel();

    expect(await screen.findByRole('heading', { name: '个人设置' })).toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: '个人设置分组' });
    expect(within(nav).getByText('模型')).toBeInTheDocument();
    expect(within(nav).getByText('推送渠道')).toBeInTheDocument();
    expect(within(nav).getByText('推送与定时')).toBeInTheDocument();
    expect(within(nav).getByText('推送偏好')).toBeInTheDocument();
    expect(within(nav).getByText('仓位规则')).toBeInTheDocument();

    expect(screen.getByTestId('personal-source-LITELLM_MODEL')).toHaveAttribute('data-source', 'user');
    expect(screen.getByTestId('personal-source-LITELLM_MODEL')).toHaveTextContent('我的设置');
    expect(screen.getByTestId('personal-source-LLM_TEMPERATURE')).toHaveAttribute('data-source', 'system_default');
    expect(screen.getByTestId('personal-source-LLM_TEMPERATURE')).toHaveTextContent('系统默认');
    expect(screen.getByTestId('personal-source-OPENAI_BASE_URL')).toHaveAttribute('data-source', 'unset');
    expect(screen.getByTestId('personal-source-OPENAI_BASE_URL')).toHaveTextContent('未配置');

    // The secret is only ever the mask; the input is a password field.
    const secretInput = document.getElementById('setting-OPENAI_API_KEY') as HTMLInputElement;
    expect(secretInput.type).toBe('password');
    expect(secretInput.value).toBe('******');
    const secretField = screen.getByTestId('personal-field-OPENAI_API_KEY');
    expect(within(secretField).getByText(/留空表示保持当前值/)).toBeInTheDocument();
    // Model/search keys never fall back to the system default; an unset one says so.
    expect(within(screen.getByTestId('personal-field-OPENAI_BASE_URL')).getByText(/不会回退到系统默认值/)).toBeInTheDocument();
    // An inherited system default is reported without its value.
    expect(within(screen.getByTestId('personal-field-LLM_TEMPERATURE')).getByText(/沿用系统默认值/)).toBeInTheDocument();
  });

  it('saves only the changed fields and refreshes the current user', async () => {
    updateSettings.mockResolvedValue(response());
    const { refreshCurrentUser } = renderPanel();
    await screen.findByTestId('personal-field-LITELLM_MODEL');

    fireEvent.change(document.getElementById('setting-LITELLM_MODEL') as HTMLInputElement, {
      target: { value: 'deepseek/deepseek-chat' },
    });
    fireEvent.change(document.getElementById('setting-OPENAI_BASE_URL') as HTMLInputElement, {
      target: { value: 'https://llm.example.com/v1' },
    });
    // Clearing a configured secret means "keep it": nothing to send for it.
    fireEvent.change(document.getElementById('setting-OPENAI_API_KEY') as HTMLInputElement, { target: { value: '' } });

    fireEvent.click(screen.getByRole('button', { name: '保存（2 项）' }));

    await waitFor(() => expect(updateSettings).toHaveBeenCalledTimes(1));
    expect(updateSettings).toHaveBeenCalledWith([
      { key: 'LITELLM_MODEL', value: 'deepseek/deepseek-chat' },
      { key: 'OPENAI_BASE_URL', value: 'https://llm.example.com/v1' },
    ]);
    expect(await screen.findByText('个人设置已保存。')).toBeInTheDocument();
    expect(refreshCurrentUser).toHaveBeenCalled();
  });

  it('sends a newly typed secret and nothing for an untouched one', async () => {
    updateSettings.mockResolvedValue(response());
    renderPanel();
    await screen.findByTestId('personal-field-OPENAI_API_KEY');

    fireEvent.change(document.getElementById('setting-OPENAI_API_KEY') as HTMLInputElement, { target: { value: 'sk-new' } });
    fireEvent.click(screen.getByRole('button', { name: '保存（1 项）' }));

    await waitFor(() => expect(updateSettings).toHaveBeenCalledWith([{ key: 'OPENAI_API_KEY', value: 'sk-new' }]));
  });

  it('restores the default of one own value through DELETE and shows the refreshed source', async () => {
    resetSetting.mockResolvedValue(
      response({
        groups: [
          {
            id: 'model',
            title: '模型',
            fields: [field({ key: 'LITELLM_MODEL', title: '主模型', value: '', source: 'system_default', configured: true })],
          },
        ],
      }),
    );
    renderPanel();
    await screen.findByTestId('personal-field-LITELLM_MODEL');

    // Only own values can be restored: not the inherited or unset ones.
    expect(within(screen.getByTestId('personal-field-LLM_TEMPERATURE')).queryByRole('button', { name: /恢复默认/ })).toBeNull();
    expect(within(screen.getByTestId('personal-field-OPENAI_BASE_URL')).queryByRole('button', { name: /恢复默认/ })).toBeNull();

    fireEvent.click(within(screen.getByTestId('personal-field-LITELLM_MODEL')).getByRole('button', { name: '恢复默认：主模型' }));

    await waitFor(() => expect(resetSetting).toHaveBeenCalledWith('LITELLM_MODEL'));
    expect(await screen.findByText('已恢复默认：主模型')).toBeInTheDocument();
    expect(screen.getByTestId('personal-source-LITELLM_MODEL')).toHaveAttribute('data-source', 'system_default');
  });

  it('puts the channel list and the schedule in front of the remaining push preferences', async () => {
    renderPanel();
    const nav = await screen.findByRole('navigation', { name: '个人设置分组' });

    const order = within(nav).getAllByRole('button').map((button) => button.textContent ?? '');
    const at = (title: string) => order.findIndex((text) => text.startsWith(title));
    expect(at('推送渠道')).toBeGreaterThan(at('模型'));
    expect(at('推送与定时')).toBe(at('推送渠道') + 1);
    expect(at('推送偏好')).toBe(at('推送与定时') + 1);
    // The channel badge is the number of channels.
    expect(within(nav).getByRole('button', { name: /^推送渠道/ })).toHaveTextContent('2');
  });

  it('hides the legacy channel credential keys and the schedule keys from every settings group', async () => {
    renderPanel();
    await screen.findByTestId('personal-field-LITELLM_MODEL');
    const nav = screen.getByRole('navigation', { name: '个人设置分组' });

    // Visit every tab: no field of the legacy channel keys shows up anywhere.
    for (const button of within(nav).getAllByRole('button')) {
      fireEvent.click(button);
      for (const key of LEGACY_CHANNEL_KEYS) {
        expect(screen.queryByTestId(`personal-field-${key}`)).toBeNull();
      }
    }

    fireEvent.click(within(nav).getByRole('button', { name: /^推送偏好/ }));
    // Only the remaining preferences are left; the switches and times moved to 推送与定时.
    expect(await screen.findByTestId('personal-field-NOTIFICATION_QUIET_HOURS')).toBeInTheDocument();
    expect(screen.queryByTestId('personal-field-DAILY_PLAN_ENABLED')).toBeNull();
    expect(screen.queryByTestId('personal-field-PREOPEN_PUSH_TIME')).toBeNull();
    expect(screen.queryByText(/旧渠道/)).toBeNull();
  });

  it('shows the push channels tab with the channels of the user', async () => {
    renderPanel();
    await screen.findByTestId('personal-field-LITELLM_MODEL');

    fireEvent.click(screen.getByRole('button', { name: /^推送渠道/ }));

    expect(await screen.findByTestId('personal-channels')).toBeInTheDocument();
    expect(within(screen.getByTestId('channel-1')).getByText('我的飞书群')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '添加渠道' })).toBeEnabled();
  });

  it('saves the schedule through the personal settings and refreshes the current user', async () => {
    updateSettings.mockResolvedValue(response());
    const { refreshCurrentUser } = renderPanel();
    await screen.findByTestId('personal-field-LITELLM_MODEL');

    fireEvent.click(screen.getByRole('button', { name: /^推送与定时/ }));
    expect(await screen.findByTestId('personal-schedule')).toBeInTheDocument();
    await screen.findByTestId('schedule-summary');
    fireEvent.click(screen.getByTestId('schedule-switch-PREOPEN_PUSH_ENABLED'));
    fireEvent.click(screen.getByRole('button', { name: '保存推送与定时' }));

    await waitFor(() => expect(updateSettings).toHaveBeenCalledWith([{ key: 'PREOPEN_PUSH_ENABLED', value: 'true' }]));
    expect(await screen.findByText('推送与定时已保存。')).toBeInTheDocument();
    expect(refreshCurrentUser).toHaveBeenCalled();
  });

  it('jumps from the no-channel notice to the channel tab', async () => {
    listNotificationChannels.mockResolvedValue(channelsResponse({ channels: [] }));
    renderPanel();
    await screen.findByTestId('personal-field-LITELLM_MODEL');

    fireEvent.click(screen.getByRole('button', { name: /^推送与定时/ }));
    expect(await screen.findByText('未配置推送渠道，定时结果只在网页查看')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '去添加渠道' }));

    expect(await screen.findByTestId('personal-channels')).toBeInTheDocument();
    expect(screen.getByText(/还没有推送渠道/)).toBeInTheDocument();
  });

  it('keeps the settings usable when the channel list cannot be loaded', async () => {
    listNotificationChannels.mockRejectedValue(rejection(500, { error: 'internal_error', message: 'boom' }));
    renderPanel();

    expect(await screen.findByTestId('personal-field-LITELLM_MODEL')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /^推送与定时/ }));
    await screen.findByTestId('schedule-summary');
    // Nothing is known about the channels, so no "no channel" claim is made.
    expect(screen.queryByText('未配置推送渠道，定时结果只在网页查看')).toBeNull();
  });

  describe('model test', () => {
    it('reports a working model', async () => {
      testModel.mockResolvedValue({ success: true, model: 'openai/gpt-x', latencyMs: 321, message: 'OK' });
      renderPanel();
      await screen.findByTestId('personal-model-test');

      fireEvent.click(screen.getByRole('button', { name: '测试模型连接' }));

      expect(await screen.findByText('模型连接正常：openai/gpt-x（321 ms）')).toBeInTheDocument();
      expect(testModel).toHaveBeenCalledTimes(1);
    });

    it.each([
      ['auth_failed', 401, /服务商拒绝了 API Key/],
      ['unreachable', null, /无法连接到模型接口地址/],
      ['timeout', null, /没有在限定时间内响应/],
      ['bad_response', 502, /不是可用的模型结果/],
    ])('shows only the generic %s category', async (category, status, text) => {
      testModel.mockResolvedValue({
        success: false,
        model: 'openai/gpt-x',
        errorCode: 'model_call_failed',
        errorCategory: category,
        httpStatus: status,
        message: 'server wording that must not be needed',
      });
      renderPanel();
      await screen.findByTestId('personal-model-test');

      fireEvent.click(screen.getByRole('button', { name: '测试模型连接' }));

      expect(await screen.findByText('模型连接测试失败')).toBeInTheDocument();
      expect(screen.getByText(text)).toBeInTheDocument();
      expect(screen.queryByText(/server wording/)).toBeNull();
    });

    it('maps model_key_required from the test call to the dedicated message', async () => {
      testModel.mockRejectedValue(rejection(400, { error: 'model_key_required', message: 'No model configured' }));
      renderPanel();
      await screen.findByTestId('personal-model-test');

      fireEvent.click(screen.getByRole('button', { name: '测试模型连接' }));

      expect(await screen.findByText('需要先配置你自己的模型 Key')).toBeInTheDocument();
    });

    it('asks to save pending edits first', async () => {
      renderPanel();
      await screen.findByTestId('personal-model-test');

      fireEvent.change(document.getElementById('setting-LITELLM_MODEL') as HTMLInputElement, { target: { value: 'x/y' } });

      expect(screen.getByRole('button', { name: '测试模型连接' })).toBeDisabled();
      expect(screen.getByText('测试使用已保存的配置，请先保存修改。')).toBeInTheDocument();
    });
  });

  describe('error codes', () => {
    async function saveWith(error: unknown) {
      updateSettings.mockRejectedValue(error);
      renderPanel();
      await screen.findByTestId('personal-field-OPENAI_BASE_URL');
      fireEvent.change(document.getElementById('setting-OPENAI_BASE_URL') as HTMLInputElement, {
        target: { value: 'http://10.0.0.1/v1' },
      });
      fireEvent.click(screen.getByRole('button', { name: '保存（1 项）' }));
    }

    it('explains unsafe_url and links to the settings that fix it', async () => {
      await saveWith(rejection(400, { error: 'unsafe_url', message: 'OPENAI_BASE_URL must be a public https address' }));

      const alert = await screen.findByRole('alert');
      expect(within(alert).getByText('地址不安全')).toBeInTheDocument();
      expect(within(alert).getByText(/只允许公网 https 地址/)).toBeInTheDocument();
    });

    it('shows invalid_value issues on the field that failed', async () => {
      await saveWith(
        rejection(400, {
          error: 'invalid_value',
          message: 'OPENAI_BASE_URL failed validation',
          detail: { issues: [{ key: 'OPENAI_BASE_URL', code: 'invalid_url', message: '地址格式不正确', severity: 'error' }] },
        }),
      );

      expect(await screen.findByText('填写的值不合法')).toBeInTheDocument();
      expect(within(screen.getByTestId('personal-field-OPENAI_BASE_URL')).getByText('地址格式不正确')).toBeInTheDocument();
      // Nothing was stored, so the draft stays editable.
      expect((document.getElementById('setting-OPENAI_BASE_URL') as HTMLInputElement).value).toBe('http://10.0.0.1/v1');
    });

    it.each([
      ['model_route_not_allowed', '该模型渠道不被允许'],
      ['scope_not_user', '这是系统级设置'],
      ['managed_elsewhere', '请在对应页面维护'],
      ['invalid_key', '未知的配置项'],
      ['primary_user_uses_env', '主用户请到系统设置修改'],
    ])('shows a friendly message for %s', async (code, title) => {
      await saveWith(rejection(code === 'primary_user_uses_env' ? 403 : 400, { error: code, message: 'raw server text' }));

      expect(await screen.findByText(title)).toBeInTheDocument();
    });
  });

  it('shows the primary user a read-only view that points at the system settings', async () => {
    getSettings.mockResolvedValue(
      response({
        isPrimary: true,
        managedInSystemSettings: true,
        groups: [
          {
            id: 'model',
            title: '模型',
            fields: [
              field({ key: 'LITELLM_MODEL', title: '主模型', value: 'openai/gpt-x', source: 'user', configured: true, editable: false }),
              field({ key: 'OPENAI_API_KEY', title: 'OpenAI API Key', isSecret: true, uiControl: 'password', value: '******', source: 'user', configured: true, editable: false }),
            ],
          },
        ],
      }),
    );
    renderPanel();

    expect(await screen.findByText('你是主用户，个人设置就是系统设置')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '前往系统设置' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^保存/ })).toBeNull();
    expect(screen.queryByRole('button', { name: '测试模型连接' })).toBeNull();
    expect(screen.queryByRole('button', { name: /恢复默认/ })).toBeNull();
    expect(document.getElementById('setting-LITELLM_MODEL')).toBeDisabled();
    expect((document.getElementById('setting-OPENAI_API_KEY') as HTMLInputElement).value).toBe('******');
  });

  it('shows the primary user the .env channels and the effective schedule read-only', async () => {
    getSettings.mockResolvedValue(
      response({
        isPrimary: true,
        managedInSystemSettings: true,
        groups: [
          { id: 'model', title: '模型', fields: [field({ key: 'LITELLM_MODEL', title: '主模型', value: 'openai/gpt-x', source: 'user', configured: true, editable: false })] },
        ],
      }),
    );
    listNotificationChannels.mockResolvedValue(primaryChannelsResponse());
    getSchedule.mockResolvedValue(scheduleResponse({ isPrimary: true, managedInSystemSettings: true }));
    renderPanel();
    await screen.findByTestId('personal-field-LITELLM_MODEL');

    fireEvent.click(screen.getByRole('button', { name: /^推送渠道/ }));
    expect(await screen.findByText('你是主用户，推送渠道来自系统设置')).toBeInTheDocument();
    expect(screen.getByTestId('channel-env-feishu')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '添加渠道' })).toBeNull();
    expect(screen.queryByRole('switch')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /^推送与定时/ }));
    expect(await screen.findByTestId('schedule-summary-plan')).toBeInTheDocument();
    expect(screen.queryByRole('switch')).toBeNull();
    expect(screen.queryByRole('button', { name: '保存推送与定时' })).toBeNull();
    expect(updateSettings).not.toHaveBeenCalled();
  });

  it('renders for local mode (no current user) without writing anything', async () => {
    render(
      <MemoryRouter>
        <UiLanguageProvider>
          <CurrentUserProvider user={null}>
            <PersonalSettingsPanel />
          </CurrentUserProvider>
        </UiLanguageProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('personal-field-LITELLM_MODEL')).toBeInTheDocument();
    expect(updateSettings).not.toHaveBeenCalled();
    expect(resetSetting).not.toHaveBeenCalled();
  });

  it('offers a retry when loading the settings fails', async () => {
    getSettings.mockRejectedValueOnce(rejection(500, { error: 'internal_error', message: 'boom' }));
    renderPanel();

    const retry = await screen.findByRole('button', { name: '重试' });
    getSettings.mockResolvedValueOnce(response());
    fireEvent.click(retry);

    expect(await screen.findByTestId('personal-field-LITELLM_MODEL')).toBeInTheDocument();
  });
});
