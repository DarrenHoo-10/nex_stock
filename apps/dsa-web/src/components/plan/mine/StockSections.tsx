import { useState, type ReactNode } from 'react';
import { RefreshCw } from 'lucide-react';
import { stocksApi } from '../../../api/stocks';
import { SegmentedControl } from '../motion/SegmentedControl';
import { ResearchLibraryPanel } from './ResearchLibraryPanel';
import { StockAnalysisPanel } from './StockAnalysisPanel';
import { useResource } from './useMineData';

import { sections, type StockSection } from './stockSectionsConfig';

function Briefs({ code, section, refreshKey }: { code: string; section: 'events' | 'news'; refreshKey: number }) {
  const { state, reload } = useResource(() => stocksApi.getBriefs(code, section), `${code}:${section}:${refreshKey}`);
  if (state.status === 'loading') return <p className="note">正在读取…</p>;
  if (state.status !== 'ready') return <button className="btn quiet" onClick={() => reload()}>读取失败，重试</button>;
  if (!state.data.length) return <p className="note">{section === 'events' ? '近 90 天暂无已收录的公司公告或重大事件。' : '近 30 天暂无已收录的重点相关新闻。'}</p>;
  return <div className="research-library">
    {section === 'news' && <p className="note">媒体报道，内容有待核实。</p>}
    <ul>{state.data.map((item, i) => <li key={item.url || `${item.date}-${i}`}>
      <div className="research-meta"><time>{item.date.slice(0, 10)}</time><span>{item.source}</span><span>{item.label}</span></div>
      <h3>{item.url ? <a href={item.url} target="_blank" rel="noopener noreferrer">{item.title}</a> : item.title}</h3>
    </li>)}</ul>
  </div>;
}

export function StockSections({ code, section, onSection, onRefreshPlan, children }: {
  code: string; section: StockSection; onSection: (next: StockSection) => void; onRefreshPlan: () => void; children: ReactNode;
}) {
  const [refreshKey, setRefreshKey] = useState(0);
  function refresh() {
    if (section === 'plan') onRefreshPlan();
    else setRefreshKey(value => value + 1);
  }
  return <>
    <div className="stock-section-toolbar">
      <SegmentedControl ariaLabel="个股详情" className="stock-sections" options={sections} value={section} onChange={onSection} />
      <button type="button" className="btn quiet sm stock-section-refresh" onClick={refresh}>
        <RefreshCw size={15} aria-hidden="true" />刷新
      </button>
    </div>
    {section === 'analysis' && <StockAnalysisPanel key={code} code={code} refreshKey={refreshKey} />}
    {section === 'plan' && children}
    {section === 'research' && <ResearchLibraryPanel code={code} refreshKey={refreshKey} />}
    {(section === 'events' || section === 'news') && <Briefs code={code} section={section} refreshKey={refreshKey} />}
  </>;
}
