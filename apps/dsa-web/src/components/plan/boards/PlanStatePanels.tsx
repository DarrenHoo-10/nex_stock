import type React from 'react';

/** Loading skeleton: `rows` shimmering bars. Decorative; the status is announced by the surrounding live region. */
export const PlanSkeleton: React.FC<{ rows?: number; label: string; className?: string }> = ({
  rows = 4,
  label,
  className,
}) => (
  <div className={className ? `skel ${className}` : 'skel'} role="status" aria-busy="true" aria-label={label}>
    {Array.from({ length: rows }, (_, index) => (
      <span key={index} className="skel-bar" style={{ width: `${92 - ((index * 13) % 38)}%` }} aria-hidden="true" />
    ))}
  </div>
);

/** 503 `daily_plan_unavailable`: the next-day plan service is not ready. */
export const PlanUnavailablePanel: React.FC<{ compact?: boolean; headingLevel?: 2 | 3 }> = ({
  compact = false,
  headingLevel = 2,
}) => {
  const Heading = headingLevel === 2 ? 'h2' : 'h3';
  return (
    <div className={compact ? 'empty sm' : 'empty'} data-testid="plan-unavailable">
      <Heading>次日计划服务未就绪</Heading>
      <p>
        后端的板块异动与次日计划服务还没有启动或加载失败，所以暂时没有数据。服务就绪后刷新页面即可，不影响其他页面。
      </p>
    </div>
  );
};

/** Load failure with a retry button. */
export const PlanErrorPanel: React.FC<{
  message: string;
  onRetry: () => void;
  compact?: boolean;
  title?: string;
}> = ({ message, onRetry, compact = false, title = '加载失败' }) => (
  <div className={compact ? 'empty sm' : 'empty'} role="alert" data-testid="plan-error">
    <h3>{title}</h3>
    <p>{message}</p>
    <div className="actions">
      <button type="button" className="btn sm" onClick={onRetry}>
        重试
      </button>
    </div>
  </div>
);
