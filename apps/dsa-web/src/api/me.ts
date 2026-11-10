import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  ChannelConfig,
  ChannelTestResult,
  CurrentUser,
  MeSchedule,
  PersonalChannel,
  PersonalChannelCreateInput,
  PersonalChannelUpdateInput,
  PersonalChannelsResponse,
  PersonalSettingUpdateItem,
  PersonalSettingsResponse,
  TestModelResult,
} from '../types/me';
import type { UsageDashboard, UsagePeriod } from './usage';

/**
 * Channel payloads are camel-cased except `config`: its keys are the snake_case field names of the channel type
 * (`webhook_url`, `bot_token` ...) that the API reads back verbatim.
 */
function parseChannel(raw: Record<string, unknown>): PersonalChannel {
  const { config, ...rest } = raw;
  return {
    ...toCamelCase<Omit<PersonalChannel, 'config'>>(rest),
    config: (config && typeof config === 'object' ? config : {}) as ChannelConfig,
  };
}

function parseChannels(data: Record<string, unknown>): PersonalChannelsResponse {
  const { channels, ...rest } = data;
  return {
    ...toCamelCase<Omit<PersonalChannelsResponse, 'channels'>>(rest),
    channels: Array.isArray(channels) ? channels.map((item) => parseChannel(item as Record<string, unknown>)) : [],
  };
}

/** Current user, personal settings, push channels, schedule and own usage (`/api/v1/me/*`). */
export const meApi = {
  async getMe(): Promise<CurrentUser> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/me');
    return toCamelCase<CurrentUser>(response.data);
  },

  async getSettings(): Promise<PersonalSettingsResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/me/settings');
    return toCamelCase<PersonalSettingsResponse>(response.data);
  },

  /** Partial update; an empty (or masked) secret keeps the stored value. Nothing is stored when any item is invalid. */
  async updateSettings(items: PersonalSettingUpdateItem[]): Promise<PersonalSettingsResponse> {
    const response = await apiClient.put<Record<string, unknown>>('/api/v1/me/settings', { items });
    return toCamelCase<PersonalSettingsResponse>(response.data);
  },

  /** Drop the user's own value so the system default applies again (or nothing, for secrets). */
  async resetSetting(key: string): Promise<PersonalSettingsResponse> {
    const response = await apiClient.delete<Record<string, unknown>>(
      `/api/v1/me/settings/${encodeURIComponent(key)}`,
    );
    return toCamelCase<PersonalSettingsResponse>(response.data);
  },

  /** One minimal completion with the user's own model key. A provider failure is `success: false`, not an HTTP error. */
  async testModel(): Promise<TestModelResult> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/me/settings/test-model');
    return toCamelCase<TestModelResult>(response.data);
  },

  /** Own LLM usage, same shape as the administrator-only `/usage/dashboard`. */
  async getUsage(params: { period?: UsagePeriod; limit?: number } = {}): Promise<UsageDashboard> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/me/usage', {
      params: {
        period: params.period ?? 'month',
        limit: params.limit ?? 50,
      },
    });
    return toCamelCase<UsageDashboard>(response.data);
  },

  /** The user's push channels (secrets masked); for the primary user the read-only `.env` channel types. */
  async listNotificationChannels(): Promise<PersonalChannelsResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/me/notification-channels');
    return parseChannels(response.data);
  },

  async createNotificationChannel(input: PersonalChannelCreateInput): Promise<PersonalChannel> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/me/notification-channels', {
      channel_type: input.channelType,
      name: input.name,
      enabled: input.enabled ?? true,
      config: input.config,
    });
    return parseChannel(response.data);
  },

  /** Partial update; an empty or masked secret keeps the stored value. */
  async updateNotificationChannel(id: number, input: PersonalChannelUpdateInput): Promise<PersonalChannel> {
    const response = await apiClient.put<Record<string, unknown>>(`/api/v1/me/notification-channels/${id}`, {
      name: input.name,
      enabled: input.enabled,
      config: input.config,
    });
    return parseChannel(response.data);
  },

  /** Returns the remaining channels. */
  async deleteNotificationChannel(id: number): Promise<PersonalChannelsResponse> {
    const response = await apiClient.delete<Record<string, unknown>>(`/api/v1/me/notification-channels/${id}`);
    return parseChannels(response.data);
  },

  /** Sends the fixed test message. A failed delivery is `success: false`; the same channel can be tested every ~10 s. */
  async testNotificationChannel(id: number): Promise<ChannelTestResult> {
    const response = await apiClient.post<Record<string, unknown>>(`/api/v1/me/notification-channels/${id}/test`);
    return toCamelCase<ChannelTestResult>(response.data);
  },

  /** Effective next-day plan / pre-open switches and times plus the allowed range. */
  async getSchedule(): Promise<MeSchedule> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/me/schedule');
    return toCamelCase<MeSchedule>(response.data);
  },
};
