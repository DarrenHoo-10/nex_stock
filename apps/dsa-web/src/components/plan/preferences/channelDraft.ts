import { toRouteModel } from './llmModels';
import type { ModelChannel, ModelValues } from './llmModels';

/**
 * Channel editing on the 模型 tab. A channel is stored as `LLM_CHANNELS` plus `LLM_<NAME>_PROTOCOL / _BASE_URL /
 * _API_KEY(S) / _MODELS / _ENABLED` (the same keys the runtime reads); `_MODELS` holds the models that are switched on.
 */

export const CHANNEL_PROTOCOLS: ReadonlyArray<{ value: string; label: string; baseUrlHint: string }> = [
  { value: 'openai', label: 'OpenAI 兼容（/v1/chat/completions）', baseUrlHint: 'https://example.com/v1' },
  { value: 'anthropic', label: 'Anthropic Messages（/v1/messages）', baseUrlHint: 'https://example.com' },
  { value: 'gemini', label: 'Gemini', baseUrlHint: '留空使用官方地址' },
  { value: 'deepseek', label: 'DeepSeek', baseUrlHint: 'https://api.deepseek.com' },
  { value: 'ollama', label: 'Ollama（本机）', baseUrlHint: 'http://127.0.0.1:11434' },
  { value: 'vertex_ai', label: 'Vertex AI', baseUrlHint: '留空使用官方地址' },
];

/** Protocols whose model list the backend can fetch (`POST /system/config/llm/discover-models`). */
export const DISCOVERABLE_PROTOCOLS: ReadonlySet<string> = new Set(['openai', 'deepseek', 'anthropic']);

export function protocolLabel(protocol: string): string {
  return CHANNEL_PROTOCOLS.find((item) => item.value === protocol)?.label ?? protocol;
}

export interface ChannelDraft {
  name: string;
  protocol: string;
  baseUrl: string;
  /** Newly typed key; '' keeps the saved one. */
  apiKey: string;
  enabled: boolean;
  /** Switched-on models, in order. */
  models: string[];
}

export function draftFromChannel(channel: ModelChannel): ChannelDraft {
  return {
    name: channel.name,
    protocol: channel.protocol,
    baseUrl: channel.baseUrl,
    apiKey: '',
    enabled: channel.enabled,
    models: [...channel.models],
  };
}

export function emptyDraft(): ChannelDraft {
  return { name: '', protocol: 'openai', baseUrl: '', apiKey: '', enabled: true, models: [] };
}

/** Config keys only allow lower-case letters, digits and `_`; `darren-cpa` becomes `darren_cpa`. */
export function normalizeChannelName(raw: string): string {
  return raw
    .trim()
    .toLowerCase()
    .replace(/[\s.-]+/g, '_')
    .replace(/[^a-z0-9_]/g, '');
}

export function draftChanged(saved: ChannelDraft, draft: ChannelDraft): boolean {
  return (
    draft.apiKey.trim() !== '' ||
    draft.name !== saved.name ||
    draft.protocol !== saved.protocol ||
    draft.baseUrl.trim() !== saved.baseUrl ||
    draft.enabled !== saved.enabled ||
    draft.models.join(',') !== saved.models.join(',')
  );
}

/** First problem that blocks saving, or null. */
export function validateDraft(
  draft: ChannelDraft,
  options: { isNew: boolean; existingNames: readonly string[]; keySaved: boolean },
): string | null {
  const name = normalizeChannelName(draft.name);
  if (options.isNew) {
    if (!name) {
      return '填写渠道名称，只能用字母、数字和下划线';
    }
    if (options.existingNames.includes(name)) {
      return `已经有名为 ${name} 的渠道`;
    }
  }
  const baseUrl = draft.baseUrl.trim();
  if (baseUrl && !/^https?:\/\/[^\s/]+/i.test(baseUrl)) {
    return '接口地址要以 http:// 或 https:// 开头';
  }
  if (!baseUrl && (draft.protocol === 'openai' || draft.protocol === 'anthropic')) {
    return '填写接口地址';
  }
  if (draft.protocol !== 'ollama' && !draft.apiKey.trim() && !options.keySaved) {
    return '填写 API Key';
  }
  if (draft.models.length === 0) {
    return '至少打开一个模型：点「获取模型」或「添加模型」';
  }
  return null;
}

/** Update items for saving one channel (new or existing). Keys this tab does not edit are left alone. */
export function channelSaveItems(draft: ChannelDraft, channelNames: readonly string[]): Array<{ key: string; value: string }> {
  const name = normalizeChannelName(draft.name);
  const prefix = `LLM_${name.toUpperCase()}`;
  const items: Array<{ key: string; value: string }> = [];
  if (!channelNames.includes(name)) {
    items.push({ key: 'LLM_CHANNELS', value: [...channelNames, name].join(',') });
  }
  items.push({ key: `${prefix}_PROTOCOL`, value: draft.protocol });
  items.push({ key: `${prefix}_BASE_URL`, value: draft.baseUrl.trim().replace(/\/+$/, '') });
  items.push({ key: `${prefix}_ENABLED`, value: draft.enabled ? 'true' : 'false' });
  items.push({ key: `${prefix}_MODELS`, value: draft.models.join(',') });
  const key = draft.apiKey.trim();
  if (key) {
    const multi = key.includes(',');
    items.push({ key: `${prefix}_API_KEY${multi ? 'S' : ''}`, value: key });
    items.push({ key: `${prefix}_API_KEY${multi ? '' : 'S'}`, value: '' });
  }
  return items;
}

const CHANNEL_KEY_SUFFIXES = ['PROTOCOL', 'API_SURFACE', 'BASE_URL', 'ENABLED', 'API_KEY', 'API_KEYS', 'MODELS', 'EXTRA_HEADERS'];

export function channelDeleteItems(name: string, channelNames: readonly string[]): Array<{ key: string; value: string }> {
  const prefix = `LLM_${name.toUpperCase()}`;
  return [
    { key: 'LLM_CHANNELS', value: channelNames.filter((item) => item !== name).join(',') },
    ...CHANNEL_KEY_SUFFIXES.map((suffix) => ({ key: `${prefix}_${suffix}`, value: '' })),
  ];
}

/** Role names that use a runtime model id. */
export function rolesUsing(value: string, roles: ModelValues): string[] {
  if (!value) {
    return [];
  }
  const used: string[] = [];
  if (roles.primary === value) used.push('分析模型');
  if (roles.fallbacks.includes(value)) used.push('备用模型');
  if (roles.agent === value) used.push('助手模型');
  if (roles.light === value) used.push('轻量模型');
  if (roles.vision === value) used.push('识图模型');
  return used;
}

/**
 * Why a saved channel's models cannot go away (switched off, channel disabled or deleted): models that a role still
 * points at. Returns a sentence, or null when nothing is in use.
 */
export function inUseProblem(
  removed: readonly string[],
  protocol: string,
  roles: ModelValues,
  providers: ReadonlySet<string>,
): string | null {
  for (const model of removed) {
    const used = rolesUsing(toRouteModel(model, protocol, providers), roles);
    if (used.length > 0) {
      return `${model} 正在用作${used.join('、')}，先在下面的「用途」里换成别的模型`;
    }
  }
  return null;
}

/** Merge saved and fetched model ids: saved first (their order), then fetched ones not yet listed. */
export function mergeModelLists(saved: readonly string[], fetched: readonly string[]): string[] {
  const seen = new Set(saved);
  const merged = [...saved];
  for (const model of fetched) {
    if (!seen.has(model)) {
      seen.add(model);
      merged.push(model);
    }
  }
  return merged;
}
