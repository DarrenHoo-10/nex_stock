import { FinanceAssistantBoundary } from '../../components/plan/assistant/FinanceAssistant';
import type React from 'react';
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { agentApi } from '../../api/agent';
import type { ChatContextInfo } from '../../api/agent';
import { getParsedApiError } from '../../api/error';
import { PlanIcon } from '../../components/plan/PlanIcon';
import { ModelKeyBanner } from '../../components/auth/ModelKeyBanner';
import { PlanPageHeader } from '../../components/plan/PlanPageHeader';
import { describeProgress } from '../../components/plan/assistant/assistantContext';
import { formatTokens, relativeTime } from '../../components/plan/assistant/assistantFormat';
import { useAssistantModels } from '../../components/plan/assistant/useAssistantModels';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { useAgentChatStore } from '../../stores/agentChatStore';
import { assistantRequestOverrides, useAssistantPrefsStore } from '../../stores/assistantPrefsStore';
import '../../styles/plan-assistant.css';

const EFFORT_LABEL: Record<string, string> = {
  none: '不思考',
  low: '低',
  medium: '中',
  high: '高',
  xhigh: '很高',
  max: '最高',
};

const SUGGESTIONS = ['今天哪个板块最值得关注', '我的持仓明天怎么做', '帮我看看贵州茅台现在能不能买'];

interface ContextPanelProps {
  info: ChatContextInfo | null;
  loading: boolean;
  compacting: boolean;
  busy: boolean;
  notice: string | null;
  onCompact: () => void;
}

const ContextPanel: React.FC<ContextPanelProps> = ({ info, loading, compacting, busy, notice, onCompact }) => {
  const used = info?.history_tokens ?? 0;
  const limit = info?.context_window ?? null;
  const ratio = limit ? Math.min(1, used / limit) : 0;
  const canCompact = Boolean(info && info.compactable_message_count > 0) && !busy && !compacting;

  return (
    <div className="asst-ctx-panel" data-testid="assistant-context-panel">
      <div className="asst-ctx-line">
        <span>上下文</span>
        <b className="num">
          {formatTokens(used)}
          {limit ? ` / ${formatTokens(limit)}` : ''}
        </b>
        {limit ? <span className="num asst-ctx-pct">{(ratio * 100).toFixed(ratio < 0.1 ? 1 : 0)}%</span> : null}
      </div>
      <div
        className="asst-meter"
        role="meter"
        aria-label="上下文用量"
        aria-valuemin={0}
        aria-valuemax={limit ?? 0}
        aria-valuenow={used}
      >
        <i style={{ width: `${Math.max(ratio * 100, used > 0 ? 1.5 : 0)}%` }} />
      </div>
      <dl className="asst-ctx-facts">
        <div>
          <dt>模型</dt>
          <dd>{info?.model ?? '—'}</dd>
        </div>
        <div>
          <dt>会话消息</dt>
          <dd className="num">{info ? `${info.message_count} 条，本轮带上 ${info.history_message_count} 条` : '—'}</dd>
        </div>
        <div>
          <dt>摘要</dt>
          <dd>
            {info?.summary
              ? `已压缩前 ${info.summary.covered_message_count} 条（约 ${formatTokens(info.summary.estimated_tokens)} tokens）`
              : '暂无'}
          </dd>
        </div>
        <div>
          <dt>自动压缩</dt>
          <dd className="num">
            {info
              ? info.compression_enabled
                ? `开，超过 ${formatTokens(info.trigger_tokens)} tokens 时`
                : '关'
              : '—'}
          </dd>
        </div>
      </dl>
      <p className="sub">
        只统计对话历史；系统提示、工具结果和本轮问题不在内。压缩会把较早的对话合成摘要，最近 {info?.protected_turns ?? 0}{' '}
        轮保持原文。
      </p>
      <div className="asst-ctx-acts">
        <button type="button" className="btn sm" disabled={!canCompact} onClick={onCompact}>
          {compacting ? '正在压缩…' : '压缩会话'}
        </button>
        {loading ? <span className="assist-spin" aria-label="正在读取" /> : null}
        {notice ? (
          <span className="asst-ctx-notice" role="status">
            {notice}
          </span>
        ) : null}
      </div>
    </div>
  );
};

/**
 * /assistant: full-page personal assistant. History sessions on the left; chat with model / reasoning-effort
 * pickers, context usage and manual compaction on the right. Shares the agent chat store with the drawer.
 */
export const LegacyAssistantPage: React.FC = () => {
  const { t } = useUiLanguage();
  const title = t('layout.nav.assistant');

  const messages = useAgentChatStore((state) => state.messages);
  const loading = useAgentChatStore((state) => state.loading);
  const stopping = useAgentChatStore((state) => state.stopping);
  const progressSteps = useAgentChatStore((state) => state.progressSteps);
  const chatError = useAgentChatStore((state) => state.chatError);
  const terminalStatus = useAgentChatStore((state) => state.terminalStatus);
  const sessionId = useAgentChatStore((state) => state.sessionId);
  const sessions = useAgentChatStore((state) => state.sessions);
  const sessionsLoading = useAgentChatStore((state) => state.sessionsLoading);
  const completionBadge = useAgentChatStore((state) => state.completionBadge);

  const model = useAssistantPrefsStore((state) => state.model);
  const reasoningEffort = useAssistantPrefsStore((state) => state.reasoningEffort);
  const setModel = useAssistantPrefsStore((state) => state.setModel);
  const setReasoningEffort = useAssistantPrefsStore((state) => state.setReasoningEffort);
  const models = useAssistantModels();

  const [input, setInput] = useState('');
  const [lastQuestion, setLastQuestion] = useState('');
  const [contextInfo, setContextInfo] = useState<ChatContextInfo | null>(null);
  const [contextLoading, setContextLoading] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [compacting, setCompacting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [sideNotice, setSideNotice] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const messagesRef = useRef<HTMLDivElement>(null);
  const inputId = useId();
  const modelId = useId();
  const effortId = useId();

  useEffect(() => {
    document.title = `${title} - DSA`;
  }, [title]);

  useEffect(() => {
    const store = useAgentChatStore.getState();
    void store.loadInitialSession().then(() => store.loadSessions());
  }, []);

  useEffect(() => {
    if (completionBadge) {
      useAgentChatStore.getState().clearCompletionBadge();
    }
  }, [completionBadge]);

  useEffect(() => {
    const element = messagesRef.current;
    if (element) {
      element.scrollTop = element.scrollHeight;
    }
  }, [messages.length, loading, progressSteps.length, chatError]);

  const hasMessages = messages.length > 0;
  const refreshContext = useCallback(async () => {
    if (!hasMessages) {
      setContextInfo(null);
      return;
    }
    setContextLoading(true);
    try {
      setContextInfo(await agentApi.getChatContext(sessionId, model || undefined));
    } catch {
      setContextInfo(null);
    } finally {
      setContextLoading(false);
    }
  }, [sessionId, model, hasMessages]);

  // After a session switch, a finished answer, or a model change.
  useEffect(() => {
    if (!loading) {
      void refreshContext();
    }
  }, [refreshContext, loading, messages.length]);

  useEffect(() => {
    setNotice(null);
  }, [sessionId]);

  const send = async (text: string) => {
    const message = text.trim();
    if (!message || useAgentChatStore.getState().loading) {
      return;
    }
    setLastQuestion(message);
    setNotice(null);
    await useAgentChatStore.getState().startStream(
      { message, ...assistantRequestOverrides(useAssistantPrefsStore.getState()) },
      { skillNames: ['通用'], onAccepted: () => setInput('') },
    );
  };

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    void send(input);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send(input);
    }
  };

  const newChat = () => {
    if (loading) {
      return;
    }
    useAgentChatStore.getState().startNewChat();
    setContextInfo(null);
    inputRef.current?.focus();
  };

  const openSession = (id: string) => {
    if (loading || id === sessionId) {
      return;
    }
    setPendingDelete(null);
    void useAgentChatStore.getState().switchSession(id);
  };

  const deleteSession = async (id: string) => {
    setPendingDelete(null);
    setSideNotice(null);
    try {
      await agentApi.deleteChatSession(id);
    } catch (error) {
      setSideNotice(getParsedApiError(error).message);
      return;
    }
    if (id === useAgentChatStore.getState().sessionId) {
      useAgentChatStore.getState().startNewChat();
      setContextInfo(null);
    }
    await useAgentChatStore.getState().loadSessions();
  };

  const compact = async () => {
    setCompacting(true);
    setNotice(null);
    try {
      const result = await agentApi.compactChatSession(sessionId, model || undefined);
      setContextInfo(result.context);
      setNotice(
        result.status === 'compacted'
          ? `已压缩 ${result.summarized_message_count} 条消息：${formatTokens(result.tokens_before)} → ${formatTokens(result.tokens_after)} tokens`
          : '没有可压缩的较早消息',
      );
    } catch (error) {
      const parsed = getParsedApiError(error);
      if (parsed.status === 502) {
        setNotice('摘要模型没有返回结果（可能超时），会话没有改动，请稍后再试。');
      } else if (parsed.status === 409) {
        setNotice('会话刚被其他操作改动，请刷新后再试。');
      } else {
        setNotice(parsed.message);
      }
    } finally {
      setCompacting(false);
    }
  };

  const modelsReady = models.status === 'ready' && models.routeModels.length > 0;
  const defaultModel = models.status === 'ready' ? models.data.default_model : null;
  const defaultEffort = models.status === 'ready' ? models.data.default_reasoning_effort : '';
  const effortOptions = models.status === 'ready' ? models.data.reasoning_effort_options : [];
  const showSuggestions = !hasMessages && !loading && !chatError;

  return (
    <>
      <PlanPageHeader title={title} subtitle="和助手聊股票、板块和持仓，会话会保存在历史里" hideAssistant />
      <ModelKeyBanner variant="plan" />
      <div className="grid2 asst-layout">
        <aside className="win side asst-side" aria-label="历史会话">
          <div className="asst-side-head">
            <h2>历史会话</h2>
            <button type="button" className="btn sm" onClick={newChat} disabled={loading}>
              <PlanIcon name="plus" size={14} />
              新对话
            </button>
          </div>
          <div className="list-scroll">
            {sessions.length === 0 ? (
              <p className="sub asst-empty">{sessionsLoading ? '正在读取…' : '还没有历史会话'}</p>
            ) : (
              <ul className="rows" data-testid="assistant-session-list">
                {sessions.map((item) => {
                  const current = item.session_id === sessionId;
                  const confirming = pendingDelete === item.session_id;
                  return (
                    <li key={item.session_id} className="asst-row-wrap">
                      <button
                        type="button"
                        className="row asst-row"
                        aria-current={current ? 'true' : undefined}
                        disabled={loading && !current}
                        onClick={() => openSession(item.session_id)}
                      >
                        <span className="asst-row-title">{item.title || '新对话'}</span>
                        <span className="asst-row-meta num">
                          {relativeTime(item.last_active)} · {item.message_count} 条
                        </span>
                      </button>
                      {confirming ? (
                        <span className="asst-row-confirm">
                          <button type="button" className="btn sm" onClick={() => void deleteSession(item.session_id)}>
                            删除
                          </button>
                          <button type="button" className="btn sm quiet" onClick={() => setPendingDelete(null)}>
                            取消
                          </button>
                        </span>
                      ) : (
                        <button
                          type="button"
                          className="asst-row-del"
                          aria-label={`删除会话 ${item.title || ''}`}
                          disabled={loading && current}
                          onClick={() => setPendingDelete(item.session_id)}
                        >
                          <PlanIcon name="trash" size={15} />
                        </button>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
          {sideNotice ? (
            <p className="asst-side-notice" role="alert">
              {sideNotice}
            </p>
          ) : null}
        </aside>

        <section className="win asst-main" aria-label="对话">
          <div className="asst-toolbar">
            <label className="asst-field" htmlFor={modelId}>
              <span>模型</span>
              <select
                id={modelId}
                value={model}
                disabled={!modelsReady}
                onChange={(event) => setModel(event.target.value)}
              >
                <option value="">默认{defaultModel ? `（${defaultModel}）` : ''}</option>
                {models.status === 'ready'
                  ? models.routeModels.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))
                  : null}
              </select>
            </label>
            <label className="asst-field" htmlFor={effortId}>
              <span>思考强度</span>
              <select
                id={effortId}
                value={reasoningEffort}
                disabled={!modelsReady}
                onChange={(event) => setReasoningEffort(event.target.value)}
              >
                <option value="">默认{defaultEffort ? `（${EFFORT_LABEL[defaultEffort] ?? defaultEffort}）` : '（模型自定）'}</option>
                <option value="none">{EFFORT_LABEL.none}</option>
                {effortOptions.map((value) => (
                  <option key={value} value={value}>
                    {EFFORT_LABEL[value] ?? value}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="cap asst-ctx-toggle"
              aria-expanded={contextOpen}
              aria-controls="assistant-context"
              onClick={() => setContextOpen((open) => !open)}
            >
              上下文 <b className="num">{formatTokens(contextInfo?.history_tokens ?? 0)}</b>
              {contextInfo?.context_window ? <span className="num">/ {formatTokens(contextInfo.context_window)}</span> : null}
            </button>
          </div>
          {models.status === 'error' ? <p className="assist-note asst-pad">模型列表读取失败，将使用默认模型。</p> : null}
          {models.status === 'ready' && models.routeModels.length === 0 ? (
            <p className="assist-note asst-pad">当前后端不支持切换模型和思考强度，使用服务端配置。</p>
          ) : null}
          {contextOpen ? (
            <div id="assistant-context">
              <ContextPanel
                info={contextInfo}
                loading={contextLoading}
                compacting={compacting}
                busy={loading || !hasMessages}
                notice={notice}
                onCompact={() => void compact()}
              />
            </div>
          ) : null}

          <div className="msgs assist-msgs asst-msgs" ref={messagesRef} aria-live="polite">
            {showSuggestions ? (
              <div className="asst-welcome">
                <p className="sub">可以直接问，或者点一个问题开始。</p>
                <div className="quick" role="group" aria-label="常用问题">
                  {SUGGESTIONS.map((question) => (
                    <button key={question} type="button" onClick={() => void send(question)}>
                      {question}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}
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
              <div className="msg bot assist-pending" data-testid="assistant-page-pending">
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

          <form className="asst-compose" onSubmit={handleSubmit}>
            <label className="vh" htmlFor={inputId}>
              输入问题
            </label>
            <textarea
              id={inputId}
              ref={inputRef}
              rows={2}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="问点什么，Enter 发送，Shift+Enter 换行"
            />
            {loading ? (
              <button
                type="button"
                className="btn"
                disabled={stopping}
                onClick={() => void useAgentChatStore.getState().stopStream()}
              >
                {stopping ? '正在停止' : '停止'}
              </button>
            ) : (
              <button type="submit" className="btn primary" disabled={!input.trim()}>
                发送
              </button>
            )}
          </form>
        </section>
      </div>
    </>
  );
};

const AssistantPage: React.FC = () => <FinanceAssistantBoundary legacy={<LegacyAssistantPage/>}/>;
export default AssistantPage;
