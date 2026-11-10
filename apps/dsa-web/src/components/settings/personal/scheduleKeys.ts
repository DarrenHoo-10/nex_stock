export const PLAN_ENABLED_KEY = 'DAILY_PLAN_ENABLED';
export const PLAN_TIME_KEY = 'DAILY_PLAN_TIME';
export const PREOPEN_ENABLED_KEY = 'PREOPEN_PUSH_ENABLED';
export const PREOPEN_TIME_KEY = 'PREOPEN_PUSH_TIME';

/** The personal settings that belong to the 推送与定时 section; the generic push group no longer lists them. */
export const SCHEDULE_SETTING_KEYS: readonly string[] = [
  PLAN_ENABLED_KEY,
  PLAN_TIME_KEY,
  PREOPEN_ENABLED_KEY,
  PREOPEN_TIME_KEY,
];
