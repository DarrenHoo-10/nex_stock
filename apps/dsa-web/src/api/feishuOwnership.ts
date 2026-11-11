import apiClient from './index';

export interface FeishuOwnership {
  managed: boolean;
  owner: 'stock' | 'paused' | 'core';
  version: number;
  appId?: string | null;
  currentAppId?: string | null;
  appliesToCurrentApp: boolean;
  settingsUrl?: string | null;
}

export function safeSettingsUrl(value: unknown): string | null {
  if (typeof value !== 'string' || !value) return null;
  try {
    const url = new URL(value);
    const local = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
    const query = [...url.searchParams.entries()];
    if (url.username || url.password || url.hash || !(url.protocol === 'https:' || local && url.protocol === 'http:')
      || !(query.length === 0 || query.length === 1 && query[0][0] === 'settings' && query[0][1] === 'feishu')) return null;
    return url.href;
  } catch { return null; }
}

export const feishuOwnershipApi = {
  async status(): Promise<FeishuOwnership> {
    try {
      const { data } = await apiClient.get<FeishuOwnership>('/api/v1/finance/assistant/feishu-settings');
      if (typeof data.managed !== 'boolean' || !['stock', 'paused', 'core'].includes(data.owner)
        || !Number.isInteger(data.version) || typeof data.appliesToCurrentApp !== 'boolean') {
        throw new Error('飞书管理状态响应无效');
      }
      return { ...data, settingsUrl: safeSettingsUrl(data.settingsUrl) };
    } catch (error) {
      if (error && typeof error === 'object' && 'response' in error && (error.response as { status?: number })?.status === 404) {
        return { managed: false, owner: 'stock', version: 0, appliesToCurrentApp: false };
      }
      throw error;
    }
  },
};
