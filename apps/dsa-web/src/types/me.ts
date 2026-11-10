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

// ---------------------------------------------------------------------------------------------------------------
// Personal push channels and schedule (docs/design/multi-user.md section 21, aligned with api/v1/schemas/me.py).
// ---------------------------------------------------------------------------------------------------------------

/** One field of a channel type. `name` is the snake_case key of the `config` object sent to / returned by the API. */
export interface ChannelFieldInfo {
  name: string;
  label: string;
  kind: string;
  required: boolean;
  secret: boolean;
}

export interface ChannelTypeInfo {
  channelType: string;
  label: string;
  fields: ChannelFieldInfo[];
}

/** Field values keyed by the snake_case field name (never camel-cased); secrets are `******` or empty. */
export type ChannelConfig = Record<string, string | number | string[] | null | undefined>;

export interface PersonalChannel {
  /** Null for the primary user's read-only `.env` channels. */
  id: number | null;
  channelType: string;
  name: string;
  enabled: boolean;
  /** `env`: a system (.env) channel of the primary user. */
  source: 'user' | 'env';
  editable: boolean;
  /** False when the stored secrets cannot be decrypted: the config has to be entered again. */
  readable: boolean;
  config: ChannelConfig;
  createdAt?: string | null;
  updatedAt?: string | null;
}

/** `GET /api/v1/me/notification-channels` (and the body of `DELETE`). */
export interface PersonalChannelsResponse {
  userId: string;
  isPrimary: boolean;
  managedInSystemSettings: boolean;
  note: string;
  maxChannels: number;
  channels: PersonalChannel[];
  channelTypes: ChannelTypeInfo[];
}

export interface PersonalChannelCreateInput {
  channelType: string;
  name?: string;
  enabled?: boolean;
  config: ChannelConfig;
}

/** Partial update: omitted fields stay; an empty or masked secret keeps the stored value. */
export interface PersonalChannelUpdateInput {
  name?: string;
  enabled?: boolean;
  config?: ChannelConfig;
}

export type ChannelTestStatus = 'sent' | 'failed' | 'blocked';

/** `POST /api/v1/me/notification-channels/{id}/test`: a status and a generic category, never an upstream message. */
export interface ChannelTestResult {
  success: boolean;
  status: ChannelTestStatus;
  /** `send_failed` / `target_not_allowed` / `unreadable`; other categories are shown generically. */
  errorCategory?: string | null;
  latencyMs?: number | null;
}

/** `GET /api/v1/me/schedule`: effective switches and times plus the allowed range. */
export interface DailyPlanSchedule {
  enabled: boolean;
  times: string[];
  defaultTime: string;
  /** Earliest allowed plan time (the system's board data is ready). */
  earliestTime: string;
}

export interface PreopenSchedule {
  enabled: boolean;
  time: string;
  defaultTime: string;
  /** Earliest allowed start of the pre-open push (after the US session closes). */
  earliestTime?: string;
  /** Latest allowed start of the pre-open push (before market open). */
  latestTime: string;
}

export interface MeSchedule {
  userId: string;
  isPrimary: boolean;
  managedInSystemSettings: boolean;
  dailyPlan: DailyPlanSchedule;
  preopenPush: PreopenSchedule;
}
