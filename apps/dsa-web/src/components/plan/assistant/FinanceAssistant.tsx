import { FinanceModelControls } from './FinanceModelControls';
import { getParsedApiError } from '../../../api/error';
import { memo, useEffect, useEffectEvent, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { financeApi as api, type Feed, type Models, type Source, type Message, type Topic } from '../../../api/financeAssistant';
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
        topics?: Topic[];
    }>(), [error, setError] = useState('');
    const visible = !drawer || open;
    if (!visible && (status || error)) { setStatus(undefined); setError(''); }
    useEffect(() => {
        if (!visible) return;
        let active = true;
        void api.status(true).then(value => { if (active)
        setStatus(value); }).catch(() => { if (active)
        setError('无法确认金融助手连接状态'); }); return () => { active = false; }; }, [visible]);
    if (error)
        return wrap(<div role="alert">{error}<button type="button" onClick={() => { setError(''); void api.status(true).then(setStatus).catch(() => setError('无法确认金融助手连接状态')); }}>重试连接</button></div>);
    if (!status)
        return wrap(<div role="status">正在连接金融助手…</div>);
    if (!status.managed)
        return <>{legacy}</>;
    if (!status.bound)
        return wrap(<div className="finance-connect"><h2>金融助手</h2><button type="button" className="btn" onClick={() => void api.connect().then(setStatus).catch(() => setError('连接未完成，请重新登录后重试'))}>连接金融助手</button></div>);
    return wrap(<ManagedFinanceAssistant key={JSON.stringify(financeSource(context))} source={financeSource(context)} compact={drawer} initialTopics={status.topics}/>);
}
function ManagedFinanceAssistant({ source, compact = false, initialTopics }: {
    source?: Source; compact?: boolean; initialTopics?: Topic[];
}) {
    const [topics, setTopics] = useState<Topic[]>(initialTopics ?? []), [selected, setSelected] = useState<string | null>(null), [drafting, setDrafting] = useState(true), [draft, setDraft] = useState(''), [feed, setFeed] = useState<Feed>({ messages: [], streaming: [] }), [models, setModels] = useState<Models>(), [sending, setSending] = useState(false), [error, setError] = useState(''), [editing, setEditing] = useState<string>();
    const current = useRef<string | null>(null), epoch = useRef(0), read = useRef(new Map<string, number>()), drafts = useRef(new Map<string, string>()), request = useRef<{
        text: string;
        topic: string | null;
        edit?: string;
        id: string;
    } | null>(null);
    const active = topics.find(t => t.conversationId === selected), selection = models?.selection;
    const [topicState, setTopicState] = useState<'loading' | 'ready' | 'failed'>(initialTopics ? 'ready' : 'loading');
    const [topicError, setTopicError] = useState(''), [savingModel, setSavingModel] = useState(false);
    const listVersion = useRef(0), catalogVersion = useRef(0), busyList = useRef(false);
    const lastFeed = useRef<Feed>({ messages: [], streaming: [] });
    const pending = useRef<Promise<void> | null>(null);
    const [loading, setLoading] = useState(false), [older, setOlder] = useState(false);
    const transcript = useRef<HTMLDivElement>(null), follow = useRef(true);
    const [outgoing, setOutgoing] = useState<{topic: string | null; text: string; clientId: string; messageId?: string}>();
    const anchor = useRef<{height: number; top: number} | null>(null), paging = useRef(false);

    function refreshList(force = false) {
        if (busyList.current && !force) return;
        const version = ++listVersion.current, topic = current.current;
        busyList.current = true;
        void api.topics().then(value => {
            if (version !== listVersion.current) return;
            setTopics(value); setTopicState('ready'); setTopicError('');
            if (topic && topic === current.current && !value.some(item => item.conversationId === topic)) discardRemovedSession(topic);
        }).catch(failure => {
            if (version === listVersion.current) { setTopicState('failed'); setTopicError(getParsedApiError(failure).message); }
        }).finally(() => { if (version === listVersion.current) busyList.current = false; });
    }
    async function loadModels() {
        const version = ++catalogVersion.current;
        try {
            const value = await api.models();
            if (version === catalogVersion.current) setModels(value);
        } catch (failure) { if (version === catalogVersion.current) setError(getParsedApiError(failure).message); }
    }
    function refresh(force = false) {
        const topic = current.current;
        if (!topic || (!force && pending.current)) return;
        const version = ++epoch.current;
        const live = () => version === epoch.current && current.current === topic;
        const work = async () => {
            const previous = lastFeed.current;
            const queued = previous.messages.find(message => message.input_status === 'pending');
            const after = queued ? Math.max(0, queued.seq - 1) : previous.messages.at(-1)?.seq;
            let next = after === undefined ? await api.messages(topic) : await api.messages(topic, {after});
            if (!live()) return;
            let append = after !== undefined && sameFeed(previous, next);
            if (after !== undefined && !append) {
                lastFeed.current = {messages: [], streaming: []};
                setFeed(lastFeed.current); setOutgoing(undefined); setLoading(true);
                next = await api.messages(topic);
                if (!live()) return;
            }
            let rows = append ? [...lastFeed.current.messages.filter(m => m.seq <= after!), ...next.messages] : next.messages;
            while (next.hasMore && next.messages.length) {
                const page = await api.messages(topic, {after: next.messages.at(-1)!.seq});
                if (!live()) return;
                if (!sameFeed(next, page)) {
                    next = await api.messages(topic);
                    if (!live()) return;
                    rows = next.messages; append = false; break;
                }
                rows.push(...page.messages); next = page;
            }
            if (!append) {
                setOlder(rows.length >= 200);
                setTopics(list => list.map(item => item.conversationId === topic
                    ? {...item, messages: rows.slice(-2)} : item));
            }
            const value = {...next, messages: rows};
            lastFeed.current = value;
            if (!append || next.messages.length || rows.length !== previous.messages.length ||
                next.streaming.length !== previous.streaming.length ||
                next.streaming.some((item, index) => item.taskId !== previous.streaming[index]?.taskId || item.text !== previous.streaming[index]?.text))
                setFeed(value);
            setLoading(false);
            setOutgoing(old => old?.topic === topic && old.messageId && rows.some(m => m.id === old.messageId) ? undefined : old);
            const seq = rows.at(-1)?.seq ?? 0;
            if (seq > (read.current.get(topic) ?? 0) && document.hasFocus()) {
                await api.action('read', { conversationId: topic, seq });
                read.current.set(topic, seq);
            }
        };
        pending.current = work().catch(failure => {
            if (live()) {
                if (!lastFeed.current.messages.length) {
                    setFeed(lastFeed.current);
                    setTopics(list => list.map(item => item.conversationId === topic ? {...item, messages: []} : item));
                }
                setLoading(false); setError(getParsedApiError(failure).message); refreshList();
            }
        }).finally(() => { if (live()) pending.current = null; });
    }
    async function loadOlder() {
        const topic = current.current, before = lastFeed.current.messages[0]?.seq;
        if (!topic || !before || paging.current) return;
        const first = lastFeed.current;
        paging.current = true;
        try {
            const page = await api.messages(topic, {before});
            if (current.current !== topic || lastFeed.current.messages[0]?.seq !== before) return;
            if (!sameFeed(first, page) || !sameFeed(lastFeed.current, page)) { refresh(true); return; }
            const el = transcript.current;
            if (el) anchor.current = {height: el.scrollHeight, top: el.scrollTop};
            const value = {...lastFeed.current, messages: [...page.messages, ...lastFeed.current.messages]};
            lastFeed.current = value; setFeed(value); setOlder(page.messages.length === 200);
        } catch (failure) { if (topic === current.current) setError(getParsedApiError(failure).message); }
        finally { paging.current = false; }
    }
    useLayoutEffect(() => {
        const el = transcript.current;
        if (!el) return;
        if (anchor.current) {
            el.scrollTop = anchor.current.top + el.scrollHeight - anchor.current.height; anchor.current = null;
        } else if (follow.current) el.scrollTop = el.scrollHeight;
    }, [feed, outgoing, selected]);
    const pollMessages = useEffectEvent(() => refresh());
    const pollList = useEffectEvent(() => refreshList());
    const loadInitialList = useEffectEvent(() => { if (!initialTopics) refreshList(); });
    const pollModels = useEffectEvent(() => { void loadModels(); });
    const invalidateRequests = useEffectEvent(() => {
        ++epoch.current; ++listVersion.current; ++catalogVersion.current; busyList.current = false; pending.current = null;
    });
    useEffect(() => {
        const visible = () => document.visibilityState !== 'hidden';
        const resume = () => { if (visible()) { pollList(); pollMessages(); pollModels(); } };
        loadInitialList(); pollModels();
        const messages = setInterval(() => { if (visible()) pollMessages(); }, 2000);
        const list = setInterval(() => { if (visible()) pollList(); }, 12000);
        document.addEventListener('visibilitychange', resume);
        return () => {
            clearInterval(messages); clearInterval(list); document.removeEventListener('visibilitychange', resume);
            invalidateRequests();
        };
    }, []);
    function resetTranscript(topic: string | null) {
        ++epoch.current; pending.current = null; follow.current = true; anchor.current = null;
        lastFeed.current = {messages: [], streaming: []};
        // Previews come from this signed-in user's list. Keep the sync cursor empty
        // until the complete, freshly authorized page arrives.
        const preview = topics.find(item => item.conversationId === topic)?.messages ?? [];
        setFeed({messages: preview, streaming: []}); setLoading(Boolean(topic)); setOlder(false); setOutgoing(undefined);
    }
    function open(topic: string | null, isDraft = false) {
        if (topic === current.current && topic !== null) return;
        drafts.current.set(current.current ?? 'new', draft);
        current.current = topic; resetTranscript(topic);
        setSelected(topic); setDrafting(isDraft); setDraft(drafts.current.get(topic ?? 'new') ?? '');
        setEditing(undefined); request.current = null; setError('');
        refresh(true);
    }
    async function send() {
        const text = draft.trim(), topic = selected, key = topic ?? 'new';
        if (!text || sending || (!topic && !drafting)) return;
        if (request.current?.text !== text || request.current.topic !== topic || request.current.edit !== editing)
            request.current = { text, topic, edit: editing, id: crypto.randomUUID() };
        const input = request.current;
        setSending(true); setError(''); setDraft(''); follow.current = true;
        setOutgoing({topic, text, clientId: input.id});
        try {
            const result = await api.action<{conversationId: string; messageId?: string}>(editing ? 'edit' : 'input', {
                text, clientId: input.id, ...(editing ? {messageId: editing} : {}),
                ...(topic ? {conversationId: topic} : {newConversation: true, ...(source ? {sourceContext: source} : {})}),
            });
            if (request.current === input) request.current = null;
            if (current.current === topic) {
                current.current = result.conversationId; setSelected(result.conversationId); setDrafting(false); setEditing(undefined);
                setOutgoing(result.messageId ? {topic: result.conversationId, text, clientId: input.id, messageId: result.messageId} : undefined);
                refresh(true);
            }
            refreshList(true);
        } catch (failure) {
            if (current.current === topic) { setOutgoing(undefined); setDraft(value => value || text); setError(getParsedApiError(failure).message); }
            else if (!drafts.current.get(key)) drafts.current.set(key, text);
        } finally { setSending(false); }
    }
    async function model(connectionId: string, name: string, reasoning?: string) {
        if (savingModel) return;
        setSavingModel(true);
        try {
            await api.action('model', { connectionId, model: name, version: selection?.model_selection_version ?? 0, reasoning });
            // Invalidate any catalog read that started before this write.
            await loadModels();
        } catch (failure) {
            setError(getParsedApiError(failure).message);
        } finally { setSavingModel(false); }
    }
    const [sessionsOpen, setSessionsOpen] = useState(false);
    const [confirmDelete, setConfirmDelete] = useState<string>(), [removing, setRemoving] = useState<string>();
    function discardRemovedSession(id: string) {
        drafts.current.delete(id);
        if (current.current !== id) return;
        current.current = null;
        request.current = null;
        setSelected(null); setDrafting(true); setDraft(drafts.current.get('new') ?? '');
        resetTranscript(null); setEditing(undefined); setError('');
    }
    async function removeSession(id: string) {
        if (removing) return;
        setRemoving(id);
        try {
            await api.action('delete', { conversationId: id });
            setConfirmDelete(undefined);
            setTopics(list => list.filter(item => item.conversationId !== id));
            discardRemovedSession(id);
            refreshList(true);
        } catch (failure) { setError(getParsedApiError(failure).message); }
        finally { setRemoving(undefined); }
    }
    function selectSession(id: string | null) {
        open(id, id === null);
        setSessionsOpen(false);
    }
    return <section className={'managed-finance' + (compact ? ' finance-compact' : '')} aria-label="金融助手话题">
      <button className="finance-session-toggle btn sm" type="button" aria-expanded={sessionsOpen} onClick={() => setSessionsOpen(!sessionsOpen)}>会话列表</button>
      <nav className="finance-sessions" aria-label="金融助手会话" data-open={sessionsOpen}>
        <button type="button" className="btn sm" onClick={() => selectSession(null)}>＋ 新对话</button>
        {topicState === 'loading' && <small role="status">正在加载会话…</small>}
        {topicState === 'ready' && !topics.length && <small>暂无历史会话</small>}
        {topicState === 'failed' && <div role="alert">会话加载失败：{topicError}<button type="button" onClick={() => refreshList(true)}>重试</button></div>}
        {topics.map(topic => <div className="finance-session-row" key={topic.conversationId}>
          <button type="button" aria-current={selected === topic.conversationId ? 'page' : undefined} onClick={() => selectSession(topic.conversationId)}>
            <strong>{topic.title}</strong><small>{topic.audience === 'public' ? '飞书话题' : '私人会话'}{topic.unread_count ? ' · ' + topic.unread_count + ' 未读' : ''}</small>
          </button>
          <SessionActions title={topic.title} onDelete={() => setConfirmDelete(topic.conversationId)}/>
          {confirmDelete === topic.conversationId && <div className="finance-remove-confirm">
            <small>从 Stock 和 NexBot 删除此会话{topic.activeTask ? '，并停止进行中的任务' : ''}？</small>
            <button type="button" disabled={Boolean(removing)} onClick={() => void removeSession(topic.conversationId)}>确认删除</button>
            <button type="button" disabled={Boolean(removing)} onClick={() => setConfirmDelete(undefined)}>取消</button>
          </div>}
        </div>)}
      </nav>
      <div className="finance-chat">{error ? <div role="alert">{error}<button type="button" aria-label="关闭错误" onClick={()=>setError('')}>关闭</button></div> : null}
 <header className="finance-chat-header"><h3>{active?.title ?? '新对话'}</h3><small>{drafting ? '私人草稿' : active?.audience === 'public' ? `回复到飞书话题${typeof active.sourceContext.chatName === 'string' ? ' · ' + active.sourceContext.chatName : ''}` : '私人话题'}</small></header>
        <div className="finance-transcript" ref={transcript} onScroll={event => {
            const el = event.currentTarget; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}>
        {drafting && source && <p>来源：{source.name ?? source.code ?? source.id}{source.tradeDate ? ' · ' + source.tradeDate : ''}</p>}
        {loading && !feed.messages.length && <small role="status">正在加载聊天记录…</small>}
        {loading && feed.messages.length > 0 && feed.messages.length < (active?.message_count ?? 0) && <small role="status">正在加载更早消息…</small>}
        {older && <button type="button" className="btn sm finance-older" onClick={() => void loadOlder()}>查看更早消息</button>}
        {feed.messages.map(m => <ChatMessage key={m.id} message={m} onEdit={() => {setEditing(m.id); setDraft(m.content); request.current = null;}}/>)}
        {outgoing && outgoing.topic === selected && !feed.messages.some(m => m.id === outgoing.messageId || m.source_key?.endsWith(':' + outgoing.clientId)) &&
          <article className="finance-message user"><div>{outgoing.text}</div></article>}
        {feed.streaming.filter(item => item.text).map(item => <article className="finance-message assistant" key={item.taskId} role="status"><ChatMarkdown text={item.text}/></article>)}
        {active?.activeTask && !feed.streaming.some(item => item.text) && <p role="status">正在研究…</p>}
        </div><form className="finance-composer" onSubmit={e => { e.preventDefault(); void send(); }}><textarea aria-label="金融助手消息" value={draft} onChange={e => setDraft(e.target.value)} placeholder={editing ? '编辑排队消息…' : '继续研究…'} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault();
        void send();
    } }}/><div className="finance-composer-actions"><FinanceModelControls catalog={models} busy={savingModel} onModel={model}/><button className="finance-send" aria-label="发送" title="发送" disabled={sending || !draft.trim()}>{sending ? '…' : '↑'}</button>{selected && active?.activeTask ? <button type="button" className="finance-stop" aria-label="停止本话题" title="停止本话题" onClick={() => void api.action('cancel', { conversationId: selected }).then(() => { refresh(true); refreshList(true); }).catch(e => setError(getParsedApiError(e).message))}>■</button> : null}</div></form></div>
 </section>;
}

function sameFeed(left: Feed, right: Feed) {
    return left.sync?.bootId === right.sync?.bootId && left.sync?.privacyEpoch === right.sync?.privacyEpoch;
}
const ChatMarkdown = memo(function ChatMarkdown({text}: {text: string}) {
    return <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>;
});
function ChatMessage({message, onEdit}: {message: Message; onEdit: () => void}) {
    return <article className={'finance-message ' + message.role}>
        <ChatMarkdown text={message.content}/>
        {message.input_status === 'pending' && <button type="button" onClick={onEdit}>编辑排队消息</button>}
    </article>;
}
function SessionActions({title, onDelete}: {title: string; onDelete: () => void}) {
    const [open, setOpen] = useState(false);
    const wrap = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null), item = useRef<HTMLButtonElement>(null);
    useEffect(() => {
        if (!open) return;
        item.current?.focus();
        function dismiss(event: PointerEvent) {
            if (event.target instanceof Node && !wrap.current?.contains(event.target)) setOpen(false);
        }
        document.addEventListener('pointerdown', dismiss);
        return () => document.removeEventListener('pointerdown', dismiss);
    }, [open]);
    return <div className="finance-session-actions" ref={wrap}
        onBlur={event => {if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);}}
        onKeyDown={event => {if (event.key === 'Escape') {setOpen(false); trigger.current?.focus();}}}>
        <button type="button" ref={trigger} title="会话操作" aria-label={'会话操作 ' + title} aria-expanded={open}
            aria-haspopup="menu" onClick={() => setOpen(!open)} onKeyDown={event => {
                if (event.key === 'ArrowDown') {event.preventDefault(); setOpen(true);}
            }}>⋯</button>
        {open && <div role="menu" aria-label="会话操作"><button type="button" ref={item} role="menuitem"
            onClick={() => {setOpen(false); onDelete();}}>删除会话</button></div>}
    </div>;
}
