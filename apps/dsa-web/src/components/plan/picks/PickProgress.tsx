import type React from 'react';

/** Progress of a running screening task. `progress` null (or 0) shows an indeterminate bar. */
export const PickProgress: React.FC<{ progress: number | null; message: string }> = ({ progress, message }) => {
  const known = typeof progress === 'number' && progress > 0;
  return (
    <div className="pick-progress" data-testid="pick-progress">
      <div
        className="bar"
        role="progressbar"
        aria-label="选股进度"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={known ? Math.round(progress) : undefined}
      >
        <i className={known ? '' : 'indeterminate'} style={known ? { width: `${progress}%` } : undefined} />
      </div>
      <p role="status" aria-live="polite">
        {message || '正在选股…'}
        {known ? `（${Math.round(progress)}%）` : ''}
      </p>
    </div>
  );
};
