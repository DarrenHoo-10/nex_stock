import { getParsedApiError } from '../../../api/error';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { financeApi as api, type Feed, type Models, type Source, type Task, type Topic } from '../../../api/financeAssistant';
import { usePlanUiStore, type AssistantContext } from '../../../stores/planUiStore';
import { GlassDrawer } from '../GlassDrawer';
import { financeSource } from './financeSource';
import '../../../styles/finance-assistant.css';
export function FinanceAssistantBoundary({ legacy, context, drawer = false }: {
    legacy: ReactNode;
    context?: AssistantContext;
    drawer?: boolean;
}) {
    const open = usePlanUiStore(s => s.assistantOpen), close = usePlanUiStore(s => s.setAssistantOpen);
    const wrap = (content: ReactNode) => drawer ? <GlassDrawer open={open} onClose={() => close(false)} title="金融助手" width={560}>{content}</GlassDrawer> : content;
    const [status, setStatus] = useState<{
        managed: boolean;
        bound: boolean;
    }>(), [error, setError] = useState('');
    useEffect(() => { let active = true; void api.status().then(value => { if (active)
        setStatus(value); }).catch(() => { if (active)
        setError('无法确认金融助手连接状态'); }); return () => { active = false; }; }, []);
    if (error)
        return wrap(<div role="alert">{error}<button type="button" onClick={() => { setError(''); void api.status().then(setStatus).catch(() => setError('无法确认金融助手连接状态')); }}>重试连接</button></div>);
    if (!status)
        return wrap(<div role="status">正在连接金融助手…</div>);
    if (!status.managed)
        return <>{legacy}</>;
    if (!status.bound)
        return wrap(<div className="finance-connect"><h2>金融助手</h2><button type="button" className="btn" onClick={() => void api.connect().then(setStatus).catch(() => setError('连接未完成，请重新登录后重试'))}>连接金融助手</button></div>);
    return wrap(<ManagedFinanceAssistant key={JSON.stringify(financeSource(context))} source={financeSource(context)} compact={drawer}/>);
}
function ManagedFinanceAssistant({ source, compact = false }: {
    source?: Source; compact?: boolean;
}) {
    const [topics, setTopics] = useState<Topic[]>([]), [selected, setSelected] = useState<string | null>(null), [drafting, setDrafting] = useState(true), [draft, setDraft] = useState(''), [feed, setFeed] = useState<Feed>({ messages: [], streaming: [] }), [tasks, setTasks] = useState<Task[]>([]), [models, setModels] = useState<Models>(), [sending, setSending] = useState(false), [error, setError] = useState(''), [editing, setEditing] = useState<string>();
    const current = useRef<string | null>(null), epoch = useRef(0), read = useRef(new Map<string, number>()), drafts = useRef(new Map<string, string>()), request = useRef<{
        text: string;
        topic: string | null;
        edit?: string;
        id: string;
    } | null>(null), retryIds = useRef(new Map<string, string>());
    const active = topics.find(t => t.conversationId === selected), selection = models?.selection, connection = models?.connections.find(c => c.id === selection?.selected_connection), chosen = selection?.selected_model || connection?.defaultModel || '', efforts = connection?.options[chosen]?.reasoning ?? [];
    async function refresh() {
        const version = ++epoch.current, topic = current.current;
        try {
            const [list, next, history, catalog] = await Promise.all([api.topics(), topic ? api.messages(topic) : Promise.resolve({ messages: [], streaming: [] }), topic ? api.tasks(topic) : Promise.resolve([]), api.models()]);
            if (version !== epoch.current || topic !== current.current)
                return;
            setTopics(list);
            setFeed(next);
            setTasks(history);
            setModels(catalog);
            const seq = next.messages.at(-1)?.seq ?? 0;
            if (topic && seq > (read.current.get(topic) ?? 0) && document.hasFocus()) {
                await api.action('read', { conversationId: topic, seq });
                read.current.set(topic, seq);
            }
        }
        catch (failure) {
            if (version === epoch.current)
                setError(getParsedApiError(failure).message);
        }
    }
    useEffect(() => { void refresh(); const timer = setInterval(() => void refresh(), 2000); return () => { clearInterval(timer); }; }, []);
    function open(topic: string | null, isDraft = false) { drafts.current.set(selected ?? 'new', draft); ++epoch.current; current.current = topic; setSelected(topic); setDrafting(isDraft); setDraft(drafts.current.get(topic ?? 'new') ?? ''); setFeed({ messages: [], streaming: [] }); setTasks([]); setEditing(undefined); request.current = null; setError(''); void refresh(); }
    async function send() {
        const text = draft.trim(), topic = selected;
        if (!text || sending || (!topic && !drafting))
            return;
        if (request.current?.text !== text || request.current.topic !== topic || request.current.edit !== editing)
            request.current = { text, topic, edit: editing, id: crypto.randomUUID() };
        setSending(true);
        setError('');
        try {
            const result = await api.action<{
                conversationId: string;
            }>(editing ? 'edit' : 'input', { text, clientId: request.current.id, ...(editing ? { messageId: editing } : {}), ...(topic ? { conversationId: topic } : { newConversation: true, ...(source ? { sourceContext: source } : {}) }) });
            request.current = null;
            drafts.current.delete(topic ?? 'new');
            if (current.current === topic) {
                current.current = result.conversationId;
                setSelected(result.conversationId);
                setDrafting(false);
                setDraft('');
                setEditing(undefined);
                await refresh();
            }
        }
        catch (failure) {
            setError(getParsedApiError(failure).message);
        }
        finally {
            setSending(false);
        }
    }
    async function model(connectionId: string, model: string, reasoning?: string) { try {
        await api.action('model', { connectionId, model, version: selection?.model_selection_version ?? 0, ...(reasoning ? { reasoning } : {}) });
        await refresh();
    }
    catch (failure) {
        setError(getParsedApiError(failure).message);
    } }
    const [sessionsOpen, setSessionsOpen] = useState(false);
    function selectSession(id: string | null) {
        open(id, id === null);
        setSessionsOpen(false);
    }
    return <section className={'managed-finance' + (compact ? ' finance-compact' : '')} aria-label="金融助手话题">
      <button className="finance-session-toggle btn sm" type="button" aria-expanded={sessionsOpen} onClick={() => setSessionsOpen(!sessionsOpen)}>会话列表</button>
      <nav className="finance-sessions" aria-label="金融助手会话" data-open={sessionsOpen}>
        <button type="button" className="btn sm" onClick={() => selectSession(null)}>＋ 新对话</button>
        {topics.map(topic => <button key={topic.conversationId} type="button" aria-current={selected === topic.conversationId ? 'page' : undefined} onClick={() => selectSession(topic.conversationId)}>
          <strong>{topic.title}</strong><small>{topic.audience === 'public' ? '飞书话题' : '私人会话'}{topic.unread_count ? ' · ' + topic.unread_count + ' 未读' : ''}</small>
        </button>)}
      </nav>
      <div className="finance-chat">{error ? <div role="alert">{error}<button type="button" aria-label="关闭错误" onClick={()=>setError('')}>关闭</button></div> : null}
 <div className="finance-models"><select aria-label="金融模型" value={connection ? JSON.stringify([connection.id, selection?.selected_model ?? '']) : ''} onChange={e => { const [id, name] = JSON.parse(e.target.value) as string[]; void model(id, name); }}><option value="" disabled>选择模型</option>{models?.connections.filter(c => c.enabled && c.configured).flatMap(c => [<option key={c.id} value={JSON.stringify([c.id, ''])}>{c.name} · 默认模型</option>, ...Array.from(new Set([c.defaultModel, ...(c.models ?? []), ...Object.keys(c.options)])).filter(Boolean).map(name => <option key={c.id + name} value={JSON.stringify([c.id, name])}>{c.name} · {c.names?.[name] ?? name}</option>)])}</select><select aria-label="金融思考强度" value={selection?.selected_reasoning ?? 'default'} onChange={e => void model(connection?.id ?? '', selection?.selected_model ?? '', e.target.value)} disabled={!connection}><option value="default" disabled>默认思考强度</option><option value="off">关闭思考</option>{efforts.filter(e => e !== 'off').map(e => <option key={e} value={e}>{e}</option>)}</select></div>
 <div className="finance-transcript"><h3>{active?.title ?? '新对话'}</h3><small>{drafting ? '私人草稿' : active?.audience === 'public' ? `回复到飞书话题${typeof active.sourceContext.chatName === 'string' ? ' · ' + active.sourceContext.chatName : ''}` : '私人话题'}</small>{drafting && source ? <p>来源：{source.name ?? source.code ?? source.id}{source.tradeDate ? ' · ' + source.tradeDate : ''}</p> : null}{feed.messages.map(m => <article key={m.id} className={'finance-message ' + m.role}><strong>{m.role === 'user' ? '我' : '金融助手'}</strong><Markdown remarkPlugins={[remarkGfm]}>{m.content}</Markdown>{m.input_status === 'pending' ? <button type="button" onClick={() => { setEditing(m.id); setDraft(m.content); request.current = null; }}>编辑排队消息</button> : null}</article>)}{feed.streaming.map(item => <article key={item.taskId} role="status"><Markdown remarkPlugins={[remarkGfm]}>{item.text}</Markdown></article>)}{active?.activeTask && !feed.streaming.length ? <p role="status">正在研究…</p> : null}</div><details className="finance-task-list"><summary>任务记录 · {tasks.length}</summary>{tasks.map(task => <div key={task.id}><strong>{task.status}</strong> {task.objective.slice(0, 100)}{task.effects.map(effect => <small key={effect.id}>{effect.tool_name} · {effect.status}</small>)}{['failed', 'cancelled'].includes(task.status) ? <button type="button" onClick={() => { const id = retryIds.current.get(task.id) ?? crypto.randomUUID(); retryIds.current.set(task.id, id); void api.action('retry', { conversationId: selected, taskId: task.id, clientId: id }).then(() => { retryIds.current.delete(task.id); return refresh(); }).catch(e => setError(getParsedApiError(e).message)); }}>重试任务</button> : null}</div>)}</details><form className="finance-composer" onSubmit={e => { e.preventDefault(); void send(); }}><textarea aria-label="金融助手消息" value={draft} onChange={e => setDraft(e.target.value)} placeholder={editing ? '编辑排队消息…' : '继续研究…'} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault();
        void send();
    } }}/><button className="btn" disabled={sending || !draft.trim()}>{sending ? '发送中…' : '发送'}</button>{selected && active?.activeTask ? <button type="button" className="btn" onClick={() => void api.action('cancel', { conversationId: selected }).then(refresh).catch(e => setError(getParsedApiError(e).message))}>停止本话题</button> : null}</form></div>
 </section>;
}
