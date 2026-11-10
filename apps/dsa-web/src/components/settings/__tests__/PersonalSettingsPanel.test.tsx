import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { CurrentUserProvider } from '../../../test-utils/currentUser';
import { regularUser } from '../../../test-utils/currentUserFixtures';
import type { PersonalSettingField, PersonalSettingsResponse } from '../../../types/me';
import { PersonalSettingsPanel } from '../PersonalSettingsPanel';

const { getSettings, updateSettings, resetSetting, testModel } = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
  resetSetting: vi.fn(),
  testModel: vi.fn(),
}));

vi.mock('../../../api/me', () => ({
  meApi: { getSettings, updateSettings, resetSetting, testModel },
}));

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
          field({ key: 'FEISHU_WEBHOOK_URL', group: 'push', title: '飞书 Webhook', isSecret: true, uiControl: 'password', inherits: false }),
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
});

describe('PersonalSettingsPanel', () => {
  it('renders the groups with a source badge on every field and masks configured secrets', async () => {
    renderPanel();

    expect(await screen.findByRole('heading', { name: '个人设置' })).toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: '个人设置分组' });
    expect(within(nav).getByText('模型')).toBeInTheDocument();
    expect(within(nav).getByText('推送')).toBeInTheDocument();
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

  it('hides push channel credentials but keeps the push preferences', async () => {
    renderPanel();
    await screen.findByTestId('personal-field-LITELLM_MODEL');

    fireEvent.click(screen.getByRole('button', { name: /^推送/ }));

    expect(await screen.findByTestId('personal-field-DAILY_PLAN_ENABLED')).toBeInTheDocument();
    expect(screen.queryByTestId('personal-field-FEISHU_WEBHOOK_URL')).toBeNull();
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

  it('offers a retry when loading the settings fails', async () => {
    getSettings.mockRejectedValueOnce(rejection(500, { error: 'internal_error', message: 'boom' }));
    renderPanel();

    const retry = await screen.findByRole('button', { name: '重试' });
    getSettings.mockResolvedValueOnce(response());
    fireEvent.click(retry);

    expect(await screen.findByTestId('personal-field-LITELLM_MODEL')).toBeInTheDocument();
  });
});
