import { describe, expect, it } from 'vitest';
import type { ThresholdItem } from '../../../../types/boards';
import type { SystemConfigItem } from '../../../../types/systemConfig';
import {
  buildModelOptions,
  diffModelItems,
  parseChannels,
  readModelValues,
  toRouteModel,
  validateTemperature,
} from '../llmModels';
import {
  channelDeleteItems,
  channelSaveItems,
  emptyDraft,
  inUseProblem,
  mergeModelLists,
  normalizeChannelName,
  rolesUsing,
  validateDraft,
} from '../channelDraft';
import { resetValues, serverValues, validateRules } from '../positionRules';
import { buildOverrides, describeThresholdError, editedKeys, groupThresholds, invalidNumbers } from '../thresholds';
import { buildConfigView } from '../usePreferencesConfig';

function item(key: string, value: string, extra: Partial<SystemConfigItem> = {}): SystemConfigItem {
  return { key, value, rawValueExists: value !== '', isMasked: false, ...extra };
}

describe('position rules', () => {
  const ok = {
    POSITION_STOP_LOSS_PCT: '10',
    POSITION_MAX_TOTAL_PCT: '100',
    POSITION_MAX_HOLDINGS: '1',
    POSITION_MAX_SINGLE_PCT: '',
    POSITION_STOP_MODE: 'fixed',
  };

  it('accepts the defaults and flags out-of-range values', () => {
    expect(validateRules(ok)).toEqual({});
    const errors = validateRules({
      ...ok,
      POSITION_STOP_LOSS_PCT: '0',
      POSITION_MAX_TOTAL_PCT: '120',
      POSITION_MAX_HOLDINGS: '1.5',
      POSITION_MAX_SINGLE_PCT: 'abc',
      POSITION_STOP_MODE: 'x',
    });
    expect(Object.keys(errors).sort()).toEqual([
      'POSITION_MAX_HOLDINGS',
      'POSITION_MAX_SINGLE_PCT',
      'POSITION_MAX_TOTAL_PCT',
      'POSITION_STOP_LOSS_PCT',
      'POSITION_STOP_MODE',
    ]);
  });

  it('treats missing backend keys as plain values and falls back to the effective rules', () => {
    const view = buildConfigView([], 'v1');
    expect(serverValues(view, null)).toEqual({ ...ok });
    expect(
      serverValues(view, {
        stopLossPct: 8,
        maxTotalPct: 80,
        maxHoldings: 2,
        maxSinglePct: 40,
        maxSinglePctDerived: true,
        stopMode: 'nearer',
      }),
    ).toEqual({
      POSITION_STOP_LOSS_PCT: '8',
      POSITION_MAX_TOTAL_PCT: '80',
      POSITION_MAX_HOLDINGS: '2',
      POSITION_MAX_SINGLE_PCT: '',
      POSITION_STOP_MODE: 'nearer',
    });
  });

  it('prefers saved config values and the schema default for reset', () => {
    const view = buildConfigView(
      [
        item('POSITION_STOP_LOSS_PCT', '7'),
        item('POSITION_MAX_SINGLE_PCT', '60'),
        item('POSITION_MAX_HOLDINGS', '', {
          schema: { defaultValue: '3' } as SystemConfigItem['schema'],
        }),
      ],
      'v1',
    );
    expect(serverValues(view, null).POSITION_STOP_LOSS_PCT).toBe('7');
    expect(serverValues(view, null).POSITION_MAX_SINGLE_PCT).toBe('60');
    expect(resetValues(view).POSITION_MAX_HOLDINGS).toBe('3');
    expect(resetValues(view).POSITION_STOP_LOSS_PCT).toBe('10');
  });
});

describe('thresholds', () => {
  const rows: ThresholdItem[] = [
    { key: 'amount_ratio_mild', label: '温和放量', value: 1.5, default: 1.5, display: 'x', unit: '倍', evidence: 'medium', note: null, overridden: false },
    { key: 'strong_rank_pct', label: '强势排名', value: 12, default: 10, display: 'x', unit: '%', evidence: 'medium', note: null, overridden: true },
    { key: 'weird_dict', label: '字典', value: { a: 1 }, default: { a: 2 }, display: 'x', unit: null, evidence: 'low', note: null, overridden: true },
    { key: 'future_thing', label: '新项', value: 3, default: 3, display: 'x', unit: null, evidence: 'inferred', note: null, overridden: false },
  ];

  it('groups by key prefix and puts unknown keys under 其他', () => {
    const groups = groupThresholds(rows);
    expect(groups.map((group) => group.title)).toEqual(['放量与超额排名', '其他']);
    expect(groups[1].items.map((entry) => entry.key)).toEqual(['weird_dict', 'future_thing']);
  });

  it('overrides keep existing overrides, add edits and drop values edited back to the default', () => {
    expect(buildOverrides(rows, {})).toEqual({ strong_rank_pct: 12, weird_dict: { a: 1 } });
    expect(buildOverrides(rows, { amount_ratio_mild: '2' })).toEqual({
      amount_ratio_mild: 2,
      strong_rank_pct: 12,
      weird_dict: { a: 1 },
    });
    expect(buildOverrides(rows, { strong_rank_pct: '10' })).toEqual({ weird_dict: { a: 1 } });
  });

  it('detects edited and invalid numeric fields', () => {
    expect(editedKeys(rows, { amount_ratio_mild: '1.5', strong_rank_pct: '15' })).toEqual(['strong_rank_pct']);
    expect(invalidNumbers(rows, { amount_ratio_mild: '', strong_rank_pct: '15' })).toEqual({ amount_ratio_mild: '请填数字' });
  });

  it('describes 400 detail lists, 409 and 503', () => {
    expect(
      describeThresholdError({ response: { status: 400, data: { error: 'validation_error', message: 'a；b', detail: ['a', 'b'] } } }),
    ).toEqual({ kind: 'validation', lines: ['a', 'b'] });
    expect(describeThresholdError({ response: { status: 409, data: { error: 'config_version_conflict', message: '配置已被修改，请刷新后重试' } } }).kind).toBe('conflict');
    expect(describeThresholdError({ response: { status: 503, data: { error: 'daily_plan_unavailable' } } }).kind).toBe('unavailable');
  });
});

describe('model roles', () => {
  const view = buildConfigView(
    [
      item('LLM_CHANNELS', 'deepseek,relay'),
      item('LLM_DEEPSEEK_PROTOCOL', 'deepseek'),
      item('LLM_DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1'),
      item('LLM_DEEPSEEK_API_KEY', '******', { isMasked: true }),
      item('LLM_DEEPSEEK_MODELS', 'deepseek-v4-flash,deepseek-v4-pro'),
      item('LLM_RELAY_PROTOCOL', 'openai'),
      item('LLM_RELAY_ENABLED', 'false'),
      item('LLM_RELAY_MODELS', 'auto/some-model'),
      item('LITELLM_MODEL', 'deepseek/deepseek-v4-flash'),
      item('LITELLM_FALLBACK_MODELS', 'deepseek/deepseek-v4-pro'),
      item('AGENT_LITELLM_MODEL', ''),
      item('VISION_MODEL', 'openai/legacy-vision'),
      item('LLM_TEMPERATURE', '0.7'),
    ],
    'v9',
    ['deepseek', 'gemini'],
  );

  it('parses channels without reading secrets and lists models of enabled channels only', () => {
    const channels = parseChannels(view);
    expect(channels).toHaveLength(2);
    expect(channels[0]).toMatchObject({ name: 'deepseek', protocol: 'deepseek', host: 'api.deepseek.com', keyConfigured: true, enabled: true });
    expect(channels[1]).toMatchObject({ name: 'relay', enabled: false, keyConfigured: false });
    expect(JSON.stringify(channels)).not.toContain('******');
    expect(buildModelOptions(channels, view.providers).map((option) => option.value)).toEqual([
      'deepseek/deepseek-v4-flash',
      'deepseek/deepseek-v4-pro',
    ]);
  });

  it('prefixes unprefixed ids with the protocol and keeps known provider prefixes', () => {
    const providers = new Set(['deepseek']);
    expect(toRouteModel('gpt-4o', 'openai', providers)).toBe('openai/gpt-4o');
    expect(toRouteModel('deepseek/deepseek-chat', 'openai', providers)).toBe('deepseek/deepseek-chat');
    expect(toRouteModel('auto/deepseek-v4.1-flash', 'openai', providers)).toBe('openai/auto/deepseek-v4.1-flash');
  });

  it('maps roles onto the existing LLM keys and diffs only changed ones', () => {
    const saved = readModelValues(view);
    expect(saved).toEqual({
      primary: 'deepseek/deepseek-v4-flash',
      agent: '',
      fallbacks: ['deepseek/deepseek-v4-pro'],
      light: '',
      vision: 'openai/legacy-vision',
      temperature: '0.7',
      effort: '',
      agentEffort: '',
      lightEffort: '',
    });
    expect(diffModelItems(saved, saved)).toEqual([]);
    expect(
      diffModelItems(saved, { ...saved, primary: 'deepseek/deepseek-v4-pro', fallbacks: [], agent: 'deepseek/deepseek-v4-flash' }),
    ).toEqual([
      { key: 'LITELLM_MODEL', value: 'deepseek/deepseek-v4-pro' },
      { key: 'LITELLM_FALLBACK_MODELS', value: '' },
      { key: 'AGENT_LITELLM_MODEL', value: 'deepseek/deepseek-v4-flash' },
    ]);
    expect(
      diffModelItems(saved, { ...saved, light: 'deepseek/deepseek-v4-flash', effort: 'max', lightEffort: 'high' }),
    ).toEqual([
      { key: 'LITELLM_LIGHT_MODEL', value: 'deepseek/deepseek-v4-flash' },
      { key: 'LLM_REASONING_EFFORT', value: 'max' },
      { key: 'LITELLM_LIGHT_REASONING_EFFORT', value: 'high' },
    ]);
  });

  it('validates the temperature range', () => {
    expect(validateTemperature('')).toBeNull();
    expect(validateTemperature('1.2')).toBeNull();
    expect(validateTemperature('2.5')).not.toBeNull();
    expect(validateTemperature('abc')).not.toBeNull();
  });
});

describe('channel drafts', () => {
  const roles = {
    primary: 'anthropic/claude-opus-5-5',
    agent: '',
    fallbacks: [],
    light: 'anthropic/claude-haiku-5-5',
    vision: '',
    temperature: '',
    effort: 'max',
    agentEffort: '',
    lightEffort: 'high',
  };
  const providers = new Set(['anthropic', 'openai']);

  it('normalizes names to config-safe keys', () => {
    expect(normalizeChannelName('Darren-CPA')).toBe('darren_cpa');
    expect(normalizeChannelName(' my gateway.v2 ')).toBe('my_gateway_v2');
    expect(normalizeChannelName('中文')).toBe('');
  });

  it('validates a new channel before saving', () => {
    const base = { ...emptyDraft(), name: 'gw', protocol: 'anthropic', baseUrl: 'https://gw.example.com', apiKey: 'k', models: ['m'] };
    const opts = { isNew: true, existingNames: ['gw2'], keySaved: false };
    expect(validateDraft(base, opts)).toBeNull();
    expect(validateDraft({ ...base, name: '' }, opts)).toContain('名称');
    expect(validateDraft({ ...base, name: 'gw2' }, opts)).toContain('已经有');
    expect(validateDraft({ ...base, baseUrl: 'gw.example.com' }, opts)).toContain('http');
    expect(validateDraft({ ...base, apiKey: '' }, opts)).toContain('API Key');
    expect(validateDraft({ ...base, apiKey: '' }, { ...opts, isNew: false, keySaved: true })).toBeNull();
    expect(validateDraft({ ...base, models: [] }, opts)).toContain('至少打开一个模型');
  });

  it('builds save items for a new channel and leaves a blank key alone for a saved one', () => {
    const draft = {
      name: 'darren-cpa',
      protocol: 'anthropic',
      baseUrl: 'https://gw.example.com/',
      apiKey: 'sk-1',
      enabled: true,
      models: ['claude-opus-5-5', 'claude-haiku-5-5'],
    };
    expect(channelSaveItems(draft, ['deepseek'])).toEqual([
      { key: 'LLM_CHANNELS', value: 'deepseek,darren_cpa' },
      { key: 'LLM_DARREN_CPA_PROTOCOL', value: 'anthropic' },
      { key: 'LLM_DARREN_CPA_BASE_URL', value: 'https://gw.example.com' },
      { key: 'LLM_DARREN_CPA_ENABLED', value: 'true' },
      { key: 'LLM_DARREN_CPA_MODELS', value: 'claude-opus-5-5,claude-haiku-5-5' },
      { key: 'LLM_DARREN_CPA_API_KEY', value: 'sk-1' },
      { key: 'LLM_DARREN_CPA_API_KEYS', value: '' },
    ]);
    const existing = channelSaveItems({ ...draft, name: 'darren_cpa', apiKey: '' }, ['darren_cpa']);
    expect(existing.map((entry) => entry.key)).toEqual([
      'LLM_DARREN_CPA_PROTOCOL',
      'LLM_DARREN_CPA_BASE_URL',
      'LLM_DARREN_CPA_ENABLED',
      'LLM_DARREN_CPA_MODELS',
    ]);
    expect(channelSaveItems({ ...draft, apiKey: 'a,b' }, [])).toContainEqual({ key: 'LLM_DARREN_CPA_API_KEYS', value: 'a,b' });
  });

  it('clears every channel key on delete', () => {
    const items = channelDeleteItems('gw', ['deepseek', 'gw']);
    expect(items[0]).toEqual({ key: 'LLM_CHANNELS', value: 'deepseek' });
    expect(items.slice(1).every((entry) => entry.key.startsWith('LLM_GW_') && entry.value === '')).toBe(true);
    expect(items.map((entry) => entry.key)).toContain('LLM_GW_API_KEY');
  });

  it('blocks removing models that a role still uses', () => {
    expect(rolesUsing('anthropic/claude-opus-5-5', roles)).toEqual(['分析模型']);
    expect(inUseProblem(['claude-haiku-5-5'], 'anthropic', roles, providers)).toContain('轻量模型');
    expect(inUseProblem(['claude-sonnet-5-5'], 'anthropic', roles, providers)).toBeNull();
  });

  it('merges saved and fetched model lists without duplicates', () => {
    expect(mergeModelLists(['b', 'a'], ['a', 'c', 'b', 'd'])).toEqual(['b', 'a', 'c', 'd']);
  });
});
