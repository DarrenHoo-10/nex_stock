import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../../contexts/UiLanguageContext';
import { attachParsedApiError } from '../../../../api/error';
import type { SystemConfigItem } from '../../../../types/systemConfig';
import { DataStatusPanel } from '../DataStatusPanel';
import { ModelsPanel } from '../ModelsPanel';
import { PositionRulesPanel } from '../PositionRulesPanel';
import { ThresholdsPanel } from '../ThresholdsPanel';
import { buildConfigView } from '../usePreferencesConfig';
import type { SaveOutcome } from '../usePreferencesConfig';

const { get, put, post, getPlanAccount, previewSizing } = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  post: vi.fn(),
  getPlanAccount: vi.fn(),
  previewSizing: vi.fn(),
}));

vi.mock('../../../../api', () => ({ default: { get, put, post } }));
vi.mock('../../../../hooks/useLatestPlanRun', () => ({ useLatestPlanRun: () => ({ status: 'ready', run: null }) }));
vi.mock('../../../../api/dailyPlan', () => ({
  getPlanAccount,
  previewSizing,
  isPlanServiceUnavailable: () => false,
  isPlanRunConflict: () => false,
}));

function item(key: string, value: string, extra: Partial<SystemConfigItem> = {}): SystemConfigItem {
  return { key, value, rawValueExists: value !== '', isMasked: false, ...extra };
}

function httpError(status: number, data: unknown): Error {
  const error = Object.assign(new Error('Request failed'), { response: { status, data } });
  attachParsedApiError(error);
  return error;
}

function wrap(node: React.ReactNode) {
  return render(
    <UiLanguageProvider>
      <MemoryRouter>{node}</MemoryRouter>
    </UiLanguageProvider>,
  );
}

beforeEach(() => {
  get.mockReset();
  put.mockReset();
  post.mockReset();
  getPlanAccount.mockReset();
  previewSizing.mockReset();
  getPlanAccount.mockRejectedValue(new Error('offline'));
});

describe('PositionRulesPanel', () => {
  const view = buildConfigView(
    [
      item('POSITION_STOP_LOSS_PCT', '10'),
      item('POSITION_MAX_TOTAL_PCT', '100'),
      item('POSITION_MAX_HOLDINGS', '1'),
      item('POSITION_MAX_SINGLE_PCT', ''),
      item('POSITION_STOP_MODE', 'fixed'),
    ],
    'v1',
  );

  it('saves only the changed keys and shows the derived single cap', async () => {
    const onSave = vi.fn<(items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>>(async () => ({ ok: true }));
    wrap(<PositionRulesPanel config={view} onSave={onSave} onReload={vi.fn()} />);

    expect(screen.getByTestId('position-derived')).toHaveTextContent('现在是 100.0%');
    fireEvent.change(screen.getByLabelText('最多持有（只）'), { target: { value: '2' } });
    expect(screen.getByTestId('position-derived')).toHaveTextContent('现在是 50.0%');
    fireEvent.change(screen.getByLabelText('止损幅度（%）'), { target: { value: '8' } });
    fireEvent.click(screen.getByRole('button', { name: '保存规则' }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith([
      { key: 'POSITION_STOP_LOSS_PCT', value: '8' },
      { key: 'POSITION_MAX_HOLDINGS', value: '2' },
    ]);
  });

  it('blocks invalid input locally and maps backend 400 issues to fields', async () => {
    const onSave = vi.fn<(items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>>(async () => ({
      ok: false,
      kind: 'validation',
      message: '配置校验失败',
      issues: [{ key: 'POSITION_MAX_TOTAL_PCT', code: 'out_of_range', message: '总仓位不能超过 100', severity: 'error' }],
    }));
    wrap(<PositionRulesPanel config={view} onSave={onSave} onReload={vi.fn()} />);

    fireEvent.change(screen.getByLabelText('止损幅度（%）'), { target: { value: '150' } });
    fireEvent.click(screen.getByRole('button', { name: '保存规则' }));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByLabelText('止损幅度（%）')).toHaveAttribute('aria-invalid', 'true');

    fireEvent.change(screen.getByLabelText('止损幅度（%）'), { target: { value: '9' } });
    fireEvent.change(screen.getByLabelText('总仓位上限（%）'), { target: { value: '90' } });
    fireEvent.click(screen.getByRole('button', { name: '保存规则' }));
    await screen.findByText('总仓位不能超过 100');
    expect(screen.getByLabelText('总仓位上限（%）')).toHaveAttribute('aria-invalid', 'true');
  });

  it('offers a reload on a 409 conflict', async () => {
    const onSave = vi.fn(async (): Promise<SaveOutcome> => ({ ok: false, kind: 'conflict', message: '配置版本冲突' }));
    const onReload = vi.fn();
    wrap(<PositionRulesPanel config={view} onSave={onSave} onReload={onReload} />);

    fireEvent.change(screen.getByLabelText('止损幅度（%）'), { target: { value: '8' } });
    fireEvent.click(screen.getByRole('button', { name: '保存规则' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('配置已变化');
    fireEvent.click(within(alert).getByRole('button', { name: '重新加载' }));
    expect(onReload).toHaveBeenCalledTimes(1);
  });

  it('works when the keys are missing from the config and the plan service is down', async () => {
    wrap(<PositionRulesPanel config={buildConfigView([], 'v1')} onSave={vi.fn()} onReload={vi.fn()} />);
    expect(screen.getByLabelText('止损幅度（%）')).toHaveValue('10');
    expect(screen.getByLabelText('总仓位上限（%）')).toHaveValue('100');
    expect(screen.getByLabelText('最多持有（只）')).toHaveValue('1');
    expect(screen.getByRole('button', { name: '保存规则' })).toBeDisabled();
  });

  it('only asks the backend for a sizing preview and shows its answer', async () => {
    getPlanAccount.mockResolvedValue({ rules: null, holdings: [{ code: '002475' }] });
    previewSizing.mockResolvedValue({
      status: 'ok',
      message: null,
      price: 47,
      shares: 2100,
      amount: 98700,
      capitalPct: 98.7,
      maxLoss: 9870,
      stop: { price: 42.3 },
      steps: ['按单只上限 2,127 股'],
    });
    wrap(<PositionRulesPanel config={view} onSave={vi.fn()} onReload={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText('股票代码')).toHaveValue('002475'));
    fireEvent.change(screen.getByLabelText('买入价（元）'), { target: { value: '47' } });
    fireEvent.click(screen.getByRole('button', { name: '试算' }));
    const result = await screen.findByTestId('sizing-result');
    expect(result).toHaveTextContent('2,100');
    expect(result).toHaveTextContent('42.30');
    expect(previewSizing).toHaveBeenCalledWith({ code: '002475', price: 47, horizon: 'short' });
  });
});

describe('ThresholdsPanel', () => {
  const rows = [
    { key: 'amount_ratio_mild', label: '温和放量', value: 1.5, default: 1.5, display: '成交额 ≥ 5 日均值 1.5 倍', unit: '倍', evidence: 'medium', note: null, overridden: false },
    { key: 'strong_rank_pct', label: '强势排名', value: 12, default: 10, display: '超额收益前 12%', unit: '%', evidence: 'low', note: null, overridden: true },
  ];

  it('loads the wrapped list, saves changed items and keeps existing overrides', async () => {
    get.mockResolvedValue({ data: { items: rows } });
    put.mockResolvedValue({ data: { items: rows } });
    wrap(<ThresholdsPanel />);

    const mild = await screen.findByLabelText('温和放量 当前值');
    expect(screen.getAllByText(/依据：/)).toHaveLength(2);
    fireEvent.change(mild, { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put).toHaveBeenCalledWith('/api/v1/boards/thresholds', { overrides: { amount_ratio_mild: 2, strong_rank_pct: 12 } });
  });

  it('resets one item as a draft and drops it from the saved overrides', async () => {
    get.mockResolvedValue({ data: { items: rows } });
    put.mockResolvedValue({ data: { items: rows } });
    wrap(<ThresholdsPanel />);
    await screen.findByLabelText('强势排名 当前值');
    fireEvent.click(screen.getByRole('button', { name: '强势排名 恢复默认' }));
    expect(screen.getByLabelText('强势排名 当前值')).toHaveValue('10');
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }));
    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/v1/boards/thresholds', { overrides: {} }));
  });

  it('restores everything with an empty overrides object after confirming', async () => {
    get.mockResolvedValue({ data: { items: rows } });
    put.mockResolvedValue({ data: { items: rows } });
    wrap(<ThresholdsPanel />);
    await screen.findByLabelText('强势排名 当前值');
    fireEvent.click(screen.getByRole('button', { name: '全部恢复默认' }));
    fireEvent.click(await screen.findByRole('button', { name: '确认恢复' }));
    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/v1/boards/thresholds', { overrides: {} }));
  });

  it('lists 400 details inline and shows the unavailable state for 503', async () => {
    get.mockResolvedValue({ data: { items: rows } });
    put.mockRejectedValue(httpError(400, { error: 'validation_error', message: 'a；b', detail: ['温和放量必须大于 1', '强势排名不能超过 50'] }));
    wrap(<ThresholdsPanel />);
    fireEvent.change(await screen.findByLabelText('温和放量 当前值'), { target: { value: '0.5' } });
    fireEvent.click(screen.getByRole('button', { name: '保存阈值' }));
    const errors = await screen.findByTestId('threshold-errors');
    expect(errors).toHaveTextContent('温和放量必须大于 1');
    expect(errors).toHaveTextContent('强势排名不能超过 50');
  });

  it('shows the service-unavailable state', async () => {
    get.mockRejectedValue(httpError(503, { error: 'daily_plan_unavailable', message: '次日计划服务未就绪' }));
    wrap(<ThresholdsPanel />);
    expect(await screen.findByText('次日计划服务未就绪')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
  });
});

describe('ModelsPanel', () => {
  const view = buildConfigView(
    [
      item('LLM_CHANNELS', 'deepseek,gemini'),
      item('LLM_DEEPSEEK_PROTOCOL', 'deepseek'),
      item('LLM_DEEPSEEK_API_KEY', '******', { isMasked: true }),
      item('LLM_DEEPSEEK_MODELS', 'deepseek-v4-flash,deepseek-v4-pro'),
      item('LLM_GEMINI_PROTOCOL', 'gemini'),
      item('LLM_GEMINI_MODELS', 'gemini-3-flash-preview'),
      item('LITELLM_MODEL', 'deepseek/deepseek-v4-flash'),
      item('LITELLM_FALLBACK_MODELS', 'gemini/gemini-3-flash-preview'),
      item('AGENT_LITELLM_MODEL', ''),
      item('VISION_MODEL', ''),
      item('LLM_TEMPERATURE', '0.7'),
    ],
    'v1',
    ['deepseek', 'gemini'],
  );

  it('edits roles over the existing keys and never shows secrets', async () => {
    post.mockResolvedValue({ data: { success: true, message: 'ok', models: ['deepseek-v4-flash', 'deepseek-v4-pro', 'deepseek-v4-lite'] } });
    const onSave = vi.fn<(items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>>(async () => ({ ok: true }));
    const { container } = wrap(<ModelsPanel config={view} onSave={onSave} onReload={vi.fn()} />);
    expect(container.textContent).not.toContain('******');
    expect(screen.getByLabelText('API Key')).toHaveValue('');
    expect(screen.getByLabelText('API Key')).toHaveAttribute('placeholder', '已保存，留空表示不修改');
    expect(screen.queryByRole('link', { name: '高级设置' })).not.toBeInTheDocument();
    expect(container.querySelector('a[href^="/settings"]')).toBeNull();

    // Picking the current fallback as primary removes it from the fallback list.
    fireEvent.change(screen.getByLabelText('分析模型'), { target: { value: 'gemini/gemini-3-flash-preview' } });
    fireEvent.change(screen.getByLabelText('助手模型'), { target: { value: 'deepseek/deepseek-v4-pro' } });
    fireEvent.change(screen.getByLabelText('识图模型'), { target: { value: 'gemini/gemini-3-flash-preview' } });
    fireEvent.change(screen.getByLabelText('轻量模型'), { target: { value: 'deepseek/deepseek-v4-flash' } });
    fireEvent.change(screen.getByLabelText('推理强度（备用模型同样使用）'), { target: { value: 'max' } });
    fireEvent.click(screen.getByRole('button', { name: '保存模型设置' }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith([
      { key: 'LITELLM_MODEL', value: 'gemini/gemini-3-flash-preview' },
      { key: 'LITELLM_FALLBACK_MODELS', value: '' },
      { key: 'AGENT_LITELLM_MODEL', value: 'deepseek/deepseek-v4-pro' },
      { key: 'LITELLM_LIGHT_MODEL', value: 'deepseek/deepseek-v4-flash' },
      { key: 'VISION_MODEL', value: 'gemini/gemini-3-flash-preview' },
      { key: 'LLM_REASONING_EFFORT', value: 'max' },
    ]);
  });

  it('lists fetched models for a saved channel and blocks switching off a model in use', async () => {
    post.mockResolvedValue({ data: { success: true, message: 'ok', models: ['deepseek-v4-flash', 'deepseek-v4-pro', 'deepseek-v4-lite'] } });
    const onSave = vi.fn<(items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>>(async () => ({ ok: true }));
    wrap(<ModelsPanel config={view} onSave={onSave} onReload={vi.fn()} />);

    // Opening the saved channel fetches its list once with the saved key.
    const lite = await screen.findByRole('switch', { name: '使用 deepseek-v4-lite' });
    expect(lite).not.toBeChecked();
    expect(post).toHaveBeenCalledWith(
      '/api/v1/system/config/llm/discover-models',
      expect.objectContaining({ name: 'deepseek', protocol: 'deepseek', use_saved_secret: true, api_key: '' }),
    );
    expect(screen.getByText(/3 个模型 · 已打开 2/)).toBeInTheDocument();

    // deepseek-v4-flash is the 分析模型: switching it off is refused before saving.
    fireEvent.click(screen.getByRole('switch', { name: '使用 deepseek-v4-flash' }));
    fireEvent.click(screen.getByRole('button', { name: '保存渠道' }));
    expect(await screen.findByText(/deepseek-v4-flash 正在用作分析模型/)).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('switch', { name: '使用 deepseek-v4-flash' }));
    fireEvent.click(lite);
    fireEvent.click(screen.getByRole('button', { name: '保存渠道' }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith([
      { key: 'LLM_DEEPSEEK_PROTOCOL', value: 'deepseek' },
      { key: 'LLM_DEEPSEEK_BASE_URL', value: '' },
      { key: 'LLM_DEEPSEEK_ENABLED', value: 'true' },
      { key: 'LLM_DEEPSEEK_MODELS', value: 'deepseek-v4-flash,deepseek-v4-pro,deepseek-v4-lite' },
    ]);
  });

  it('adds an Anthropic channel: fetches models with the typed key and saves a config-safe name', async () => {
    post.mockResolvedValue({ data: { success: true, message: 'ok', models: ['claude-opus-5-5', 'claude-haiku-5-5'] } });
    const onSave = vi.fn<(items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>>(async () => ({ ok: true }));
    wrap(<ModelsPanel config={buildConfigView([], 'v1', ['anthropic'])} onSave={onSave} onReload={vi.fn()} />);

    expect(screen.getByText('还没有可选的模型。先在上面添加渠道并打开模型。')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: 'darren-cpa' } });
    expect(screen.getByText(/保存为 darren_cpa/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('接口地址'), { target: { value: 'https://gw.example.com' } });
    fireEvent.change(screen.getByLabelText('API 格式'), { target: { value: 'anthropic' } });
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'sk-typed' } });
    fireEvent.click(screen.getByRole('button', { name: '获取模型' }));

    const opus = await screen.findByRole('switch', { name: '使用 claude-opus-5-5' });
    expect(post).toHaveBeenCalledWith(
      '/api/v1/system/config/llm/discover-models',
      expect.objectContaining({ name: 'darren_cpa', protocol: 'anthropic', api_key: 'sk-typed', use_saved_secret: false }),
    );
    fireEvent.click(screen.getByRole('button', { name: '全选' }));
    expect(opus).toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: '添加渠道' }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith([
      { key: 'LLM_CHANNELS', value: 'darren_cpa' },
      { key: 'LLM_DARREN_CPA_PROTOCOL', value: 'anthropic' },
      { key: 'LLM_DARREN_CPA_BASE_URL', value: 'https://gw.example.com' },
      { key: 'LLM_DARREN_CPA_ENABLED', value: 'true' },
      { key: 'LLM_DARREN_CPA_MODELS', value: 'claude-opus-5-5,claude-haiku-5-5' },
      { key: 'LLM_DARREN_CPA_API_KEY', value: 'sk-typed' },
      { key: 'LLM_DARREN_CPA_API_KEYS', value: '' },
    ]);
  });

  it('deletes an unused channel after a second confirming click', async () => {
    post.mockResolvedValue({ data: { success: false, message: 'no', models: [] } });
    const onSave = vi.fn<(items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>>(async () => ({ ok: true }));
    const unused = buildConfigView(
      [
        item('LLM_CHANNELS', 'deepseek,spare'),
        item('LLM_DEEPSEEK_PROTOCOL', 'deepseek'),
        item('LLM_DEEPSEEK_MODELS', 'deepseek-v4-flash'),
        item('LLM_SPARE_PROTOCOL', 'gemini'),
        item('LLM_SPARE_MODELS', 'gemini-3-flash-preview'),
        item('LITELLM_MODEL', 'deepseek/deepseek-v4-flash'),
      ],
      'v1',
      ['deepseek', 'gemini'],
    );
    wrap(<ModelsPanel config={unused} onSave={onSave} onReload={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: '删除渠道' }));
    expect(await screen.findByText(/正在用作分析模型/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^spare/ }));
    fireEvent.click(screen.getByRole('button', { name: '删除渠道' }));
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0][0]).toEqual({ key: 'LLM_CHANNELS', value: 'deepseek' });
  });

  it('rejects an out-of-range temperature before saving', () => {
    const onSave = vi.fn();
    wrap(<ModelsPanel config={view} onSave={onSave} onReload={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('温度'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: '保存模型设置' }));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText('温度填 0 到 2 之间的数')).toBeInTheDocument();
  });
});

describe('DataStatusPanel source settings', () => {
  function provider(name: string, label: string, configured: boolean, configKeys: string[]) {
    return {
      name, label, enabled: configured, configured, status: configured ? 'unknown' : 'unconfigured',
      priority: null, markets: ['cn'], datasets: ['quote.realtime'], dataset_markets: {}, warnings: [],
      last_error: null, cooldown: null, config_keys: configKeys,
    };
  }
  const overview = (biyingConfigured: boolean) => ({
    data: {
      as_of: '2026-11-09T01:00:00+08:00',
      providers: [
        provider('akshare', 'AkShare', true, []),
        provider('tushare', 'Tushare', true, ['TUSHARE_TOKEN']),
        provider('biying', 'Biying', biyingConfigured, ['BIYING_LICENCE', 'BIYING_TIMEOUT_SECONDS']),
      ],
      datasets: [],
      priorities: [],
      warnings: [],
    },
  });
  const view = buildConfigView(
    [
      item('TUSHARE_TOKEN', '******', { isMasked: true, schema: { key: 'TUSHARE_TOKEN', isSensitive: true } as never }),
      item('BIYING_LICENCE', '', { schema: { key: 'BIYING_LICENCE', isSensitive: true } as never }),
      item('BIYING_TIMEOUT_SECONDS', '8'),
    ],
    'v1',
  );

  it('configures a source in place, sends only typed values and refreshes the status', async () => {
    get.mockResolvedValueOnce(overview(false)).mockResolvedValueOnce(overview(true));
    const onSave = vi.fn<(items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>>(async () => ({ ok: true }));
    wrap(<DataStatusPanel config={view} onSave={onSave} onReload={vi.fn()} />);

    expect(await screen.findByRole('button', { name: '配置 Biying' })).toHaveTextContent('配置');
    expect(screen.queryByRole('button', { name: '配置 AkShare' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '配置 Tushare' })).toHaveTextContent('修改');

    fireEvent.click(screen.getByRole('button', { name: '配置 Biying' }));
    const form = screen.getByRole('form', { name: 'Biying 配置' });
    const licence = within(form).getByLabelText('必盈 licence');
    expect(licence).toHaveAttribute('type', 'password');
    expect(licence).toHaveAttribute('placeholder', '未配置');
    expect(within(form).getByLabelText('单次请求超时（秒）')).toHaveValue('8');
    expect(within(form).getByRole('button', { name: '保存' })).toBeDisabled();

    fireEvent.change(licence, { target: { value: '  my-licence  ' } });
    fireEvent.click(within(form).getByRole('button', { name: '保存' }));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith([{ key: 'BIYING_LICENCE', value: 'my-licence' }]));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole('form', { name: 'Biying 配置' })).not.toBeInTheDocument();
    expect(await screen.findByRole('button', { name: '配置 Biying' })).toHaveTextContent('修改');
  });

  it('never shows a saved secret and keeps it when left empty', async () => {
    get.mockResolvedValue(overview(true));
    const onSave = vi.fn<(items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>>(async () => ({ ok: true }));
    wrap(<DataStatusPanel config={view} onSave={onSave} onReload={vi.fn()} />);

    fireEvent.click(await screen.findByRole('button', { name: '配置 Tushare' }));
    const token = screen.getByLabelText('Tushare Token');
    expect(token).toHaveValue('');
    expect(token).toHaveAttribute('placeholder', '已配置，留空不修改');
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('shows the source list read-only while the settings are not loaded', async () => {
    get.mockResolvedValue(overview(false));
    wrap(<DataStatusPanel />);
    expect(await screen.findByText('Biying')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^配置 / })).not.toBeInTheDocument();
  });
});
