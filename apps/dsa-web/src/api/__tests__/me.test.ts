import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createApiError, createParsedApiError, parseApiError } from '../error';
import { meApi } from '../me';
import { MULTI_USER_ERROR_CODES } from '../multiUserErrors';

const { get, put, post, del } = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
}));

vi.mock('../index', () => ({
  default: { get, put, post, delete: del },
}));

describe('meApi', () => {
  beforeEach(() => {
    get.mockReset();
    put.mockReset();
    post.mockReset();
    del.mockReset();
  });

  it('reads the current user and camelCases it', async () => {
    get.mockResolvedValueOnce({
      data: {
        user_id: 'u-1',
        handle: 'alice',
        display_name: 'Alice',
        role: 'user',
        is_admin: false,
        auth_mode: 'user_center',
        model_key_configured: false,
      },
    });

    const me = await meApi.getMe();

    expect(get).toHaveBeenCalledWith('/api/v1/me');
    expect(me).toEqual({
      userId: 'u-1',
      handle: 'alice',
      displayName: 'Alice',
      role: 'user',
      isAdmin: false,
      authMode: 'user_center',
      modelKeyConfigured: false,
    });
  });

  it('reads the grouped personal settings', async () => {
    get.mockResolvedValueOnce({
      data: {
        user_id: 'u-1',
        is_primary: false,
        managed_in_system_settings: false,
        groups: [
          {
            id: 'model',
            title: '模型',
            fields: [
              {
                key: 'OPENAI_API_KEY',
                group: 'model',
                is_secret: true,
                value: '******',
                source: 'user',
                inherits: false,
                configured: true,
                editable: true,
              },
            ],
          },
        ],
      },
    });

    const settings = await meApi.getSettings();

    expect(get).toHaveBeenCalledWith('/api/v1/me/settings');
    expect(settings.managedInSystemSettings).toBe(false);
    expect(settings.groups[0].fields[0]).toMatchObject({ key: 'OPENAI_API_KEY', isSecret: true, source: 'user' });
  });

  it('sends only the given items on update and url-encodes the key on reset', async () => {
    put.mockResolvedValueOnce({ data: { user_id: 'u-1', is_primary: false, managed_in_system_settings: false, groups: [] } });
    del.mockResolvedValueOnce({ data: { user_id: 'u-1', is_primary: false, managed_in_system_settings: false, groups: [] } });

    await meApi.updateSettings([{ key: 'LITELLM_MODEL', value: 'openai/gpt-x' }]);
    await meApi.resetSetting('LLM_MY CHANNEL_API_KEY');

    expect(put).toHaveBeenCalledWith('/api/v1/me/settings', { items: [{ key: 'LITELLM_MODEL', value: 'openai/gpt-x' }] });
    expect(del).toHaveBeenCalledWith('/api/v1/me/settings/LLM_MY%20CHANNEL_API_KEY');
  });

  it('returns the generic test-model category without any upstream text', async () => {
    post.mockResolvedValueOnce({
      data: {
        success: false,
        model: 'openai/x',
        latency_ms: 12,
        error_code: 'model_call_failed',
        error_category: 'auth_failed',
        http_status: 401,
        message: 'The provider rejected the API key (authentication failed) (HTTP 401)',
      },
    });

    const result = await meApi.testModel();

    expect(post).toHaveBeenCalledWith('/api/v1/me/settings/test-model');
    expect(result).toMatchObject({ success: false, errorCategory: 'auth_failed', httpStatus: 401, latencyMs: 12 });
  });

  it('reads the own usage dashboard with the default period and limit', async () => {
    get.mockResolvedValueOnce({
      data: {
        period: 'month',
        from_date: '2026-11-01',
        to_date: '2026-11-09',
        total_calls: 1,
        total_prompt_tokens: 1,
        total_completion_tokens: 2,
        total_tokens: 3,
        by_call_type: [],
        by_model: [],
        recent_calls: [],
      },
    });

    const usage = await meApi.getUsage();

    expect(get).toHaveBeenCalledWith('/api/v1/me/usage', { params: { period: 'month', limit: 50 } });
    expect(usage.totalTokens).toBe(3);
  });
});

describe('multi-user error codes in parseApiError', () => {
  const axiosLike = (status: number, data: unknown) => ({ response: { status, data } });

  it.each(MULTI_USER_ERROR_CODES.map((code) => [code]))('keeps the %s code on the parsed error', (code) => {
    const parsed = parseApiError(axiosLike(code === 'ADMIN_REQUIRED' || code === 'primary_user_uses_env' ? 403 : 400, {
      error: code,
      message: 'server text',
    }));

    expect(parsed.code).toBe(code);
    expect(parsed.category).toBe('http_error');
    expect(parsed.rawMessage).toBe('server text');
    expect(parsed.title).not.toBe('请求失败');
  });

  it('renders ADMIN_REQUIRED as a clear administrator-permission message', () => {
    const parsed = parseApiError(axiosLike(403, { error: 'ADMIN_REQUIRED', message: 'Administrator role required' }));

    expect(parsed.title).toBe('需要管理员权限');
    expect(parsed.status).toBe(403);
  });

  it('survives wrapping in an ApiRequestError (codes are read from the response body)', () => {
    const parsed = parseApiError(axiosLike(400, { error: 'model_key_required', message: 'No model configured' }));
    const wrapped = createApiError(createParsedApiError({ ...parsed }), { response: { status: 400, data: {} } });

    expect(wrapped.parsedError?.code).toBe('model_key_required');
  });

  it('leaves unrelated errors alone', () => {
    const parsed = parseApiError(axiosLike(500, { error: 'internal_error', message: 'boom' }));

    expect(parsed.code).toBeUndefined();
  });
});
