import { stocksApi } from '../../../api/stocks';
import { useResource } from './useMineData';

export function ResearchLibraryPanel({ code, refreshKey }: { code: string; refreshKey: number }) {
  const { state, reload } = useResource(() => stocksApi.getResearchLibrary(code), `${code}:${refreshKey}`);
  if (state.status === 'loading') return <p className="note" role="status">正在读取研报…</p>;
  if (state.status !== 'ready') {
    return (
      <section className="empty sm" role="alert">
        <p>研报列表读取失败</p>
        <button type="button" className="btn" onClick={() => reload()}>重试</button>
      </section>
    );
  }

  const { reports, eastmoney } = state.data;
  return (
    <section className="research-library" aria-label="券商研报">
      {eastmoney.status !== 'ok' && (
        <p className="note" role="status">
          {eastmoney.status === 'rate_limited' ? '按需查询次数已达上限。' : '东方财富研报暂时无法更新。'}
          {eastmoney.syncedAt && `最近同步于 ${new Date(eastmoney.syncedAt).toLocaleDateString('zh-CN')}。`}
        </p>
      )}
      {reports.length === 0 ? (
        <p className="note">{eastmoney.status === 'ok' ? '近三年暂无这只股票的券商研报。' : '暂无已缓存的研报。'}</p>
      ) : (
        <ul aria-label="研报列表">
          {reports.map(({ payload, title, eventDate, source }) => {
            const heading = payload.title || title;
            const eps = payload.forecasts?.find((forecast) => forecast.eps != null);
            return (
              <li key={payload.infoCode ?? JSON.stringify([eventDate, payload.org, heading])}>
                <div className="research-meta">
                  <time dateTime={eventDate}>{eventDate}</time>
                  {payload.org && <span>{payload.org}</span>}
                  {payload.rating && <span className="tag line">{payload.rating}</span>}
                </div>
                <h3>{heading}</h3>
                <div className="research-meta">
                  <span>{source === 'eastmoney' ? '东方财富' : 'Tushare'}</span>
                  {payload.pages && <span>{payload.pages} 页</span>}
                  {eps && <span>{eps.quarter} 预测 EPS {eps.eps} 元</span>}
                  {payload.pdfUrl && (
                    <a href={payload.pdfUrl} target="_blank" rel="noopener noreferrer" aria-label={`查看原文：${heading}`}>
                      查看原文 ↗
                    </a>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
