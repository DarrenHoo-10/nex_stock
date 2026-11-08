import { getParsedApiError } from '../../../api/error';
import { isPlanServiceUnavailable } from '../../../api/dailyPlan';

/** Maps a failed `PUT /plan/holdings` to the sentence shown inline (400/422 carry the server's Chinese reason). */
export function holdingsSaveErrorText(error: unknown): string {
  if (isPlanServiceUnavailable(error)) {
    return '次日计划服务未就绪，暂时无法保存。';
  }
  const parsed = getParsedApiError(error);
  if (parsed.status === 400 || parsed.status === 422) {
    return parsed.message;
  }
  return parsed.message ? `保存失败：${parsed.message}` : '保存失败，请稍后重试。';
}
