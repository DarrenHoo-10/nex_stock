import type { ConfigView } from './usePreferencesConfig';

/**
 * The 模型 tab is a by-role view over the runtime LLM keys:
 *   分析模型 = LITELLM_MODEL, 助手模型 = AGENT_LITELLM_MODEL, 备用模型 = LITELLM_FALLBACK_MODELS,
 *   轻量模型 = LITELLM_LIGHT_MODEL, 识图模型 = VISION_MODEL, 温度 = LLM_TEMPERATURE, plus the three reasoning-effort
 *   keys; choices come from LLM_CHANNELS / LLM_<NAME>_* (edited on the same tab).
 */
export const MODEL_KEYS = {
  primary: 'LITELLM_MODEL',
  agent: 'AGENT_LITELLM_MODEL',
  fallbacks: 'LITELLM_FALLBACK_MODELS',
  light: 'LITELLM_LIGHT_MODEL',
  vision: 'VISION_MODEL',
  temperature: 'LLM_TEMPERATURE',
  effort: 'LLM_REASONING_EFFORT',
  agentEffort: 'AGENT_REASONING_EFFORT',
  lightEffort: 'LITELLM_LIGHT_REASONING_EFFORT',
} as const;

/** Standard `reasoning_effort` values; '' means "not sent" (or "follow" for the assistant). */
export const EFFORT_LEVELS: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' },
  { value: 'xhigh', label: '很高' },
  { value: 'max', label: '最高' },
];

const PROTOCOL_ALIASES: Record<string, string> = {
  vertexai: 'vertex_ai',
  vertex: 'vertex_ai',
  claude: 'anthropic',
  google: 'gemini',
  openai_compatible: 'openai',
  openai_compat: 'openai',
};

const FALSEY = new Set(['0', 'false', 'no', 'off']);

export interface ModelChannel {
  /** Lower-case channel name as listed in LLM_CHANNELS. */
  name: string;
  protocol: string;
  baseUrl: string;
  /** Host part of the base URL (display only). */
  host: string;
  enabled: boolean;
  /** An API key is saved for the channel (the key itself is never read). */
  keyConfigured: boolean;
  /** Models exactly as configured. */
  models: string[];
}

export interface ModelOption {
  /** Runtime id saved in the config, e.g. `openai/gpt-4o` or `deepseek/deepseek-v4-flash`. */
  value: string;
  /** Model name as configured on the channel. */
  label: string;
  channel: string;
}

export function splitList(text: string): string[] {
  return text
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function canonicalProtocol(value: string): string {
  const normalized = value.trim().toLowerCase().replace(/-/g, '_');
  return PROTOCOL_ALIASES[normalized] || normalized;
}

/**
 * Runtime id of a channel model: ids already prefixed with the channel protocol or a known LiteLLM provider
 * stay as they are, everything else is prefixed with the protocol (same rule as the advanced settings page,
 * without its Requesty gateway special case).
 */
export function toRouteModel(model: string, protocol: string, providers: ReadonlySet<string>): string {
  const trimmed = model.trim();
  if (!trimmed) {
    return trimmed;
  }
  const slash = trimmed.indexOf('/');
  if (slash > 0) {
    const prefix = canonicalProtocol(trimmed.slice(0, slash));
    if (prefix === protocol || providers.has(prefix) || providers.has(trimmed.slice(0, slash).toLowerCase())) {
      return `${prefix}/${trimmed.slice(slash + 1)}`;
    }
  }
  return `${protocol}/${trimmed}`;
}

function hostOf(baseUrl: string): string {
  const trimmed = baseUrl.trim();
  if (!trimmed) {
    return '';
  }
  try {
    return new URL(trimmed).host;
  } catch {
    return trimmed.replace(/^[a-z]+:\/\//i, '').split(/[/?#]/)[0];
  }
}

export function parseChannels(config: ConfigView): ModelChannel[] {
  return splitList(config.value('LLM_CHANNELS')).map((rawName) => {
    const upper = rawName.toUpperCase();
    const models = splitList(config.value(`LLM_${upper}_MODELS`));
    const explicit = config.value(`LLM_${upper}_PROTOCOL`).trim();
    const prefixed = models.find((model) => model.includes('/'));
    const protocol = canonicalProtocol(explicit || (prefixed ? prefixed.split('/', 1)[0] : 'openai')) || 'openai';
    const baseUrl = config.value(`LLM_${upper}_BASE_URL`).trim();
    const enabledText = config.value(`LLM_${upper}_ENABLED`).trim().toLowerCase();
    return {
      name: rawName.toLowerCase(),
      protocol,
      baseUrl,
      host: hostOf(baseUrl),
      enabled: enabledText === '' || !FALSEY.has(enabledText),
      keyConfigured: config.isSet(`LLM_${upper}_API_KEY`) || config.isSet(`LLM_${upper}_API_KEYS`),
      models,
    };
  });
}

/** Selectable models of the enabled channels, in channel order. */
export function buildModelOptions(channels: ModelChannel[], providers: readonly string[]): ModelOption[] {
  const providerSet = new Set(providers.map((provider) => provider.toLowerCase()));
  const options: ModelOption[] = [];
  const seen = new Set<string>();
  for (const channel of channels) {
    if (!channel.enabled) {
      continue;
    }
    for (const model of channel.models) {
      const value = toRouteModel(model, channel.protocol, providerSet);
      if (value && !seen.has(value)) {
        seen.add(value);
        options.push({ value, label: model, channel: channel.name });
      }
    }
  }
  return options;
}

export interface ModelValues {
  primary: string;
  agent: string;
  fallbacks: string[];
  light: string;
  vision: string;
  temperature: string;
  effort: string;
  agentEffort: string;
  lightEffort: string;
}

export function readModelValues(config: ConfigView): ModelValues {
  return {
    primary: config.value(MODEL_KEYS.primary).trim(),
    agent: config.value(MODEL_KEYS.agent).trim(),
    fallbacks: splitList(config.value(MODEL_KEYS.fallbacks)),
    light: config.value(MODEL_KEYS.light).trim(),
    vision: config.value(MODEL_KEYS.vision).trim(),
    temperature: config.value(MODEL_KEYS.temperature).trim(),
    effort: config.value(MODEL_KEYS.effort).trim().toLowerCase(),
    agentEffort: config.value(MODEL_KEYS.agentEffort).trim().toLowerCase(),
    lightEffort: config.value(MODEL_KEYS.lightEffort).trim().toLowerCase(),
  };
}

/** Update items for the keys whose value differs between `saved` and `draft`. */
export function diffModelItems(saved: ModelValues, draft: ModelValues): Array<{ key: string; value: string }> {
  const items: Array<{ key: string; value: string }> = [];
  if (draft.primary !== saved.primary) {
    items.push({ key: MODEL_KEYS.primary, value: draft.primary });
  }
  if (draft.fallbacks.join(',') !== saved.fallbacks.join(',')) {
    items.push({ key: MODEL_KEYS.fallbacks, value: draft.fallbacks.join(',') });
  }
  const singles = ['agent', 'light', 'vision', 'temperature', 'effort', 'agentEffort', 'lightEffort'] as const;
  for (const field of singles) {
    if (draft[field] !== saved[field]) {
      items.push({ key: MODEL_KEYS[field], value: draft[field] });
    }
  }
  return items;
}

/** Temperature must be empty (keep the model default) or a number from 0 to 2. */
export function validateTemperature(text: string): string | null {
  const trimmed = text.trim();
  if (trimmed === '') {
    return null;
  }
  const value = Number(trimmed);
  return Number.isFinite(value) && value >= 0 && value <= 2 ? null : '温度填 0 到 2 之间的数';
}
