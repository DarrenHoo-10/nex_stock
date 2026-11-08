import type React from 'react';
import { useEffect, useId, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useLatestPlanRun } from '../../../hooks/useLatestPlanRun';
import { useAgentChatStore } from '../../../stores/agentChatStore';
import { assistantRequestOverrides, useAssistantPrefsStore } from '../../../stores/assistantPrefsStore';
import { ASSISTANT_NO_CONTEXT, usePlanUiStore } from '../../../stores/planUiStore';
import { GlassDrawer } from '../GlassDrawer';
import { PlanIcon } from '../PlanIcon';
import {
  buildStreamContext,
  contextKey,
  contextLabel,
  describeProgress,
  normalizeContext,
  suggestedQuestions,
} from './assistantContext';
import { useAssistantModels } from './useAssistantModels';
import '../../../styles/plan-assistant.css';

/** "2026-11-06" -> "11-06". */
function shortDate(tradeDate: string | null | undefined): string | null {
  return tradeDate && /^\d{4}-\d{2}-\d{2}/.test(tradeDate) ? tradeDate.slice(5, 10) : (tradeDate ?? null);
}

/**
 * Right-side assistant drawer (full screen at <=860px). Reuses the agent chat store and the
 * `/agent/chat/stream` API; every question carries `context.daily_plan` for the current board / stock.
 * Mounted once by PlanShell (lazily, on first open); open it with `usePlanUiStore().openAssistant(context)`.
 */
export const AssistantDrawer: React.FC = () => {
  const open = usePlanUiStore((state) => state.assistantOpen);
  const storedContext = usePlanUiStore((state) => state.assistantContext);
  const sessionKey = usePlanUiStore((state) => state.assistantSessionKey);
  const setAssistantOpen = usePlanUiStore((state) => state.setAssistantOpen);
  const setAssistantContext = usePlanUiStore((state) => state.setAssistantContext);
  const setSessionKey = usePlanUiStore((state) => state.setAssistantSessionKey);

  const messages = useAgentChatStore((state) => state.messages);
  const loading = useAgentChatStore((state) => state.loading);
  const stopping = useAgentChatStore((state) => state.stopping);
  const progressSteps = useAgentChatStore((state) => state.progressSteps);
  const chatError = useAgentChatStore((state) => state.chatError);
  const terminalStatus = useAgentChatStore((state) => state.terminalStatus);
  const completionBadge = useAgentChatStore((state) => state.completionBadge);
  const startStream = useAgentChatStore((state) => state.startStream);
  const stopStream = useAgentChatStore((state) => state.stopStream);
  const clearCompletionBadge = useAgentChatStore((state) => state.clearCompletionBadge);

  // Validates the saved model / effort choice shared with the assistant page.
  useAssistantModels();
  const latest = useLatestPlanRun();
  const latestTradeDate = latest.status === 'ready' ? (latest.run?.tradeDate ?? null) : null;

  const context = normalizeContext(storedContext);
  const key = contextKey(context);
  const questions = suggestedQuestions(context);

  const [input, setInput] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const messagesRef = useRef<HTMLDivElement>(null);
  const [lastQuestion, setLastQuestion] = useState('');
  const inputId = useId();

  // A different context starts a fresh conversation (a stream in progress is never cut off).
  useEffect(() => {
    if (!open || sessionKey === key || useAgentChatStore.getState().loading) {
      return;
    }
    useAgentChatStore.getState().startNewChat();
    setSessionKey(key);
  }, [open, key, sessionKey, setSessionKey]);

  // The old layout's completion badge is meaningless while the answer is read here.
  useEffect(() => {
    if (open && completionBadge) {
      clearCompletionBadge();
    }
  }, [open, completionBadge, clearCompletionBadge]);

  useEffect(() => {
    const element = messagesRef.current;
    if (element) {
      element.scrollTop = element.scrollHeight;
    }
  }, [messages.length, loading, progressSteps.length, chatError]);

  const close = () => setAssistantOpen(false);

  const removeContext = () => {
    // Same conversation, no object: keep the transcript and stop sending the object.
    setAssistantContext({ ...ASSISTANT_NO_CONTEXT, tradeDate: context.tradeDate ?? null });
    setSessionKey('none');
    inputRef.current?.focus();
  };

  const send = async (text: string) => {
    const message = text.trim();
    if (!message || useAgentChatStore.getState().loading) {
      return;
    }
    setLastQuestion(message);
    await startStream(
      {
        message,
        context: buildStreamContext(context, latestTradeDate),
        ...assistantRequestOverrides(useAssistantPrefsStore.getState()),
      },
      { skillNames: ['通用'], onAccepted: () => setInput('') },
    );
  };

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    void send(input);
  };

  const tradeLabel = shortDate(context.tradeDate ?? latestTradeDate);
  const showEmptyHint = messages.length === 0 && !loading && !chatError;

  return (
    <GlassDrawer
      open={open}
      onClose={close}
      title="助手"
      icon="assist"
      initialFocusRef={inputRef}
      footer={
        <form className="assist-compose" onSubmit={handleSubmit}>
          <label className="vh" htmlFor={inputId}>
            输入问题
          </label>
          <input
            id={inputId}
            ref={inputRef}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            autoComplete="off"
            placeholder="问问这只股票或这个板块"
          />
          {loading ? (
            <button type="button" className="btn" disabled={stopping} onClick={() => void stopStream()}>
              {stopping ? '正在停止' : '停止'}
            </button>
          ) : (
            <button type="submit" className="btn primary" disabled={!input.trim()}>
              发送
            </button>
          )}
        </form>
      }
    >
      <div className="ctx assist-ctx">
        <span>正在看</span>
        <span className="assist-chip" data-kind={context.kind} data-testid="assistant-context-chip">
          <b>{contextLabel(context)}</b>
          {context.kind !== 'none' ? (
            <button type="button" aria-label="取消选中对象" onClick={removeContext}>
              <PlanIcon name="close" size={12} />
            </button>
          ) : null}
        </span>
        {tradeLabel ? <span className="assist-date">{tradeLabel} 收盘计划</span> : null}
      </div>

      <div className="msgs assist-msgs" ref={messagesRef} aria-live="polite" aria-label="对话">
        {showEmptyHint ? <p className="sub assist-hint">可以直接点下面的问题，或者自己输入。</p> : null}
        {messages.map((message) =>
          message.role === 'user' ? (
            <div key={message.id} className="msg me">
              {message.content}
            </div>
          ) : (
            <div key={message.id} className="msg bot">
              <div className="assist-md">
                <Markdown remarkPlugins={[remarkGfm]}>{message.content}</Markdown>
              </div>
            </div>
          ),
        )}
        {loading ? (
          <div className="msg bot assist-pending" data-testid="assistant-pending">
            <span className="assist-spin" aria-hidden="true" />
            <span>{describeProgress(progressSteps)}</span>
          </div>
        ) : null}
        {!loading && terminalStatus ? (
          <p className="assist-note">{terminalStatus === 'cancelled' ? '已停止生成。' : '等待超时，请稍后重试。'}</p>
        ) : null}
        {chatError ? (
          <div className="assist-err" role="alert">
            <b>{chatError.title}</b>
            <span>{chatError.message}</span>
            {lastQuestion ? (
              <button type="button" className="btn sm" onClick={() => void send(lastQuestion)}>
                重试
              </button>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="quick" role="group" aria-label="常用问题">
        {questions.map((question) => (
          <button key={question} type="button" disabled={loading} onClick={() => void send(question)}>
            {question}
          </button>
        ))}
      </div>
      <p className="bound">
        回答只用{tradeLabel ? ` ${tradeLabel} ` : ''}收盘后的同一份数据，不会改动计划，也不会下单。
      </p>
    </GlassDrawer>
  );
};
