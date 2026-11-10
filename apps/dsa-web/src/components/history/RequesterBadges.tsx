import type React from 'react';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { useCurrentUser } from '../../hooks/useCurrentUser';
import { Badge } from '../common/Badge';

export type RequesterInfo = {
  requesterName?: string | null;
  requesterUserId?: string | null;
  isMine?: boolean | null;
  isPrivate?: boolean | null;
};

/**
 * Who requested a shared report, and the 私有 badge for reports generated with personal context (holdings ...),
 * visible only to their requester (docs/design/multi-user.md section 7). Renders nothing in local mode, where
 * there is only one person and the history looks exactly as before.
 */
export const RequesterBadges: React.FC<{ info: RequesterInfo; className?: string }> = ({ info, className }) => {
  const { t } = useUiLanguage();
  const { isMultiUser } = useCurrentUser();
  if (!isMultiUser) {
    return null;
  }
  const isMine = info.isMine === true;
  const name = info.requesterName?.trim() || info.requesterUserId?.trim() || '';
  const hasAttribution = info.isMine === true || info.isMine === false;
  if (!hasAttribution && !info.isPrivate) {
    return null;
  }
  return (
    <span className={className ?? 'inline-flex flex-wrap items-center gap-1'} data-testid="requester-badges">
      {hasAttribution ? (
        <Badge
          variant={isMine ? 'info' : 'default'}
          size="sm"
          className="shrink-0 shadow-none text-[10px] leading-none"
          data-testid="requester-badge"
        >
          {isMine ? t('history.requestedByMe') : t('history.requestedBy', { name: name || t('history.requestedByUnknown') })}
        </Badge>
      ) : null}
      {info.isPrivate ? (
        <Badge
          variant="warning"
          size="sm"
          className="shrink-0 shadow-none text-[10px] leading-none"
          aria-label={t('history.privateHint')}
          data-testid="private-badge"
        >
          {t('history.private')}
        </Badge>
      ) : null}
    </span>
  );
};
