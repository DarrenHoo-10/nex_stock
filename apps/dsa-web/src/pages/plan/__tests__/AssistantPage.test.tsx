import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { resetAssistantModelsCache } from '../../../components/plan/assistant/useAssistantModels';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { useAgentChatStore } from '../../../stores/agentChatStore';
import { useAssistantPrefsStore } from '../../../stores/assistantPrefsStore';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../utils/uiLanguage';
import { formatTokens, relativeTime } from '../../../components/plan/assistant/assistantFormat';
import { LegacyAssistantPage as AssistantPage } from '../AssistantPage';

const api = vi.hoisted(() => ({
  getModels: vi.fn(),
  getChatContext: vi.fn(),
  compactChatSession: vi.fn(),
  deleteChatSession: vi.fn(),
}));
vi.mock('../../../api/agent', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../api/agent')>();
  return { ...actual, agentApi: { ...actual.agentApi, ...api } };
});
const { getLatestRun } = vi.hoisted(() => ({ getLatestRun: vi.fn() }));
vi.mock('../../../api/dailyPlan', () => ({ getLatestRun, isPlanServiceUnavailable: () => false }));

const startStream = vi.fn(async () => undefined);
const startNewChat = vi.fn();
const switchSession = vi.fn(async () => undefined);
const loadSessions = vi.fn(async () => undefined);
const loadInitialSession = vi.fn(async () => undefined);

const CONTEXT = {
  session_id: 's1',
  model: 'primary-alias',
  context_window: 128000,
  message_count: 8,
  history_tokens: 3200,
  history_message_count: 8,
  compression_enabled: false,
  trigger_tokens: 12000,
  protected_turns: 4,
  compactable_message_count: 2,
  summary: null,
};

function renderPage() {
  return render(
    <UiLanguageProvider>
      <MemoryRouter>
        <AssistantPage />
      </MemoryRouter>
    </UiLanguageProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
  resetAssistantModelsCache();
  Object.values(api).forEach((fn) => fn.mockReset());
  [startStream, startNewChat, switchSession, loadSessions, loadInitialSession].forEach((fn) => fn.mockClear());
  getLatestRun.mockResolvedValue({ tradeDate: '2026-11-06' });
  api.getModels.mockResolvedValue({
    models: [
      { deployment_id: 'a', model: 'openai/p', provider: 'openai', source: 'llm_channels', api_base: null, deployment_name: 'primary-alias', route_model: 'primary-alias', is_primary: true, is_fallback: false },
      { deployment_id: 'b', model: 'openai/f', provider: 'openai', source: 'llm_channels', api_base: null, deployment_name: 'fallback-alias', route_model: 'fallback-alias', is_primary: false, is_fallback: true },
    ],
    default_model: 'primary-alias',
    default_reasoning_effort: 'high',
    reasoning_effort_options: ['low', 'medium', 'high', 'xhigh', 'max'],
  });
  api.getChatContext.mockResolvedValue(CONTEXT);
  useAssistantPrefsStore.setState({ model: '', reasoningEffort: '' });
  useAgentChatStore.setState({
    messages: [
      { id: 'm1', role: 'user', content: '茅台怎么看' },
      { id: 'm2', role: 'assistant', content: '**观望**' },
    ],
    loading: false,
    stopping: false,
    chatError: null,
    progressSteps: [],
    terminalStatus: null,
    completionBadge: false,
    sessionId: 's1',
    sessions: [
      { session_id: 's1', title: '茅台怎么看', message_count: 8, created_at: null, last_active: null },
      { session_id: 's2', title: '半导体还能追吗', message_count: 4, created_at: null, last_active: null },
    ],
    sessionsLoading: false,
    startStream,
    startNewChat,
    switchSession,
    loadSessions,
    loadInitialSession,
  } as never);
});

describe('AssistantPage', () => {
  it('lists history sessions and switches to another one', async () => {
    renderPage();
    const list = screen.getByTestId('assistant-session-list');
    expect(within(list).getByRole('button', { name: /^茅台怎么看/ })).toHaveAttribute('aria-current', 'true');

    fireEvent.click(within(list).getByRole('button', { name: /^半导体还能追吗/ }));
    expect(switchSession).toHaveBeenCalledWith('s2');
    await waitFor(() => expect(loadSessions).toHaveBeenCalled());
  });

  it('sends the chosen model and reasoning effort with the question', async () => {
    renderPage();
    const modelSelect = await screen.findByLabelText('模型');
    await waitFor(() => expect(modelSelect).not.toBeDisabled());
    fireEvent.change(modelSelect, { target: { value: 'fallback-alias' } });
    fireEvent.change(screen.getByLabelText('思考强度'), { target: { value: 'low' } });

    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '明天怎么做' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(startStream).toHaveBeenCalled());
    expect((startStream.mock.calls[0] as unknown[])[0]).toEqual({
      message: '明天怎么做',
      model: 'fallback-alias',
      reasoning_effort: 'low',
    });
    expect(JSON.parse(localStorage.getItem('dsa_assistant_prefs') ?? '{}')).toEqual({
      model: 'fallback-alias',
      reasoningEffort: 'low',
    });
  });

  it('omits overrides by default and drops a saved model the server no longer offers', async () => {
    useAssistantPrefsStore.setState({ model: 'removed-model', reasoningEffort: '' });
    renderPage();
    await waitFor(() => expect(useAssistantPrefsStore.getState().model).toBe(''));

    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: 'hi' } });
    fireEvent.keyDown(screen.getByLabelText('输入问题'), { key: 'Enter' });

    await waitFor(() => expect(startStream).toHaveBeenCalled());
    expect((startStream.mock.calls[0] as unknown[])[0]).toEqual({ message: 'hi' });
  });

  it('shows context usage and compacts the session on demand', async () => {
    api.compactChatSession.mockResolvedValue({
      status: 'compacted',
      summarized_message_count: 4,
      tokens_before: 3200,
      tokens_after: 900,
      context: { ...CONTEXT, history_tokens: 900, compactable_message_count: 0, summary: { covered_message_count: 4, estimated_tokens: 300, updated_at: null } },
    });
    renderPage();
    const toggle = await screen.findByRole('button', { name: /上下文 3\.2k/ });
    fireEvent.click(toggle);

    const panel = screen.getByTestId('assistant-context-panel');
    expect(within(panel).getByRole('meter', { name: '上下文用量' })).toHaveAttribute('aria-valuenow', '3200');
    expect(panel).toHaveTextContent('128k');

    fireEvent.click(within(panel).getByRole('button', { name: '压缩会话' }));
    await waitFor(() => expect(api.compactChatSession).toHaveBeenCalledWith('s1', undefined));
    expect(await within(panel).findByRole('status')).toHaveTextContent('已压缩 4 条消息');
    expect(panel).toHaveTextContent('已压缩前 4 条');
    expect(within(panel).getByRole('button', { name: '压缩会话' })).toBeDisabled();
  });

  it('explains a failed compaction without changing the session', async () => {
    api.compactChatSession.mockRejectedValue(
      Object.assign(new Error('Request failed with status code 502'), {
        isAxiosError: true,
        response: { status: 502, data: { detail: { error: 'compaction_failed', message: 'x' } } },
      }),
    );
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /上下文 3\.2k/ }));
    const panel = screen.getByTestId('assistant-context-panel');
    fireEvent.click(within(panel).getByRole('button', { name: '压缩会话' }));

    expect(await within(panel).findByRole('status')).toHaveTextContent('摘要模型没有返回结果');
    expect(within(panel).getByRole('button', { name: '压缩会话' })).not.toBeDisabled();
  });

  it('asks before deleting a session and starts a new chat when the current one is deleted', async () => {
    api.deleteChatSession.mockResolvedValue(undefined);
    renderPage();

    fireEvent.click(screen.getByRole('button', { name: '删除会话 茅台怎么看' }));
    expect(api.deleteChatSession).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '删除' }));

    await waitFor(() => expect(api.deleteChatSession).toHaveBeenCalledWith('s1'));
    expect(startNewChat).toHaveBeenCalled();
  });
});

describe('formatters', () => {
  it('formats token counts and relative times', () => {
    expect(formatTokens(950)).toBe('950');
    expect(formatTokens(3200)).toBe('3.2k');
    expect(formatTokens(128000)).toBe('128k');
    expect(formatTokens(1_000_000)).toBe('1.0M');
    const now = new Date('2026-11-08T12:00:00').getTime();
    expect(relativeTime('2026-11-08T11:58:00', now)).toBe('2 分钟前');
    expect(relativeTime('2026-11-06T09:00:00', now)).toBe('11-06');
    expect(relativeTime(null, now)).toBe('');
  });
});
