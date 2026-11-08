import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import apiClient from '../index';
import { getParsedApiError } from '../error';
import { setUserCenterSession } from '../../utils/userCenterSession';

describe('authentication response redirect', () => {
  const assign = vi.fn();

  beforeEach(() => {
    assign.mockClear();
    vi.stubGlobal('window', {
      location: { pathname: '/settings', search: '?category=system', hash: '#desktop-version-info', assign },
    });
  });

  afterEach(() => vi.unstubAllGlobals());

  it('preserves the complete destination on an expired session', async () => {
    const error = { response: { status: 401 }, message: 'expired session' };
    await expect(apiClient.get('/fixture', { adapter: () => Promise.reject(error) })).rejects.toBe(error);
    expect(assign).toHaveBeenCalledWith('/login?redirect=%2Fsettings%3Fcategory%3Dsystem%23desktop-version-info');
  });

  it('does not redirect an unauthorized login request into a loop', async () => {
    window.location.pathname = '/login';
    const error = { response: { status: 401 }, message: 'incorrect password' };
    await expect(apiClient.get('/fixture', { adapter: () => Promise.reject(error) })).rejects.toBe(error);
    expect(assign).not.toHaveBeenCalled();
  });

  it('does not treat a server error as a session expiry', async () => {
    const error = { response: { status: 500 }, message: 'server error' };
    await expect(apiClient.get('/fixture', { adapter: () => Promise.reject(error) })).rejects.toBe(error);
    expect(assign).not.toHaveBeenCalled();
  });

  it.each([
    '/api/v1/system/config/generation-backends/status/preview',
    '/api/v1/system/config/generation-backends/smoke-test',
    '/api/v1/system/config',
  ])('preserves actionable config issues through the real interceptor for %s', async (endpoint) => {
    const issues = [
      { key: 'LLM_MODELFLARE_API_KEY', code: 'missing_api_key', message: 'API key required', actual: 'private-key-value' },
      { key: 'LITELLM_FALLBACK_MODELS', code: 'unknown_model', message: 'Undeclared fallback model' },
    ];
    const error = { response: { status: 400, data: { detail: {
      error: 'validation_failed', message: 'System configuration validation failed', issues,
    } } } };
    await expect(apiClient.post(endpoint, {}, { adapter: () => Promise.reject(error) })).rejects.toBe(error);
    const parsed = getParsedApiError(error);
    expect(parsed.title).toBe('配置需要调整');
    expect(parsed.message).toContain('LLM_MODELFLARE_API_KEY：请填写此渠道的 API Key。');
    expect(parsed.message).toContain('LITELLM_FALLBACK_MODELS：请选择当前启用渠道中的模型');
    expect(parsed.rawMessage).not.toContain('private-key-value');
    expect(parsed.code).toBe('validation_failed');
    expect(assign).not.toHaveBeenCalled();
  });

  it('handles normalized validation payloads and incomplete issue objects safely', async () => {
    const error = { response: { status: 400, data: {
      error: 'validation_failed', message: 'System configuration validation failed',
      issues: [null, { actual: 'private-key-value' }],
    } } };
    await expect(apiClient.post('/fixture', {}, { adapter: () => Promise.reject(error) })).rejects.toBe(error);
    expect(getParsedApiError(error).message).toBe('请检查渠道的 API Key、模型和运行参数后重试。');
    expect(getParsedApiError(error).rawMessage).not.toContain('private-key-value');
  });
});

describe('user_center mode', () => {
  const assign = vi.fn();
  const LOGIN_URL = 'https://user.example.test/login?return=https%3A%2F%2Fstock.example.test%2F';

  type FixtureConfig = { headers: { get: (name: string) => unknown } };
  const okAdapter = (captured: { headers?: FixtureConfig['headers'] }) => (config: FixtureConfig) => {
    captured.headers = config.headers;
    return Promise.resolve({ data: {}, status: 200, statusText: 'OK', headers: {}, config });
  };

  beforeEach(() => {
    assign.mockClear();
    vi.stubGlobal('window', {
      location: { pathname: '/boards', search: '', hash: '', assign },
    });
  });

  afterEach(() => {
    setUserCenterSession(null);
    vi.unstubAllGlobals();
  });

  it.each(['post', 'put', 'patch', 'delete'] as const)('attaches X-CSRF-Token to %s requests', async (method) => {
    setUserCenterSession({ csrfToken: 'csrf-123', loginUrl: LOGIN_URL });
    const captured: { headers?: FixtureConfig['headers'] } = {};
    await apiClient.request({ url: '/fixture', method, adapter: okAdapter(captured) as never });
    expect(captured.headers?.get('X-CSRF-Token')).toBe('csrf-123');
  });

  it('does not attach X-CSRF-Token to GET requests', async () => {
    setUserCenterSession({ csrfToken: 'csrf-123', loginUrl: LOGIN_URL });
    const captured: { headers?: FixtureConfig['headers'] } = {};
    await apiClient.get('/fixture', { adapter: okAdapter(captured) as never });
    expect(captured.headers?.get('X-CSRF-Token')).toBeFalsy();
  });

  it('sends no CSRF header outside user_center mode', async () => {
    const captured: { headers?: FixtureConfig['headers'] } = {};
    await apiClient.post('/fixture', {}, { adapter: okAdapter(captured) as never });
    expect(captured.headers?.get('X-CSRF-Token')).toBeFalsy();
  });

  it('redirects to the user-center login URL when the session expires', async () => {
    setUserCenterSession({ csrfToken: 'csrf-123', loginUrl: LOGIN_URL });
    const error = { response: { status: 401 }, message: 'expired session' };
    await expect(apiClient.get('/fixture', { adapter: () => Promise.reject(error) })).rejects.toBe(error);
    expect(assign).toHaveBeenCalledWith(LOGIN_URL);
  });

  it('does not redirect on 403 (non-admin or CSRF failure)', async () => {
    setUserCenterSession({ csrfToken: 'csrf-123', loginUrl: LOGIN_URL });
    const error = { response: { status: 403, data: { error: 'ADMIN_REQUIRED' } }, message: 'forbidden' };
    await expect(apiClient.get('/fixture', { adapter: () => Promise.reject(error) })).rejects.toBe(error);
    expect(assign).not.toHaveBeenCalled();
  });
});
