import { describe, expect, it } from 'vitest';
import { parseApiError } from '../error';
import {
  CHANNEL_ERROR_CODES,
  CHANNEL_ERROR_TEXT,
  getApiErrorCode,
  getApiErrorIssues,
  isChannelErrorCode,
  isMultiUserErrorCode,
} from '../multiUserErrors';
import { UI_TEXT } from '../../i18n/uiText';

const axiosLike = (status: number, data: unknown) => ({ response: { status, data } });

describe('channel / schedule error helpers', () => {
  it('has a zh and an en text for every channel error code', () => {
    for (const code of CHANNEL_ERROR_CODES) {
      const text = CHANNEL_ERROR_TEXT[code];
      for (const language of ['zh', 'en'] as const) {
        expect(UI_TEXT[language][text.title]).toBeTruthy();
        expect(UI_TEXT[language][text.message]).toBeTruthy();
      }
    }
  });

  it('reads the code from the body, from a nested detail, or from a wrapped request error', () => {
    expect(getApiErrorCode(axiosLike(400, { error: 'unsafe_target', message: 'x' }))).toBe('unsafe_target');
    expect(getApiErrorCode(axiosLike(400, { detail: { error: 'invalid_config' } }))).toBe('invalid_config');
    expect(getApiErrorCode({ parsedError: { code: 'channel_not_found' } })).toBe('channel_not_found');
    expect(getApiErrorCode(axiosLike(500, 'plain text'))).toBeNull();
    expect(getApiErrorCode(null)).toBeNull();
  });

  it('reads the issues with the allowed time range of the schedule errors', () => {
    const issues = getApiErrorIssues(
      axiosLike(400, {
        error: 'plan_time_too_early',
        message: 'early',
        detail: {
          issues: [
            { key: 'DAILY_PLAN_TIME', code: 'plan_time_too_early', message: 'm', severity: 'error', earliest_time: '17:30' },
            { key: 'PREOPEN_PUSH_TIME', code: 'preopen_time_too_late', message: 'm', latest_time: '09:00' },
            { code: 'no_key' },
          ],
        },
      }),
    );

    expect(issues).toEqual([
      { key: 'DAILY_PLAN_TIME', code: 'plan_time_too_early', message: 'm', earliestTime: '17:30' },
      { key: 'PREOPEN_PUSH_TIME', code: 'preopen_time_too_late', message: 'm', latestTime: '09:00' },
    ]);
    expect(getApiErrorIssues(axiosLike(400, { error: 'x' }))).toEqual([]);
  });

  it('recognises exactly the channel codes', () => {
    expect(isChannelErrorCode('rate_limited')).toBe(true);
    expect(isChannelErrorCode('internal_error')).toBe(false);
    expect(isChannelErrorCode(undefined)).toBe(false);
  });

  it('does not rewrite codes that other endpoints share (the login also answers rate_limited)', () => {
    for (const code of ['rate_limited', 'invalid_config', 'unsafe_target', 'plan_time_too_early', 'preopen_time_too_late']) {
      expect(isMultiUserErrorCode(code)).toBe(false);
      const parsed = parseApiError(axiosLike(429, { error: code, message: 'server text' }));
      expect(parsed.code).toBeUndefined();
      expect(parsed.message).toBe('server text');
    }
  });
});
