import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../../contexts/UiLanguageContext';
import { useAgentChatStore } from '../../../../stores/agentChatStore';
import { ASSISTANT_NO_CONTEXT, usePlanUiStore } from '../../../../stores/planUiStore';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../../utils/uiLanguage';
import { AssistantDrawer } from '../AssistantDrawer';

const { getLatestRun } = vi.hoisted(() => ({ getLatestRun: vi.fn() }));
vi.mock('../../../../api/dailyPlan', () => ({ getLatestRun, isPlanServiceUnavailable: () => false }));

const startStream = vi.fn(async () => undefined);
const startNewChat = vi.fn();

function renderDrawer() {
  return render(
    <UiLanguageProvider>
      <MemoryRouter>
        <button type="button">trigger</button>
        <AssistantDrawer />
      </MemoryRouter>
    </UiLanguageProvider>,
  );
}

function sentPayload(index: number): { message: string; context: Record<string, unknown> } {
  return (startStream.mock.calls[index] as unknown as [{ message: string; context: Record<string, unknown> }])[0];
}

beforeEach(() => {
  localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
  getLatestRun.mockReset();
  getLatestRun.mockResolvedValue({ tradeDate: '2026-11-06' });
  startStream.mockClear();
  startNewChat.mockClear();
  useAgentChatStore.setState({
    messages: [],
    loading: false,
    chatError: null,
    progressSteps: [],
    startStream,
    startNewChat,
  } as never);
  usePlanUiStore.setState({
    assistantOpen: false,
    assistantContext: ASSISTANT_NO_CONTEXT,
    assistantSessionKey: null,
  });
});

describe('AssistantDrawer', () => {
  it('opens with no context, closes on Escape and returns focus to the trigger', async () => {
    renderDrawer();
    const trigger = screen.getByRole('button', { name: 'trigger' });
    trigger.focus();
    act(() => usePlanUiStore.getState().setAssistantOpen(true));

    const dialog = await screen.findByRole('dialog', { name: '助手' });
    expect(screen.getByTestId('assistant-context-chip')).toHaveTextContent('未选中');
    expect(screen.queryByRole('button', { name: '取消选中对象' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: '在问股页打开' })).toHaveAttribute('href', '/chat');
    expect(dialog.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(usePlanUiStore.getState().assistantOpen).toBe(false);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(document.activeElement).toBe(trigger);
  });

  it('sends the question with context.daily_plan and starts a new chat for a new context', async () => {
    renderDrawer();
    act(() =>
      usePlanUiStore.getState().openAssistant({
        kind: 'stock',
        stockCode: '000988',
        stockName: '华工科技',
        horizon: 'short',
        held: true,
      }),
    );
    await screen.findByRole('dialog', { name: '助手' });
    expect(screen.getByTestId('assistant-context-chip')).toHaveTextContent('当前：华工科技 · 短线');
    await waitFor(() => expect(startNewChat).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: '现在加仓风险大吗' }));
    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    expect(sentPayload(0).message).toBe('现在加仓风险大吗');
    expect(sentPayload(0).context).toEqual({
      daily_plan: {
        trade_date: '2026-11-06',
        kind: 'stock',
        board_id: null,
        stock_code: '000988',
        horizon: 'short',
      },
      stock_code: '000988',
      stock_name: '华工科技',
    });
  });

  it('removes the context with the chip button without starting a new chat', async () => {
    renderDrawer();
    act(() =>
      usePlanUiStore.getState().openAssistant({ kind: 'board', boardId: 'industry:BK1', boardName: '半导体' }),
    );
    await screen.findByRole('dialog', { name: '助手' });
    expect(screen.getByTestId('assistant-context-chip')).toHaveTextContent('当前：板块 半导体');
    await waitFor(() => expect(startNewChat).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: '取消选中对象' }));
    expect(screen.getByTestId('assistant-context-chip')).toHaveTextContent('未选中');
    expect(usePlanUiStore.getState().assistantContext.kind).toBe('none');
    expect(startNewChat).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '今天哪个板块最值得关注' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    expect((sentPayload(0).context.daily_plan as { kind: string }).kind).toBe('none');
  });

  it('shows the error state with a retry that re-sends the last question', async () => {
    renderDrawer();
    act(() => usePlanUiStore.getState().setAssistantOpen(true));
    await screen.findByRole('dialog', { name: '助手' });

    fireEvent.click(screen.getByRole('button', { name: '我的持仓明天怎么做' }));
    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    act(() =>
      useAgentChatStore.setState({
        chatError: {
          title: '回复未完整返回',
          message: 'Agent 流式响应在完成前中断，请重试。',
          rawMessage: '',
          category: 'upstream_network',
        },
      } as never),
    );
    expect(screen.getByRole('alert')).toHaveTextContent('Agent 流式响应在完成前中断');
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(2));
    expect(sentPayload(1).message).toBe('我的持仓明天怎么做');
  });

  it('renders assistant answers as markdown', async () => {
    useAgentChatStore.setState({
      messages: [
        { id: '1', role: 'user', content: '为什么不建议买' },
        { id: '2', role: 'assistant', content: '**不追高**，等回踩。' },
      ],
    } as never);
    renderDrawer();
    act(() => usePlanUiStore.getState().setAssistantOpen(true));
    const strong = await screen.findByText('不追高');
    expect(strong.tagName).toBe('STRONG');
  });
});

describe('planUiStore assistant context', () => {
  it('openAssistant without context means none and the header toggle resets a previous context', () => {
    usePlanUiStore.getState().openAssistant({ kind: 'board', boardId: 'b1' });
    expect(usePlanUiStore.getState().assistantContext.kind).toBe('board');
    usePlanUiStore.getState().setAssistantOpen(false);
    usePlanUiStore.getState().setAssistantOpen(true);
    expect(usePlanUiStore.getState().assistantContext.kind).toBe('none');
    usePlanUiStore.getState().openAssistant();
    expect(usePlanUiStore.getState().assistantContext).toEqual(ASSISTANT_NO_CONTEXT);
  });
});
