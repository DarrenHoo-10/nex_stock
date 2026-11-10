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
