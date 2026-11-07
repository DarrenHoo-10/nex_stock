import type React from 'react';
import { useId, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { systemConfigApi } from '../../../api/systemConfig';
import { getParsedApiError } from '../../../api/error';
import { showPlanToast } from '../../../stores/planToastStore';
import {
  MODEL_KEYS,
  buildModelOptions,
  diffModelItems,
  parseChannels,
  readModelValues,
  validateTemperature,
} from './llmModels';
import type { ModelChannel, ModelOption, ModelValues } from './llmModels';
import { PanelHead, SaveProblem } from './shared';
import type { ConfigView, SaveOutcome } from './usePreferencesConfig';

interface ModelsPanelProps {
  config: ConfigView;
  onSave: (items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>;
  onReload: () => void;
}

type ChannelTest =
  | { status: 'testing' }
  | { status: 'ok'; latencyMs: number | null }
  | { status: 'down'; message: string };

function labelOf(value: string, options: ModelOption[]): string {
  const match = options.find((option) => option.value === value);
  if (match) {
    return `${match.label}（${match.channel}）`;
  }
  return value ? `${value}（当前配置）` : '未设置';
}

/** `<option>`s grouped by channel; a configured value that no channel lists stays selectable. */
const ModelSelectOptions: React.FC<{
  options: ModelOption[];
  current: string;
  exclude?: string[];
  emptyLabel?: string;
}> = ({ options, current, exclude = [], emptyLabel }) => {
  const groups = new Map<string, ModelOption[]>();
  for (const option of options) {
    if (exclude.includes(option.value) && option.value !== current) {
      continue;
    }
    groups.set(option.channel, [...(groups.get(option.channel) ?? []), option]);
  }
  const known = options.some((option) => option.value === current);
  return (
    <>
      {emptyLabel !== undefined ? <option value="">{emptyLabel}</option> : null}
      {current && !known ? <option value={current}>{`${current}（当前配置）`}</option> : null}
      {[...groups.entries()].map(([channel, items]) => (
        <optgroup key={channel} label={channel}>
          {items.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </optgroup>
      ))}
    </>
  );
};

export const ModelsPanel: React.FC<ModelsPanelProps> = ({ config, onSave, onReload }) => {
  const saved = useMemo(() => readModelValues(config), [config]);
  const channels = useMemo(() => parseChannels(config), [config]);
  const options = useMemo(() => buildModelOptions(channels, config.providers), [channels, config.providers]);

  const [edits, setEdits] = useState<Partial<ModelValues>>({});
  const [problem, setProblem] = useState<Exclude<SaveOutcome, { ok: true }> | null>(null);
  const [temperatureError, setTemperatureError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [tests, setTests] = useState<Record<string, ChannelTest>>({});
  const idPrefix = useId();

  const draft: ModelValues = { ...saved, ...edits };
  const changes = diffModelItems(saved, draft);
  const dirty = changes.length > 0;
  const addable = options.filter(
    (option) => option.value !== draft.primary && !draft.fallbacks.includes(option.value),
  );

  const patch = (next: Partial<ModelValues>) => {
    setEdits((current) => ({ ...current, ...next }));
    setProblem(null);
  };

  const changePrimary = (value: string) => {
    // The primary model cannot also be a fallback.
    patch({ primary: value, fallbacks: draft.fallbacks.filter((model) => model !== value) });
  };

  const moveUp = (index: number) => {
    const next = [...draft.fallbacks];
    [next[index - 1], next[index]] = [next[index], next[index - 1]];
    patch({ fallbacks: next });
  };

  const discard = () => {
    setEdits({});
    setProblem(null);
    setTemperatureError(null);
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    const error = validateTemperature(draft.temperature);
    setTemperatureError(error);
    if (error || !dirty) {
      return;
    }
    setSaving(true);
    const outcome = await onSave(changes);
    setSaving(false);
    if (outcome.ok) {
      setEdits({});
      setProblem(null);
      showPlanToast('模型设置已保存');
      return;
    }
    setProblem(outcome);
  };

  const testChannel = async (channel: ModelChannel) => {
    setTests((current) => ({ ...current, [channel.name]: { status: 'testing' } }));
    try {
      const result = await systemConfigApi.testLLMChannel({
        name: channel.name,
        protocol: channel.protocol,
        baseUrl: channel.baseUrl,
        models: channel.models,
        enabled: channel.enabled,
        useSavedSecret: true,
      });
      setTests((current) => ({
        ...current,
        [channel.name]: result.success
          ? { status: 'ok', latencyMs: result.latencyMs ?? null }
          : { status: 'down', message: result.message || result.error || '连接失败' },
      }));
    } catch (error) {
      setTests((current) => ({
        ...current,
        [channel.name]: { status: 'down', message: getParsedApiError(error).message },
      }));
    }
  };

  const primaryChannel = options.find((option) => option.value === draft.primary)?.channel;
  const primaryTest = primaryChannel ? tests[primaryChannel] : undefined;

  return (
    <>
      <PanelHead
        title="模型"
        sub="每天 16:00 的分析和助手的回答用哪个模型。改完点保存，下一次运行生效"
      />
      <form className="pref-form" onSubmit={handleSubmit} noValidate>
        <ul className="roles">
          <li>
            <div>
              <p className="rk">分析模型</p>
              <p className="rd">
                16:00 分析板块、选股、自选和持仓，生成计划和飞书消息 <span className="role-key">{MODEL_KEYS.primary}</span>
              </p>
            </div>
            <div className="f-cell">
              <label className="vh" htmlFor={`${idPrefix}-primary`}>
                分析模型
              </label>
              <select
                id={`${idPrefix}-primary`}
                value={draft.primary}
                onChange={(event) => changePrimary(event.target.value)}
              >
                <ModelSelectOptions options={options} current={draft.primary} emptyLabel="未设置" />
              </select>
            </div>
          </li>
          <li>
            <div>
              <p className="rk">备用模型</p>
              <p className="rd">
                主模型超时或报错时，按顺序换下一个；都失败时这只股票标为「分析失败」 <span className="role-key">{MODEL_KEYS.fallbacks}</span>
              </p>
            </div>
            <div className="fb-box">
              {draft.fallbacks.length > 0 ? (
                <ol className="fb">
                  {draft.fallbacks.map((model, index) => (
                    <li key={model}>
                      <span className="fb-n num">{index + 1}</span>
                      <span className="fb-name">{labelOf(model, options)}</span>
                      <span className="fb-acts">
                        {index > 0 ? (
                          <button
                            type="button"
                            className="btn quiet sm"
                            aria-label={`上移 ${model}`}
                            onClick={() => moveUp(index)}
                          >
                            上移
                          </button>
                        ) : null}
                        <button
                          type="button"
                          className="btn quiet sm"
                          aria-label={`移除 ${model}`}
                          onClick={() => patch({ fallbacks: draft.fallbacks.filter((item) => item !== model) })}
                        >
                          移除
                        </button>
                      </span>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="note pref-flush">没有备用模型，主模型失败时直接标为「分析失败」。</p>
              )}
              {addable.length > 0 ? (
                <div className="f-cell">
                  <label className="vh" htmlFor={`${idPrefix}-fb-add`}>
                    添加备用模型
                  </label>
                  <select
                    id={`${idPrefix}-fb-add`}
                    value=""
                    onChange={(event) => {
                      if (event.target.value) {
                        patch({ fallbacks: [...draft.fallbacks, event.target.value] });
                      }
                    }}
                  >
                    <option value="">+ 添加备用模型</option>
                    <ModelSelectOptions
                      options={addable}
                      current=""
                      exclude={[draft.primary, ...draft.fallbacks]}
                    />
                  </select>
                </div>
              ) : null}
            </div>
          </li>
          <li>
            <div>
              <p className="rk">助手模型</p>
              <p className="rd">
                回答追问。不单独设置时和分析模型用同一个模型 <span className="role-key">{MODEL_KEYS.agent}</span>
              </p>
            </div>
            <div className="f-cell">
              <label className="vh" htmlFor={`${idPrefix}-agent`}>
                助手模型
              </label>
              <select
                id={`${idPrefix}-agent`}
                value={draft.agent}
                onChange={(event) => patch({ agent: event.target.value })}
              >
                <ModelSelectOptions options={options} current={draft.agent} emptyLabel="跟随分析模型" />
              </select>
            </div>
          </li>
          <li>
            <div>
              <p className="rk">识图模型</p>
              <p className="rd">
                从持仓截图或自选截图里识别股票代码，需要支持图片的模型 <span className="role-key">{MODEL_KEYS.vision}</span>
              </p>
            </div>
            <div className="f-cell">
              <label className="vh" htmlFor={`${idPrefix}-vision`}>
                识图模型
              </label>
              <select
                id={`${idPrefix}-vision`}
                value={draft.vision}
                onChange={(event) => patch({ vision: event.target.value })}
              >
                <ModelSelectOptions options={options} current={draft.vision} emptyLabel="不单独设置" />
              </select>
            </div>
          </li>
          <li>
            <div>
              <p className="rk">温度</p>
              <p className="rd">
                0 到 2，越低回答越稳定。留空使用模型默认值 <span className="role-key">{MODEL_KEYS.temperature}</span>
              </p>
            </div>
            <div className="f-cell">
              <label className="f" htmlFor={`${idPrefix}-temp`}>
                <span className="vh">温度</span>
                <input
                  id={`${idPrefix}-temp`}
                  inputMode="decimal"
                  value={draft.temperature}
                  aria-invalid={temperatureError ? true : undefined}
                  aria-describedby={temperatureError ? `${idPrefix}-temp-err` : undefined}
                  onChange={(event) => {
                    patch({ temperature: event.target.value });
                    setTemperatureError(null);
                  }}
                />
              </label>
              {temperatureError ? (
                <span className="field-err" id={`${idPrefix}-temp-err`}>
                  {temperatureError}
                </span>
              ) : null}
            </div>
          </li>
        </ul>

        {primaryTest?.status === 'down' ? (
          <div className="notice pref-notice" role="alert">
            <b>主模型不可用</b>
            <p>{primaryChannel} 渠道最近一次连接失败：{primaryTest.message}</p>
          </div>
        ) : null}
        <SaveProblem outcome={problem} onReload={() => { discard(); onReload(); }} />
        <div className="actions pref-actions">
          <button type="submit" className="btn primary" disabled={!dirty || saving}>
            {saving ? '正在保存…' : '保存模型设置'}
          </button>
          {dirty ? (
            <button type="button" className="btn quiet" onClick={discard}>
              放弃修改
            </button>
          ) : (
            <span className="note pref-flush">没有未保存的修改</span>
          )}
        </div>
      </form>

      <section className="pref-section" aria-labelledby={`${idPrefix}-channels`}>
        <div className="sec-head">
          <h3 id={`${idPrefix}-channels`}>渠道</h3>
          <Link className="btn quiet sm" to="/settings?category=ai_model">
            高级设置
          </Link>
        </div>
        <p className="note pref-flush">
          渠道、接口地址和密钥在「高级设置」里增删改；这里只列出已配置的渠道，密钥不会显示。
        </p>
        {channels.length === 0 ? (
          <p className="note">还没有配置渠道。到「高级设置」添加后，这里就能选模型。</p>
        ) : (
          <ul className="chs pref-chs">
            {channels.map((channel) => {
              const test = tests[channel.name];
              return (
                <li className="ch" key={channel.name}>
                  <div className="ch-main">
                    <p className="ch-name">
                      <b>{channel.name}</b>
                      <span className="tag line">{channel.protocol}</span>
                      {channel.enabled ? null : <span className="tag skip">已停用</span>}
                    </p>
                    <p className="ch-meta">
                      {channel.host ? `${channel.host}，` : ''}
                      {channel.keyConfigured ? '密钥已配置' : '未保存密钥'}
                    </p>
                    <p className="ch-models">
                      {channel.models.map((model) => (
                        <span className="tag" key={model}>
                          {model}
                        </span>
                      ))}
                    </p>
                  </div>
                  <div className="ch-side">
                    <p
                      className={`ch-st ${test ? `st-${test.status}` : ''}`}
                      role="status"
                      aria-live="polite"
                    >
                      <span className="sdot" aria-hidden="true" />
                      {test?.status === 'testing' ? '测试中' : null}
                      {test?.status === 'ok'
                        ? `正常${test.latencyMs ? `，${(test.latencyMs / 1000).toFixed(1)} 秒` : ''}`
                        : null}
                      {test?.status === 'down' ? '连接失败' : null}
                      {test ? null : '未测试'}
                    </p>
                    <p className="ch-btns">
                      <button
                        type="button"
                        className="btn sm"
                        disabled={!channel.enabled || test?.status === 'testing'}
                        onClick={() => void testChannel(channel)}
                      >
                        测试连接
                      </button>
                    </p>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </>
  );
};
