import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../../contexts/UiLanguageContext';
import { attachParsedApiError } from '../../../../api/error';
import type { SystemConfigItem } from '../../../../types/systemConfig';
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
    const onSave = vi.fn<(items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>>(async () => ({ ok: true }));
    const { container } = wrap(<ModelsPanel config={view} onSave={onSave} onReload={vi.fn()} />);
    expect(container.textContent).not.toContain('******');
    expect(screen.getByText('密钥已配置', { exact: false })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '高级设置' })).toHaveAttribute('href', '/settings?category=ai_model');

    // Picking the current fallback as primary removes it from the fallback list.
    fireEvent.change(screen.getByLabelText('分析模型'), { target: { value: 'gemini/gemini-3-flash-preview' } });
    fireEvent.change(screen.getByLabelText('助手模型'), { target: { value: 'deepseek/deepseek-v4-pro' } });
    fireEvent.change(screen.getByLabelText('识图模型'), { target: { value: 'gemini/gemini-3-flash-preview' } });
    fireEvent.click(screen.getByRole('button', { name: '保存模型设置' }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith([
      { key: 'LITELLM_MODEL', value: 'gemini/gemini-3-flash-preview' },
      { key: 'LITELLM_FALLBACK_MODELS', value: '' },
      { key: 'AGENT_LITELLM_MODEL', value: 'deepseek/deepseek-v4-pro' },
      { key: 'VISION_MODEL', value: 'gemini/gemini-3-flash-preview' },
    ]);
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
