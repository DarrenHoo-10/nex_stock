import { act, fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FinanceAssistantBoundary } from '../FinanceAssistant';
import { financeSource } from '../financeSource';
import AssistantPage from '../../../../pages/plan/AssistantPage';
import { AssistantDrawer } from '../AssistantDrawer';
import { usePlanUiStore } from '../../../../stores/planUiStore';
import { UiLanguageProvider } from '../../../../contexts/UiLanguageContext';
const api = vi.hoisted(() => ({ status: vi.fn(), connect: vi.fn(), topics: vi.fn(), messages: vi.fn(), tasks: vi.fn(), models: vi.fn(), action: vi.fn() }));
vi.mock('../../../../api/financeAssistant', () => ({ financeApi: api }));
const topic = { conversationId: 'same-core-topic', title: '群里的复盘', audience: 'public', sourceContext: { chatName: '合成群' }, last_activity_at: 1, message_count: 2, unread_count: 1, messages: [{ id: 'answer', role: 'assistant', content: '公开答案', seq: 2 }], activeTask: { id: 'task', status: 'running' } };
beforeEach(() => { vi.resetAllMocks(); api.status.mockResolvedValue({ managed: true, bound: true }); api.connect.mockResolvedValue({ managed: true, bound: true }); api.topics.mockResolvedValue([topic]); api.messages.mockResolvedValue({ messages: topic.messages, streaming: [] }); api.tasks.mockResolvedValue([]); api.models.mockResolvedValue({ selection: { selected_connection: 'conn', selected_model: 'chosen', selected_reasoning: 'high', model_selection_version: 3 }, connections: [{ id: 'conn', name: 'Provider', defaultModel: 'chosen', enabled: true, configured: true, models: ['chosen'], options: { chosen: { reasoning: ['low', 'high'] } } }] }); api.action.mockResolvedValue({ conversationId: topic.conversationId }); });
afterEach(cleanup);
describe('managed finance assistant', () => {
    it('shows authorized previews immediately, then replaces them with the fresh history', async () => {
        api.status.mockResolvedValue({ managed: true, bound: true, topics: [topic] });
        let finish!: (value: unknown) => void;
        api.messages.mockReturnValue(new Promise(resolve => { finish = resolve; }));
        render(<FinanceAssistantBoundary legacy={null}/>);
        fireEvent.click(await screen.findByRole('button', { name: /^群里的复盘/ }));
        expect(api.status).toHaveBeenCalledWith(true);
        expect(api.topics).not.toHaveBeenCalled();
        expect(api.messages).toHaveBeenCalledWith(topic.conversationId);
        expect(screen.getByText('公开答案')).toBeVisible();
        await act(async () => finish({ messages: [{ ...topic.messages[0], content: '完整记录' }], streaming: [] }));
        expect(screen.queryByText('公开答案')).toBeNull();
        expect(screen.getByText('完整记录')).toBeVisible();
    });
    it('renders late history even while the model catalog is pending', async () => {
        vi.useFakeTimers();
        try {
            let resolveTopics!: (topics: typeof topic[]) => void;
            api.topics.mockImplementation(() => new Promise(resolve => { resolveTopics = resolve; }));
            api.models.mockReturnValue(new Promise(() => {}));
            render(<FinanceAssistantBoundary legacy={null}/>);
            await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
            expect(api.topics).toHaveBeenCalledTimes(1);
            expect(screen.getByText('正在加载会话…')).toBeVisible();
            await act(async () => { resolveTopics([topic]); });
            expect(screen.getByRole('button', { name: /^群里的复盘/ })).toBeVisible();
            expect(screen.getByLabelText('金融模型').closest('form')).toContainElement(screen.getByLabelText('金融助手消息'));
        } finally { vi.useRealTimers(); }
    });
    it('distinguishes a list error from an empty history and retries it', async () => {
        api.topics.mockRejectedValueOnce(Error('timeout')).mockResolvedValue([]);
        render(<FinanceAssistantBoundary legacy={null}/>);
        expect(await screen.findByText(/会话加载失败/)).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: '重试' }));
        expect(await screen.findByText('暂无历史会话')).toBeVisible();
    });
    it('preserves legacy only when service explicitly says unconfigured', async () => { api.status.mockResolvedValue({ managed: false, bound: false }); render(<FinanceAssistantBoundary legacy={<div>legacy</div>}/>); expect(await screen.findByText('legacy')).toBeInTheDocument(); expect(api.topics).not.toHaveBeenCalled(); });
    it('does not silently fall back after an identity/network failure', async () => { api.status.mockRejectedValue(Error('unauthorized')); render(<FinanceAssistantBoundary legacy={<div>legacy</div>}/>); expect(await screen.findByRole('alert')).toBeInTheDocument(); expect(screen.queryByText('legacy')).not.toBeInTheDocument(); });
    it('requires explicit binding then enters the actual managed page', async () => { api.status.mockResolvedValue({ managed: true, bound: false }); render(<AssistantPage />); fireEvent.click(await screen.findByRole('button', { name: '连接金融助手' })); expect(await screen.findByText('群里的复盘')).toBeInTheDocument(); expect(api.connect).toHaveBeenCalledOnce(); });
    it('continues the selected Core conversation, scopes stop, and preserves retry IDs', async () => {
        render(<FinanceAssistantBoundary legacy={<div>legacy</div>}/>);
        fireEvent.click(await screen.findByRole('button', { name: /^群里的复盘/ }));
        expect(await screen.findByText('回复到飞书话题 · 合成群')).toBeInTheDocument();
        expect(api.messages).toHaveBeenCalledWith('same-core-topic');
        fireEvent.change(screen.getByLabelText('金融助手消息'), { target: { value: '继续研究' } });
        api.action.mockRejectedValueOnce(Error('response lost'));
        fireEvent.click(screen.getByRole('button', { name: '发送' }));
        await screen.findByText('response lost');
        const first = api.action.mock.calls.find(call => call[0] === 'input')![1];
        api.action.mockResolvedValue({ conversationId: topic.conversationId });
        fireEvent.click(screen.getByRole('button', { name: '发送' }));
        await waitFor(() => expect(api.action.mock.calls.filter(call => call[0] === 'input')).toHaveLength(2));
        expect(api.action.mock.calls.filter(call => call[0] === 'input')[1][1]).toEqual(first);
        expect(first).toMatchObject({ conversationId: 'same-core-topic', text: '继续研究' });
        expect(first).not.toHaveProperty('audience');
        fireEvent.click(await screen.findByRole('button', { name: '停止本话题' }));
        await waitFor(() => expect(api.action).toHaveBeenCalledWith('cancel', { conversationId: 'same-core-topic' }));
        fireEvent.click(screen.getByRole('button', { name: '金融思考强度' }));
        fireEvent.click(screen.getByRole('radio', { name: '低' }));
        await waitFor(() => expect(api.action).toHaveBeenCalledWith('model', { connectionId: 'conn', model: 'chosen', version: 3, reasoning: 'low' }));
    });
    it('opens the actual drawer with holding context and keeps it on first input', async () => {
        usePlanUiStore.setState({ assistantOpen: true, assistantContext: { kind: 'stock', stockCode: '600000', stockName: '合成持仓', held: true, tradeDate: '2026-11-09' } });
        render(<UiLanguageProvider><AssistantDrawer /></UiLanguageProvider>);
        expect(await screen.findByRole('dialog', { name: '金融助手' })).toBeInTheDocument();
        expect(await screen.findByText(/来源：合成持仓/)).toBeInTheDocument();
        fireEvent.change(screen.getByLabelText('金融助手消息'), { target: { value: '跨标的研究' } });
        fireEvent.click(screen.getByRole('button', { name: '发送' }));
        await waitFor(() => expect(api.action).toHaveBeenCalledWith('input', expect.objectContaining({ newConversation: true, sourceContext: { kind: 'holding', code: '600000', name: '合成持仓', tradeDate: '2026-11-09' }, text: '跨标的研究' })));
    });
    it('keeps both sessions in the sidebar while switching the transcript', async () => {
        const other = { ...topic, conversationId: 'second', title: '第二个会话' };
        api.topics.mockResolvedValue([topic, other]);
        render(<FinanceAssistantBoundary legacy={null}/>);
        const sidebar = await screen.findByRole('navigation', { name: '金融助手会话' });
        const first = await screen.findByRole('button', { name: /^群里的复盘/ });
        const second = screen.getByRole('button', { name: /^第二个会话/ });
        expect(sidebar).toContainElement(first);
        fireEvent.click(first);
        await waitFor(() => expect(first).toHaveAttribute('aria-current', 'page'));
        fireEvent.click(second);
        await waitFor(() => expect(api.messages).toHaveBeenCalledWith('second'));
        expect(sidebar).toContainElement(first);
        expect(second).toHaveAttribute('aria-current', 'page');
        expect(screen.getByLabelText('金融助手消息')).toBeVisible();
    });
    it('confirms shared deletion and keeps drafts for other conversations', async () => {
        const other = { ...topic, conversationId: 'second', title: '第二个会话' };
        let rows = [topic, other];
        api.topics.mockImplementation(async () => [...rows]);
        api.action.mockImplementation(async (op, body) => {
            if (op === 'delete') rows = rows.filter(row => row.conversationId !== body.conversationId);
        });
        render(<FinanceAssistantBoundary legacy={null}/>);
        fireEvent.change(await screen.findByLabelText('金融助手消息'), { target: { value: '新会话的草稿' } });
        fireEvent.click(await screen.findByRole('button', { name: /^群里的复盘/ }));
        fireEvent.change(screen.getByLabelText('金融助手消息'), { target: { value: '当前会话草稿' } });
        expect(screen.queryByText(/任务记录/)).toBeNull();
        const remove = await screen.findByLabelText('会话操作 第二个会话');
        fireEvent.click(remove);
        fireEvent.click(screen.getByRole('menuitem', {name: '删除会话'}));
        fireEvent.click(screen.getByRole('button', { name: '取消' }));
        expect(api.action).not.toHaveBeenCalledWith('delete', expect.anything());
        fireEvent.click(remove);
        fireEvent.click(screen.getByRole('menuitem', {name: '删除会话'}));
        fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
        await waitFor(() => expect(screen.queryByLabelText('会话操作 第二个会话')).toBeNull());
        expect(screen.getByLabelText('金融助手消息')).toHaveValue('当前会话草稿');
        fireEvent.click(screen.getByLabelText('会话操作 群里的复盘'));
        fireEvent.click(screen.getByRole('menuitem', {name: '删除会话'}));
        api.action.mockRejectedValueOnce(Error('delete failed'));
        fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
        expect(await screen.findByText('delete failed')).toBeVisible();
        expect(screen.getByLabelText('会话操作 群里的复盘')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
        expect(await screen.findByText('暂无历史会话')).toBeVisible();
        expect(screen.getByLabelText('金融助手消息')).toHaveValue('新会话的草稿');
    });
    it('preserves board identifiers and the displayed trade date', () => { expect(financeSource({ kind: 'board', boardId: 'BK1', boardName: '寒潮', tradeDate: '2026-11-09' })).toEqual({ kind: 'board', id: 'BK1', name: '寒潮', tradeDate: '2026-11-09' }); });
});

it('reads and edits queued messages without polling execution history',async()=>{
 vi.spyOn(document,'hasFocus').mockReturnValue(true);
 api.messages.mockResolvedValue({messages:[{id:'pending',role:'user',content:'pending question',seq:4,input_status:'pending'}],streaming:[]});
 api.tasks.mockResolvedValue([{id:'failed-task',objective:'write action',status:'failed',effects:[{id:'unknown-call',tool_name:'connector.call',status:'unknown'}]}]);
 render(<FinanceAssistantBoundary legacy={<div>legacy</div>}/>);fireEvent.click(await screen.findByRole('button',{name:/^群里的复盘/}));
 await waitFor(()=>expect(api.action).toHaveBeenCalledWith('read',{conversationId:'same-core-topic',seq:4}));
 fireEvent.click(await screen.findByRole('button',{name:'编辑排队消息'}));fireEvent.change(screen.getByLabelText('金融助手消息'),{target:{value:'corrected question'}});fireEvent.click(screen.getByRole('button',{name:'发送'}));
 await waitFor(()=>expect(api.action).toHaveBeenCalledWith('edit',expect.objectContaining({conversationId:'same-core-topic',messageId:'pending',text:'corrected question'})));
 expect(screen.queryByText(/任务记录/)).toBeNull();
 expect(api.tasks).not.toHaveBeenCalled();
 vi.restoreAllMocks();
});

it('polls only new messages and resets previously displayed history when privacy changes', async () => {
    vi.useFakeTimers();
    try {
        const sync = {bootId: 'one', privacyEpoch: 0};
        const record = (seq: number, content: string) => ({id: String(seq), seq, content, role: 'assistant'});
        api.messages.mockResolvedValueOnce({sync, messages: [record(10,'历史文本')], streaming: []})
            .mockResolvedValueOnce({sync, messages: [record(11,'新回复')], streaming: []})
            .mockResolvedValueOnce({sync: {...sync, privacyEpoch: 1}, messages: [], streaming: []})
            .mockResolvedValueOnce({sync: {...sync, privacyEpoch: 1}, messages: [record(11,'新回复')], streaming: []});
        render(<FinanceAssistantBoundary legacy={null}/>);
        await act(async () => {});
        await act(async () => { fireEvent.click(screen.getByRole('button', {name: /^群里的复盘/})); });
        await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
        expect(api.messages).toHaveBeenLastCalledWith(topic.conversationId, {after:10});
        expect(screen.getByText('历史文本')).toBeVisible();
        expect(screen.getByText('新回复')).toBeVisible();
        expect(api.models).toHaveBeenCalledTimes(1); expect(api.topics).toHaveBeenCalledTimes(1);
        await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
        expect(api.messages).toHaveBeenLastCalledWith(topic.conversationId);
        expect(screen.queryByText('历史文本')).toBeNull();
    } finally { vi.useRealTimers(); }
});

it('shows the outgoing bubble before acknowledgment and keeps the next draft when history catches up', async () => {
    let accept!: (result: {conversationId: string; messageId: string}) => void;
    api.action.mockImplementation((action) => action === 'input' ? new Promise(resolve => {accept = resolve;}) : Promise.resolve({}));
    render(<FinanceAssistantBoundary legacy={null}/>);
    fireEvent.click(await screen.findByRole('button', {name: /^群里的复盘/}));
    await screen.findByText('公开答案');
    const input = screen.getByLabelText('金融助手消息');
    fireEvent.change(input, {target: {value:'先发送这条'}});
    fireEvent.click(screen.getByRole('button', {name:'发送'}));
    expect(screen.getByText('先发送这条')).toBeVisible();
    expect(input).toHaveValue('');
    fireEvent.change(input, {target: {value:'下一条草稿'}});
    api.messages.mockResolvedValue({messages:[{id:'confirmed', seq:3, role:'user', content:'先发送这条'}], streaming:[]});
    await act(async () => accept({conversationId:topic.conversationId, messageId:'confirmed'}));
    expect(screen.getAllByText('先发送这条')).toHaveLength(1);
    expect(input).toHaveValue('下一条草稿');
});
