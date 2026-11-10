import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  CurrentUser,
  PersonalSettingUpdateItem,
  PersonalSettingsResponse,
  TestModelResult,
} from '../types/me';
import type { UsageDashboard, UsagePeriod } from './usage';

/** Current user, personal settings and own usage (`/api/v1/me/*`). */
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
};
