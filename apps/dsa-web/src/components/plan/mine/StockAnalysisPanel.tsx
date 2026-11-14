import { useEffect, useState } from 'react';
import { agentApi } from '../../../api/agent';
import { analysisApi, DuplicateTaskError } from '../../../api/analysis';
import { getParsedApiError } from '../../../api/error';
import { historyApi } from '../../../api/history';
import { ReportMarkdownBody } from '../../report/ReportMarkdownBody';
import { useResource } from './useMineData';

export function StockAnalysisPanel({ code, refreshKey }: { code: string; refreshKey: number }) {
  const [skill, setSkill] = useState('');
  const [page, setPage] = useState(1);
  const [record, setRecord] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [task, setTask] = useState('');
  const [message, setMessage] = useState('');
  const skills = useResource(record ? null : agentApi.getSkills, 'skills', refreshKey);
  const history = useResource(record ? null : () => historyApi.getList({ stockCode: code, page, limit: 10 }), `${code}:${page}:${refreshKey}`);
  const report = useResource(record ? () => historyApi.getMarkdown(record) : null, `${record}:${refreshKey}`);
  const refresh = history.reload;

  useEffect(() => {
    if (!task) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await analysisApi.getStatus(task);
        if (stopped) return;
        if (result.status === 'completed') {
          setTask('');
          setMessage('分析已完成');
          setPage(1);
          refresh();
        } else if (result.status === 'failed' || result.status === 'cancelled') {
          setTask('');
          setMessage(result.error || '分析任务未完成');
        } else {
          timer = setTimeout(poll, 3000);
        }
      } catch (error) {
        if (!stopped) {
          setTask('');
          setMessage(getParsedApiError(error).message);
        }
      }
    }
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [task, refresh]);

  async function generate() {
    if (!skill || task || submitting) return;
    setSubmitting(true);
    setMessage('');
    try {
      const result = await analysisApi.analyzeAsync({ stockCode: code, skills: [skill], forceRegenerate: true, notify: false });
      const id = 'taskId' in result ? result.taskId : result.accepted[0]?.taskId ?? result.duplicates[0]?.existingTaskId;
      if (id) setTask(id);
      else setMessage('分析任务未创建');
    } catch (error) {
      if (error instanceof DuplicateTaskError) setTask(error.existingTaskId);
      else setMessage(getParsedApiError(error).message);
    } finally {
      setSubmitting(false);
    }
  }

  if (record) return (
    <section className="stock-analysis">
      <button type="button" className="btn quiet" onClick={() => setRecord(null)}>‹ 返回记录</button>
      {report.state.status === 'ready' ? <ReportMarkdownBody content={report.state.data} /> :
        report.state.status === 'loading' ? <p className="note">正在读取详情…</p> :
        <button type="button" className="btn quiet" onClick={() => report.reload()}>详情读取失败，重试</button>}
    </section>
  );

  return (
    <section className="stock-analysis">
      <div className="analysis-controls">
        <select aria-label="请选择策略" value={skill} onChange={e => setSkill(e.target.value)}>
          <option value="" disabled>请选择策略</option>
          {skills.state.status === 'ready' && skills.state.data.skills.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <button type="button" className="btn primary" disabled={!skill || submitting || Boolean(task)} onClick={generate}>
          {task || submitting ? '正在生成…' : '生成分析'}
        </button>
      </div>
      {skills.state.status === 'ready' && !skills.state.data.skills.length && <p className="note">暂无可用策略</p>}
      {skills.state.status !== 'loading' && skills.state.status !== 'ready' &&
        <button className="btn quiet" onClick={() => skills.reload()}>策略读取失败，重试</button>}
      {message && <p role="status" className="note">{message}</p>}
      {history.state.status === 'ready' ? <>
        {history.state.data.items.length === 0 ? <p className="note">暂无个股分析记录</p> :
          <ul className="stock-record-list" aria-label="历史分析">
            {history.state.data.items.map(item => <li key={item.id}>
              <button type="button" onClick={() => setRecord(item.id)}>
                <span className="research-meta">{item.createdAt.replace('T', ' ').slice(0, 16)}</span>
                <strong>{item.actionLabel || item.operationAdvice || '个股分析'}</strong>
                {item.analysisSummary && <span className="analysis-excerpt">{item.analysisSummary}</span>}
              </button>
            </li>)}
          </ul>}
        {history.state.data.total > 10 && <div className="actions">
          <button className="btn" disabled={page === 1} onClick={() => setPage(page - 1)}>上一页</button>
          <button className="btn" disabled={page * 10 >= history.state.data.total} onClick={() => setPage(page + 1)}>下一页</button>
        </div>}
      </> : history.state.status === 'loading' ? <p className="note">正在读取分析记录…</p> :
        <button className="btn quiet" onClick={() => history.reload()}>记录读取失败，重试</button>}
    </section>
  );
}
