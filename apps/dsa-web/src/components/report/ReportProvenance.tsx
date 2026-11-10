import type React from 'react';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { useCurrentUser } from '../../hooks/useCurrentUser';
import type { AnalysisReuseInfo, ReportMeta } from '../../types/analysis';
import { Badge } from '../common/Badge';
import { RequesterBadges } from '../history/RequesterBadges';

/**
 * Where the report on screen came from: its requester / 私有 badge, and whether the last analysis of it returned an
 * existing shared report ("复用已有报告", no model call) or re-used inputs already collected the same trading day
 * ("已复用数据", the model still wrote a fresh report). Renders nothing when there is nothing to say.
 */
export const ReportProvenance: React.FC<{
  meta: Pick<ReportMeta, 'requesterName' | 'requesterUserId' | 'isMine' | 'isPrivate'>;
  reuse?: AnalysisReuseInfo | null;
}> = ({ meta, reuse }) => {
  const { t } = useUiLanguage();
  const { isMultiUser } = useCurrentUser();
  const showMetadata = Boolean(reuse && !reuse.reused && reuse.metadataReusedFrom);
  const showReused = Boolean(reuse?.reused);

  const showRequester = isMultiUser && (meta.isMine === true || meta.isMine === false || meta.isPrivate === true);
  if (!showRequester && !showReused && !showMetadata) {
    return null;
  }
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="report-provenance">
      {showRequester ? <RequesterBadges info={meta} /> : null}
      {showReused ? (
        <Badge variant="success" size="sm" aria-label={t('history.reusedReportHint')} data-testid="reused-report-badge">
          {t('history.reusedReport')}
        </Badge>
      ) : null}
      {showMetadata ? (
        <Badge variant="info" size="sm" aria-label={t('history.reusedMetadataHint')} data-testid="reused-metadata-badge">
          {t('history.reusedMetadata')}
        </Badge>
      ) : null}
    </div>
  );
};
