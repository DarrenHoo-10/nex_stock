import type React from 'react';
import type { SaveOutcome } from './usePreferencesConfig';

/** Panel heading used by every settings tab. */
export const PanelHead: React.FC<{ title: string; sub?: React.ReactNode }> = ({ title, sub }) => (
  <>
    <h2>{title}</h2>
    {sub ? <p className="sub">{sub}</p> : null}
  </>
);

/** Warning box shown after a failed save; a conflict offers the reload button. */
export const SaveProblem: React.FC<{
  outcome: Exclude<SaveOutcome, { ok: true }> | null;
  onReload: () => void;
}> = ({ outcome, onReload }) => {
  if (!outcome) {
    return null;
  }
  if (outcome.kind === 'conflict') {
    return (
      <div className="notice pref-notice" role="alert">
        <b>配置已变化</b>
        <p>设置在别处被修改过，当前页面的数据已过期。重新加载后再改一次。</p>
        <button type="button" className="btn sm" onClick={onReload}>
          重新加载
        </button>
      </div>
    );
  }
  return (
    <p className="err" role="alert">
      {outcome.message}
    </p>
  );
};

/** Plain "still loading" / "could not load" states of a config-backed panel. */
export const ConfigGate: React.FC<{
  status: 'loading' | 'error';
  message?: string;
  onRetry: () => void;
}> = ({ status, message, onRetry }) =>
  status === 'loading' ? (
    <p className="note" role="status">
      正在读取设置…
    </p>
  ) : (
    <div className="notice pref-notice" role="alert">
      <b>设置读取失败</b>
      <p>{message || '请稍后重试。'}</p>
      <button type="button" className="btn sm" onClick={onRetry}>
        重试
      </button>
    </div>
  );
