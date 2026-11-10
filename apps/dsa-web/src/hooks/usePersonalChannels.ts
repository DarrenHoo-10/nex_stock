import { useCallback, useEffect, useState } from 'react';
import { meApi } from '../api/me';
import type { PersonalChannel, PersonalChannelsResponse } from '../types/me';

export interface PersonalChannelsState {
  data: PersonalChannelsResponse | null;
  isLoading: boolean;
  /** The raw failure of the last load, rendered by `PersonalApiError`. */
  loadError: unknown;
  reload: () => Promise<void>;
  /** Replace the whole list (the body of a DELETE is the remaining channels). */
  replaceAll: (next: PersonalChannelsResponse) => void;
  /** Insert a created channel or replace an updated one in place. */
  upsert: (channel: PersonalChannel) => void;
}

/**
 * The personal push channels, loaded once and shared by the channel list and the schedule section (which needs
 * to know whether anything is enabled to push to). A failed load never breaks the rest of the personal settings.
 */
export function usePersonalChannels(): PersonalChannelsState {
  const [data, setData] = useState<PersonalChannelsResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>(null);

  const reload = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      setData(await meApi.listNotificationChannels());
    } catch (error) {
      setLoadError(error);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const upsert = useCallback((channel: PersonalChannel) => {
    setData((current) => {
      if (!current) return current;
      const exists = current.channels.some((item) => item.id === channel.id);
      return {
        ...current,
        channels: exists
          ? current.channels.map((item) => (item.id === channel.id ? channel : item))
          : [...current.channels, channel],
      };
    });
  }, []);

  return { data, isLoading, loadError, reload, replaceAll: setData, upsert };
}
