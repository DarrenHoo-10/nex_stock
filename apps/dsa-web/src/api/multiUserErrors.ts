import type { UiTextKey } from '../i18n/uiText';

/**
 * Stable `error` codes of the multi-user backend (docs/design/multi-user.md sections 2.4, 3, 16).
 * `parseApiError` keeps the code on `ParsedApiError.code`; `ApiErrorAlert` and the personal-settings page
 * turn it into a translated, actionable message via {@link MULTI_USER_ERROR_TEXT}.
 */
export const ADMIN_REQUIRED_CODE = 'ADMIN_REQUIRED';
export const MODEL_KEY_REQUIRED_CODE = 'model_key_required';

export const MULTI_USER_ERROR_CODES = [
  ADMIN_REQUIRED_CODE,
  MODEL_KEY_REQUIRED_CODE,
  'model_route_not_allowed',
  'unsafe_url',
  'scope_not_user',
  'managed_elsewhere',
  'invalid_value',
  'invalid_key',
  'primary_user_uses_env',
] as const;

export type MultiUserErrorCode = (typeof MULTI_USER_ERROR_CODES)[number];

export type MultiUserErrorText = { title: UiTextKey; message: UiTextKey };

export const MULTI_USER_ERROR_TEXT: Record<MultiUserErrorCode, MultiUserErrorText> = {
  ADMIN_REQUIRED: { title: 'me.error.adminRequired.title', message: 'me.error.adminRequired.message' },
  model_key_required: { title: 'me.error.modelKeyRequired.title', message: 'me.error.modelKeyRequired.message' },
  model_route_not_allowed: {
    title: 'me.error.modelRouteNotAllowed.title',
    message: 'me.error.modelRouteNotAllowed.message',
  },
  unsafe_url: { title: 'me.error.unsafeUrl.title', message: 'me.error.unsafeUrl.message' },
  scope_not_user: { title: 'me.error.scopeNotUser.title', message: 'me.error.scopeNotUser.message' },
  managed_elsewhere: { title: 'me.error.managedElsewhere.title', message: 'me.error.managedElsewhere.message' },
  invalid_value: { title: 'me.error.invalidValue.title', message: 'me.error.invalidValue.message' },
  invalid_key: { title: 'me.error.invalidKey.title', message: 'me.error.invalidKey.message' },
  primary_user_uses_env: {
    title: 'me.error.primaryUserUsesEnv.title',
    message: 'me.error.primaryUserUsesEnv.message',
  },
};

export function isMultiUserErrorCode(code: unknown): code is MultiUserErrorCode {
  return typeof code === 'string' && (MULTI_USER_ERROR_CODES as readonly string[]).includes(code);
}

/** The multi-user code carried by a parsed error (or any `{ code }` carrier), if any. */
export function getMultiUserErrorCode(error: { code?: string } | null | undefined): MultiUserErrorCode | null {
  return error && isMultiUserErrorCode(error.code) ? error.code : null;
}

// ---------------------------------------------------------------------------------------------------------------
// Personal push channels and schedule (P3). These codes are read from the response body by the two personal
// sections themselves instead of being registered above: `parseApiError` would rewrite them for every caller, and
// some (`rate_limited`) are shared with unrelated endpoints such as the login.
// ---------------------------------------------------------------------------------------------------------------

export const CHANNEL_ERROR_CODES = [
  'channel_not_found',
  'invalid_channel_type',
  'invalid_config',
  'unsafe_target',
  'channel_limit_reached',
  'rate_limited',
  'secret_store_unavailable',
] as const;

export type ChannelErrorCode = (typeof CHANNEL_ERROR_CODES)[number];

export const CHANNEL_ERROR_TEXT: Record<ChannelErrorCode, MultiUserErrorText> = {
  channel_not_found: { title: 'personalChannels.error.channelNotFound.title', message: 'personalChannels.error.channelNotFound.message' },
  invalid_channel_type: { title: 'personalChannels.error.invalidChannelType.title', message: 'personalChannels.error.invalidChannelType.message' },
  invalid_config: { title: 'personalChannels.error.invalidConfig.title', message: 'personalChannels.error.invalidConfig.message' },
  unsafe_target: { title: 'personalChannels.error.unsafeTarget.title', message: 'personalChannels.error.unsafeTarget.message' },
  channel_limit_reached: { title: 'personalChannels.error.limitReached.title', message: 'personalChannels.error.limitReached.message' },
  rate_limited: { title: 'personalChannels.error.rateLimited.title', message: 'personalChannels.error.rateLimited.message' },
  secret_store_unavailable: { title: 'personalChannels.error.secretStoreUnavailable.title', message: 'personalChannels.error.secretStoreUnavailable.message' },
};

export function isChannelErrorCode(code: unknown): code is ChannelErrorCode {
  return typeof code === 'string' && (CHANNEL_ERROR_CODES as readonly string[]).includes(code);
}

export const PLAN_TIME_TOO_EARLY_CODE = 'plan_time_too_early';
export const PREOPEN_TIME_TOO_LATE_CODE = 'preopen_time_too_late';
export const PREOPEN_TIME_TOO_EARLY_CODE = 'preopen_time_too_early';

type ErrorBody = { error?: unknown; code?: unknown; detail?: unknown };

function errorBody(error: unknown): ErrorBody | null {
  const data = (error as { response?: { data?: unknown } } | null)?.response?.data;
  return data && typeof data === 'object' ? (data as ErrorBody) : null;
}

/** The stable `error` code of an API error response (also when it is nested under `detail`), if any. */
export function getApiErrorCode(error: unknown): string | null {
  const body = errorBody(error);
  const detail = body?.detail && typeof body.detail === 'object' ? (body.detail as ErrorBody) : null;
  for (const candidate of [body?.error, detail?.error, body?.code, detail?.code]) {
    if (typeof candidate === 'string' && candidate.trim()) return candidate.trim();
  }
  const parsed = (error as { parsedError?: { code?: unknown } } | null)?.parsedError?.code;
  return typeof parsed === 'string' && parsed ? parsed : null;
}

/** One entry of `detail.issues`: `key` is the setting key or the channel field name the problem belongs to. */
export interface ApiErrorIssue {
  key: string;
  code: string;
  message: string;
  /** Allowed range reported with `plan_time_too_early` / `preopen_time_too_early` / `preopen_time_too_late` (HH:MM). */
  earliestTime?: string;
  latestTime?: string;
}

export function getApiErrorIssues(error: unknown): ApiErrorIssue[] {
  const body = errorBody(error);
  const detail = body?.detail && typeof body.detail === 'object' ? (body.detail as Record<string, unknown>) : null;
  const nested = detail?.detail && typeof detail.detail === 'object' ? (detail.detail as Record<string, unknown>) : null;
  const raw = detail?.issues ?? nested?.issues;
  if (!Array.isArray(raw)) return [];
  const issues: ApiErrorIssue[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const entry = item as Record<string, unknown>;
    if (typeof entry.key !== 'string' || !entry.key) continue;
    issues.push({
      key: entry.key,
      code: typeof entry.code === 'string' ? entry.code : 'invalid_value',
      message: typeof entry.message === 'string' ? entry.message : '',
      ...(typeof entry.earliest_time === 'string' ? { earliestTime: entry.earliest_time } : {}),
      ...(typeof entry.latest_time === 'string' ? { latestTime: entry.latest_time } : {}),
    });
  }
  return issues;
}
