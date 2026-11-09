import { useEffect, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { PlanPageHeader } from '../../components/plan/PlanPageHeader';
import '../../styles/plan-closing-analysis.css';
import { closingAnalysisApi } from '../../api/closingAnalysis';
import { useUiLanguage } from '../../contexts/UiLanguageContext';

function DailyReport({ day }: { day: string }) {
  const { t } = useUiLanguage();
  const [report, setReport] = useState<Awaited<ReturnType<typeof closingAnalysisApi.get>>>();
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const request = new AbortController();
    closingAnalysisApi.get(day, request.signal).then((data) => {
      if (!request.signal.aborted) setReport(data);
    }).catch(() => {
      if (!request.signal.aborted) setFailed(true);
    });
    return () => request.abort();
  }, [day, attempt]);

  if (failed) return <LoadError retry={() => { setFailed(false); setAttempt(attempt + 1); }} />;
  if (!report) return <p role="status">{t('common.loading')}</p>;
  return (
    <>
      {([
        ['closing.plan', report.daily_plan],
        ['closing.market', report.market_review],
        ['closing.stocks', report.stock_summary],
      ] as const).map(([label, content]) => content && (
        <section key={label} className="win closing-report" aria-label={t(label)}>
          <h2>{t(label)}</h2>
          <div className="closing-body"><Markdown remarkPlugins={[remarkGfm]}>{content}</Markdown></div>
        </section>
      ))}
      {!report.daily_plan && !report.market_review && !report.stock_summary && <p className="empty">{t('closing.empty')}</p>}
    </>
  );
}

function LoadError({ retry }: { retry: () => void }) {
  const { t } = useUiLanguage();
  return <div className="win empty" role="alert">
    <p>{t('closing.failed')}</p>
    <button type="button" className="btn sm" onClick={retry}>{t('common.retry')}</button>
  </div>;
}

export default function ClosingAnalysisPage() {
  const { t } = useUiLanguage();
  const title = t('layout.nav.closingAnalysis');
  useEffect(() => { document.title = `${title} - DSA`; }, [title]);
  const [dates, setDates] = useState<string[]>();
  const [selection, setSelection] = useState('');
  const [failed, setFailed] = useState(false);
  const day = selection || dates?.[0];
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const request = new AbortController();
    closingAnalysisApi.dates(request.signal).then((data) => {
      if (!request.signal.aborted) setDates(data);
    }).catch(() => {
      if (!request.signal.aborted) setFailed(true);
    });
    return () => request.abort();
  }, [attempt]);

  return (
    <>
      <PlanPageHeader hideRunStatus title={title} actions={day && (
        <label className="closing-date">
          {t('closing.date')}
          <select className="sel"
            value={day} onChange={(event) => setSelection(event.target.value)}>
            {dates?.map((date) => <option key={date}>{date}</option>)}
          </select>
        </label>
      )} />
      {failed ? <LoadError retry={() => { setFailed(false); setAttempt(attempt + 1); }} /> : !dates ? <p role="status">{t('common.loading')}</p>
        : day ? <DailyReport key={day} day={day} />
          : <div className="win empty"><h2>{t('closing.empty')}</h2><p>{t('closing.emptyHint')}</p></div>}
    </>
  );
}
