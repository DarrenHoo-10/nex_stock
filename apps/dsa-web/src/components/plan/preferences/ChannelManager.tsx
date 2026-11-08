import type React from 'react';
import { useEffect, useId, useMemo, useState } from 'react';
import { systemConfigApi } from '../../../api/systemConfig';
import { getParsedApiError } from '../../../api/error';
import { showPlanToast } from '../../../stores/planToastStore';
import {
  CHANNEL_PROTOCOLS,
  DISCOVERABLE_PROTOCOLS,
  channelDeleteItems,
  channelSaveItems,
  draftChanged,
  draftFromChannel,
  emptyDraft,
  inUseProblem,
  mergeModelLists,
  normalizeChannelName,
  validateDraft,
} from './channelDraft';
import type { ChannelDraft } from './channelDraft';
import type { ModelChannel, ModelValues } from './llmModels';
import { SaveProblem } from './shared';
import type { SaveOutcome } from './usePreferencesConfig';

const NEW = '__new__';

type ChannelTest =
  | { status: 'testing' }
  | { status: 'ok'; latencyMs: number | null }
  | { status: 'down'; message: string };

type Discovery = { status: 'loading' } | { status: 'ok'; count: number } | { status: 'error'; message: string };

interface ChannelManagerProps {
  channels: ModelChannel[];
  /** Saved role values, to keep models that a role uses from being switched off or deleted. */
  roles: ModelValues;
  providers: ReadonlySet<string>;
  /** Channel that serves the saved 分析模型 (shown as 默认). */
  primaryChannel: string | undefined;
  onSave: (items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>;
  onReload: () => void;
}

function testMessage(result: { message?: string | null; error?: string | null }): string {
  return result.error || result.message || '连接失败';
}

/**
 * Provider-style channel editor: channel list on the left; name, address, API format, key, on/off and the model list
 * (fetch, add, search, switch each model on or off) on the right. Each channel saves on its own.
 */
export const ChannelManager: React.FC<ChannelManagerProps> = ({
  channels,
  roles,
  providers,
  primaryChannel,
  onSave,
  onReload,
}) => {
  const idPrefix = useId();
  const names = useMemo(() => channels.map((channel) => channel.name), [channels]);
  const [selected, setSelected] = useState<string>(() => names[0] ?? NEW);
  const [drafts, setDrafts] = useState<Record<string, ChannelDraft>>({});
  const [fetched, setFetched] = useState<Record<string, string[]>>({});
  const [discovery, setDiscovery] = useState<Record<string, Discovery>>({});
  const [tests, setTests] = useState<Record<string, ChannelTest>>({});
  const [showKey, setShowKey] = useState(false);
  const [search, setSearch] = useState('');
  const [adding, setAdding] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [problem, setProblem] = useState<Exclude<SaveOutcome, { ok: true }> | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  // A channel that disappeared (deleted here or elsewhere) falls back to the first one.
  const current = selected === NEW || names.includes(selected) ? selected : (names[0] ?? NEW);
  const isNew = current === NEW;
  const savedChannel = isNew ? undefined : channels.find((channel) => channel.name === current);
  const saved: ChannelDraft = savedChannel ? draftFromChannel(savedChannel) : emptyDraft();
  const draft: ChannelDraft = drafts[current] ?? saved;
  const keySaved = savedChannel?.keyConfigured ?? false;
  const dirty = isNew ? draftChanged(emptyDraft(), draft) : draftChanged(saved, draft);
  const requestName = isNew ? normalizeChannelName(draft.name) || 'new_channel' : current;
  const discoverable = DISCOVERABLE_PROTOCOLS.has(draft.protocol);
  const fetchedModels = fetched[current] ?? [];
  const allModels = mergeModelLists(mergeModelLists(saved.models, draft.models), fetchedModels);
  const query = search.trim().toLowerCase();
  const visibleModels = query ? allModels.filter((model) => model.toLowerCase().includes(query)) : allModels;
  const enabledSet = new Set(draft.models);
  const found = discovery[current];
  const test = tests[current];
  const protocolInfo = CHANNEL_PROTOCOLS.find((item) => item.value === draft.protocol);

  const select = (name: string) => {
    setSelected(name);
    setSearch('');
    setAdding(null);
    setShowKey(false);
    setLocalError(null);
    setProblem(null);
    setConfirmDelete(false);
  };

  const patch = (next: Partial<ChannelDraft>) => {
    setDrafts((all) => ({ ...all, [current]: { ...draft, ...next } }));
    setLocalError(null);
    setProblem(null);
    setConfirmDelete(false);
  };

  const discover = async (target: string, request: ChannelDraft, useSaved: boolean) => {
    setDiscovery((all) => ({ ...all, [target]: { status: 'loading' } }));
    try {
      const result = await systemConfigApi.discoverLLMChannelModels({
        name: target === NEW ? normalizeChannelName(request.name) || 'new_channel' : target,
        protocol: request.protocol,
        baseUrl: request.baseUrl.trim(),
        apiKey: request.apiKey.trim(),
        models: request.models,
        useSavedSecret: useSaved,
      });
      if (result.success) {
        setFetched((all) => ({ ...all, [target]: result.models }));
        setDiscovery((all) => ({ ...all, [target]: { status: 'ok', count: result.models.length } }));
      } else {
        setDiscovery((all) => ({ ...all, [target]: { status: 'error', message: testMessage(result) } }));
      }
    } catch (error) {
      setDiscovery((all) => ({ ...all, [target]: { status: 'error', message: getParsedApiError(error).message } }));
    }
  };

  // Opening a saved channel fetches its model list once, so switched-off models are listed too.
  useEffect(() => {
    if (!savedChannel || !savedChannel.keyConfigured || !DISCOVERABLE_PROTOCOLS.has(savedChannel.protocol)) {
      return;
    }
    if (discovery[savedChannel.name]) {
      return;
    }
    void discover(savedChannel.name, draftFromChannel(savedChannel), true);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once per opened channel
  }, [savedChannel?.name]);

  // Switched-on models keep the list order (saved models first), so toggling back restores the saved value.
  const withModels = (on: ReadonlySet<string>) => allModels.filter((model) => on.has(model));

  const toggleModel = (model: string, on: boolean) => {
    const next = new Set(enabledSet);
    if (on) next.add(model);
    else next.delete(model);
    patch({ models: withModels(next) });
  };

  const setVisible = (on: boolean) => {
    const next = new Set(enabledSet);
    for (const model of visibleModels) {
      if (on) next.add(model);
      else next.delete(model);
    }
    patch({ models: withModels(next) });
  };

  const addModel = () => {
    const model = (adding ?? '').trim();
    if (!model) {
      setAdding(null);
      return;
    }
    if (!enabledSet.has(model)) {
      patch({ models: [...draft.models, model] });
    }
    setAdding(null);
  };

  const runTest = async () => {
    setTests((all) => ({ ...all, [current]: { status: 'testing' } }));
    try {
      const result = await systemConfigApi.testLLMChannel({
        name: requestName,
        protocol: draft.protocol,
        baseUrl: draft.baseUrl.trim(),
        apiKey: draft.apiKey.trim(),
        models: draft.models,
        enabled: true,
        useSavedSecret: !draft.apiKey.trim() && keySaved,
      });
      setTests((all) => ({
        ...all,
        [current]: result.success
          ? { status: 'ok', latencyMs: result.latencyMs ?? null }
          : { status: 'down', message: testMessage(result) },
      }));
    } catch (error) {
      setTests((all) => ({ ...all, [current]: { status: 'down', message: getParsedApiError(error).message } }));
    }
  };

  const save = async () => {
    const invalid = validateDraft(draft, { isNew, existingNames: names, keySaved });
    if (invalid) {
      setLocalError(invalid);
      return;
    }
    if (savedChannel) {
      const removed = draft.enabled ? saved.models.filter((model) => !enabledSet.has(model)) : saved.models;
      const blocked = inUseProblem(removed, saved.protocol, roles, providers)
        ?? (draft.protocol !== saved.protocol ? inUseProblem(saved.models, saved.protocol, roles, providers) : null);
      if (blocked) {
        setLocalError(blocked);
        return;
      }
    }
    setSaving(true);
    const outcome = await onSave(channelSaveItems(draft, names));
    setSaving(false);
    if (!outcome.ok) {
      setProblem(outcome);
      return;
    }
    const savedName = isNew ? normalizeChannelName(draft.name) : current;
    setDrafts((all) => {
      const next = { ...all };
      delete next[current];
      return next;
    });
    if (isNew) {
      // The new channel keeps its fetched list; the 添加渠道 form starts empty next time.
      setFetched((all) => {
        const next = { ...all, [savedName]: fetchedModels };
        delete next[NEW];
        return next;
      });
      setDiscovery((all) => {
        const next = found ? { ...all, [savedName]: found } : { ...all };
        delete next[NEW];
        return next;
      });
    }
    setShowKey(false);
    setSelected(savedName);
    showPlanToast(`渠道 ${savedName} 已保存`);
  };

  const remove = async () => {
    if (!savedChannel) {
      return;
    }
    const blocked = inUseProblem(saved.models, saved.protocol, roles, providers);
    if (blocked) {
      setLocalError(blocked);
      return;
    }
    if (!confirmDelete) {
      setConfirmDelete(true);
      return;
    }
    setSaving(true);
    const outcome = await onSave(channelDeleteItems(current, names));
    setSaving(false);
    setConfirmDelete(false);
    if (!outcome.ok) {
      setProblem(outcome);
      return;
    }
    showPlanToast(`渠道 ${current} 已删除`);
    select(names.find((name) => name !== current) ?? NEW);
  };

  const discard = () => {
    setDrafts((all) => {
      const next = { ...all };
      delete next[current];
      return next;
    });
    setLocalError(null);
    setProblem(null);
  };

  const dotClass = (name: string, enabled: boolean) => {
    const state = tests[name]?.status;
    if (state === 'ok' || state === 'down' || state === 'testing') return `st-${state}`;
    return enabled ? 'st-idle' : 'st-off';
  };

  return (
    <div className="chm">
      <nav className="chm-list" aria-label="渠道列表">
        <ul>
          {channels.map((channel) => (
            <li key={channel.name}>
              <button
                type="button"
                className="chm-item"
                aria-current={channel.name === current ? 'true' : undefined}
                onClick={() => select(channel.name)}
              >
                <span className="chm-item-name">{channel.name}</span>
                {channel.name === primaryChannel ? <span className="tag ok">默认</span> : null}
                {channel.enabled ? null : <span className="tag skip">已停用</span>}
                <span className={`chm-dot ${dotClass(channel.name, channel.enabled)}`} aria-hidden="true">
                  <span className="sdot" />
                </span>
              </button>
            </li>
          ))}
        </ul>
        <button
          type="button"
          className="chm-add"
          aria-current={isNew ? 'true' : undefined}
          onClick={() => select(NEW)}
        >
          + 添加渠道
        </button>
      </nav>

      <section className="chm-detail" aria-label={isNew ? '新渠道' : `渠道 ${current}`}>
        <div className="chm-head">
          <h4>{isNew ? '新渠道' : current}</h4>
          <label className="switch chm-switch" htmlFor={`${idPrefix}-enabled`}>
            <input
              id={`${idPrefix}-enabled`}
              type="checkbox"
              role="switch"
              checked={draft.enabled}
              onChange={(event) => patch({ enabled: event.target.checked })}
            />
            <span className="switch-track" aria-hidden="true" />
            <span>{draft.enabled ? '已启用' : '已停用'}</span>
          </label>
        </div>

        <div className="chm-fields">
          <div className="f chm-name">
            <label htmlFor={`${idPrefix}-name`}>名称</label>
            <input
              id={`${idPrefix}-name`}
              value={draft.name}
              readOnly={!isNew}
              placeholder="例如 darren_cpa"
              aria-describedby={`${idPrefix}-name-note`}
              onChange={(event) => patch({ name: event.target.value })}
            />
            <span className="f-label" id={`${idPrefix}-name-note`}>
              {isNew
                ? draft.name && normalizeChannelName(draft.name) !== draft.name
                  ? `保存为 ${normalizeChannelName(draft.name) || '（无效名称）'}，只能用小写字母、数字和下划线`
                  : '只能用小写字母、数字和下划线，保存后不能改'
                : '名称保存后不能改'}
            </span>
          </div>
          <label className="f" htmlFor={`${idPrefix}-base`}>
            接口地址
            <input
              id={`${idPrefix}-base`}
              type="text"
              inputMode="url"
              value={draft.baseUrl}
              placeholder={protocolInfo?.baseUrlHint ?? ''}
              onChange={(event) => patch({ baseUrl: event.target.value })}
            />
          </label>
          <label className="f" htmlFor={`${idPrefix}-protocol`}>
            API 格式
            <select
              id={`${idPrefix}-protocol`}
              value={draft.protocol}
              onChange={(event) => patch({ protocol: event.target.value })}
            >
              {CHANNEL_PROTOCOLS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <div className="f chm-key">
            <label htmlFor={`${idPrefix}-key`}>API Key</label>
            <span className="chm-key-row">
              <input
                id={`${idPrefix}-key`}
                type={showKey ? 'text' : 'password'}
                autoComplete="off"
                value={draft.apiKey}
                placeholder={keySaved ? '已保存，留空表示不修改' : draft.protocol === 'ollama' ? '本机 Ollama 可以不填' : '多个 Key 用英文逗号分隔'}
                onChange={(event) => patch({ apiKey: event.target.value })}
              />
              <button
                type="button"
                className="btn quiet sm"
                aria-pressed={showKey}
                onClick={() => setShowKey((value) => !value)}
              >
                {showKey ? '隐藏' : '显示'}
              </button>
            </span>
          </div>
        </div>

        <div className="chm-models">
          <div className="sec-head chm-models-head">
            <h5>模型列表</h5>
            <span className="chm-models-acts">
              <button
                type="button"
                className="btn sm"
                disabled={!discoverable || found?.status === 'loading'}
                onClick={() => void discover(current, draft, !draft.apiKey.trim() && keySaved)}
              >
                {found?.status === 'loading' ? '获取中…' : '获取模型'}
              </button>
              <button type="button" className="btn sm" onClick={() => setAdding('')}>
                + 添加模型
              </button>
            </span>
          </div>
          <p className="chm-models-meta" role="status" aria-live="polite">
            <span>
              {allModels.length} 个模型 · 已打开 {draft.models.length}
            </span>
            {found?.status === 'error' ? <span className="chm-err">获取失败：{found.message}</span> : null}
            {found?.status === 'ok' ? <span>已从接口获取 {found.count} 个</span> : null}
            {!discoverable ? <span>这种 API 格式需要手动添加模型</span> : null}
          </p>
          {adding !== null ? (
            <div className="chm-add-model">
              <label className="vh" htmlFor={`${idPrefix}-add`}>
                模型名称
              </label>
              <input
                id={`${idPrefix}-add`}
                autoFocus
                value={adding}
                placeholder="模型名称，例如 claude-opus-5-5"
                onChange={(event) => setAdding(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault();
                    addModel();
                  } else if (event.key === 'Escape') {
                    setAdding(null);
                  }
                }}
              />
              <button type="button" className="btn sm" onClick={addModel}>
                添加
              </button>
              <button type="button" className="btn quiet sm" onClick={() => setAdding(null)}>
                取消
              </button>
            </div>
          ) : null}
          {allModels.length > 0 ? (
            <div className="chm-filter">
              <label className="vh" htmlFor={`${idPrefix}-search`}>
                搜索模型
              </label>
              <input
                id={`${idPrefix}-search`}
                type="search"
                placeholder="搜索"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
              <button type="button" className="btn quiet sm" onClick={() => setVisible(true)}>
                全选
              </button>
              <button type="button" className="btn quiet sm" onClick={() => setVisible(false)}>
                全不选
              </button>
            </div>
          ) : null}
          {visibleModels.length > 0 ? (
            <ul className="chm-model-list">
              {visibleModels.map((model) => {
                const on = enabledSet.has(model);
                const inputId = `${idPrefix}-m-${model}`;
                return (
                  <li key={model} className={on ? 'on' : undefined}>
                    <label className="chm-model" htmlFor={inputId}>
                      <span className="chm-model-id">{model}</span>
                      <input
                        id={inputId}
                        type="checkbox"
                        role="switch"
                        checked={on}
                        aria-label={`使用 ${model}`}
                        onChange={(event) => toggleModel(model, event.target.checked)}
                      />
                      <span className="switch-track" aria-hidden="true" />
                    </label>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="note pref-flush">
              {allModels.length > 0
                ? '没有匹配的模型。'
                : discoverable
                  ? '还没有模型。填好接口地址和 API Key 后点「获取模型」，或者手动添加。'
                  : '还没有模型。点「添加模型」填写模型名称。'}
            </p>
          )}
        </div>

        {test ? (
          <p className={`ch-st chm-test st-${test.status}`} role="status" aria-live="polite">
            <span className="sdot" aria-hidden="true" />
            {test.status === 'testing' ? '正在测试连接…' : null}
            {test.status === 'ok'
              ? `连接正常${test.latencyMs ? `，${(test.latencyMs / 1000).toFixed(1)} 秒` : ''}`
              : null}
            {test.status === 'down' ? `连接失败：${test.message}` : null}
          </p>
        ) : null}
        {localError ? (
          <p className="err" role="alert">
            {localError}
          </p>
        ) : null}
        <SaveProblem outcome={problem} onReload={() => { discard(); onReload(); }} />

        <div className="actions pref-actions chm-actions">
          <button type="button" className="btn primary" disabled={!dirty || saving} onClick={() => void save()}>
            {saving ? '正在保存…' : isNew ? '添加渠道' : '保存渠道'}
          </button>
          <button
            type="button"
            className="btn sm"
            disabled={draft.models.length === 0 || test?.status === 'testing'}
            onClick={() => void runTest()}
          >
            测试连接
          </button>
          {dirty ? (
            <button type="button" className="btn quiet sm" onClick={discard}>
              放弃修改
            </button>
          ) : null}
          {!isNew ? (
            <button
              type="button"
              className={`btn sm chm-delete${confirmDelete ? ' confirm' : ''}`}
              disabled={saving}
              onClick={() => void remove()}
            >
              {confirmDelete ? '确认删除' : '删除渠道'}
            </button>
          ) : null}
        </div>
      </section>
    </div>
  );
};
