/**
 * Current-user / personal-settings types (docs/design/multi-user.md sections 3.3, 3.6, 10).
 * Aligned with api/v1/schemas/me.py.
 */

export type AuthModeValue = 'local' | 'user_center';

/** `GET /api/v1/me`. */
export interface CurrentUser {
  userId: string;
  handle: string;
  displayName: string;
  role: string;
  isAdmin: boolean;
  authMode: AuthModeValue;
  /** False: the user has no usable model of their own, so analysis / chat / plan need a personal model key first. */
  modelKeyConfigured: boolean;
}

export type PersonalSettingSource = 'user' | 'system_default' | 'unset';

export interface PersonalSettingOption {
  value: string;
  label: string;
}

export interface PersonalSettingField {
  key: string;
  title: string;
  description: string;
  group: string;
  dataType: string;
  uiControl: string;
  options: Array<string | PersonalSettingOption>;
  isSecret: boolean;
  /** Own value or the inherited default; `******` for a configured secret; empty for an inherited system default. */
  value: string;
  source: PersonalSettingSource;
  /** Whether the key falls back to the system default while the user has none. */
  inherits: boolean;
  configured: boolean;
  /** False for the primary user: their settings are the system (.env) settings. */
  editable: boolean;
  updatedAt?: string | null;
}

export interface PersonalSettingGroup {
  id: string;
  title: string;
  fields: PersonalSettingField[];
}

export interface PersonalSettingsResponse {
  userId: string;
  isPrimary: boolean;
  managedInSystemSettings: boolean;
  groups: PersonalSettingGroup[];
}

export interface PersonalSettingUpdateItem {
  key: string;
  value: string;
}

/** `POST /api/v1/me/settings/test-model`. Failure carries a generic category only, never the upstream text. */
export interface TestModelResult {
  success: boolean;
  model: string;
  latencyMs?: number | null;
  errorCode?: string | null;
  errorCategory?: 'auth_failed' | 'unreachable' | 'timeout' | 'bad_response' | string | null;
  httpStatus?: number | null;
  message: string;
}
