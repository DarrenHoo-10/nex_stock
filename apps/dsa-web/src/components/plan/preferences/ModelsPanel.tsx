import type React from 'react';
import { useId, useMemo, useState } from 'react';
import { showPlanToast } from '../../../stores/planToastStore';
import { ChannelManager } from './ChannelManager';
import {
  EFFORT_LEVELS,
  MODEL_KEYS,
  buildModelOptions,
  diffModelItems,
  parseChannels,
  readModelValues,
  validateTemperature,
} from './llmModels';
import type { ModelOption, ModelValues } from './llmModels';
import { PanelHead, SaveProblem } from './shared';
import type { ConfigView, SaveOutcome } from './usePreferencesConfig';

interface ModelsPanelProps {
  config: ConfigView;
  onSave: (items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>;
  onReload: () => void;
}

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

const EffortSelect: React.FC<{
  id: string;
  label: string;
  value: string;
  emptyLabel: string;
  onChange: (value: string) => void;
}> = ({ id, label, value, emptyLabel, onChange }) => {
  const known = value === '' || EFFORT_LEVELS.some((level) => level.value === value);
  return (
    <span className="role-effort">
      <label className="f-label" htmlFor={id}>
        {label}
      </label>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">{emptyLabel}</option>
        {known ? null : <option value={value}>{`${value}（当前配置）`}</option>}
        {EFFORT_LEVELS.map((level) => (
          <option key={level.value} value={level.value}>
            {level.label}
          </option>
        ))}
      </select>
    </span>
  );
};

export const ModelsPanel: React.FC<ModelsPanelProps> = ({ config, onSave, onReload }) => {
  const saved = useMemo(() => readModelValues(config), [config]);
  const channels = useMemo(() => parseChannels(config), [config]);
  const providers = useMemo(() => new Set(config.providers.map((provider) => provider.toLowerCase())), [config.providers]);
  const options = useMemo(() => buildModelOptions(channels, config.providers), [channels, config.providers]);

  const [edits, setEdits] = useState<Partial<ModelValues>>({});
  const [problem, setProblem] = useState<Exclude<SaveOutcome, { ok: true }> | null>(null);
  const [temperatureError, setTemperatureError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const idPrefix = useId();

  const draft: ModelValues = { ...saved, ...edits };
  const changes = diffModelItems(saved, draft);
  const dirty = changes.length > 0;
  const addable = options.filter(
    (option) => option.value !== draft.primary && !draft.fallbacks.includes(option.value),
  );
  const primaryChannel = options.find((option) => option.value === saved.primary)?.channel;
  const noChannels = options.length === 0;

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

  return (
    <>
      <PanelHead title="模型" sub="先添加渠道（接口地址和 API Key）并打开要用的模型，再给每个用途选模型。保存后，下一次调用就用新设置" />

      <section className="pref-section pref-first" aria-labelledby={`${idPrefix}-channels`}>
        <div className="sec-head">
          <h3 id={`${idPrefix}-channels`}>渠道</h3>
        </div>
        <ChannelManager
          channels={channels}
          roles={saved}
          providers={providers}
          primaryChannel={primaryChannel}
          onSave={onSave}
          onReload={onReload}
        />
      </section>

      <section className="pref-section" aria-labelledby={`${idPrefix}-roles`}>
        <div className="sec-head">
          <h3 id={`${idPrefix}-roles`}>用途</h3>
        </div>
        {noChannels ? (
          <p className="note pref-flush">还没有可选的模型。先在上面添加渠道并打开模型。</p>
        ) : null}
        <form className="pref-form pref-flush" onSubmit={handleSubmit} noValidate>
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
                <EffortSelect
                  id={`${idPrefix}-effort`}
                  label="推理强度（备用模型同样使用）"
                  value={draft.effort}
                  emptyLabel="模型默认"
                  onChange={(value) => patch({ effort: value })}
                />
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
                <EffortSelect
                  id={`${idPrefix}-agent-effort`}
                  label="推理强度"
                  value={draft.agentEffort}
                  emptyLabel="跟随分析模型"
                  onChange={(value) => patch({ agentEffort: value })}
                />
              </div>
            </li>
            <li>
              <div>
                <p className="rk">轻量模型</p>
                <p className="rd">
                  资讯分类、打标签、短摘要这类杂活。不单独设置时用分析模型 <span className="role-key">{MODEL_KEYS.light}</span>
                </p>
              </div>
              <div className="f-cell">
                <label className="vh" htmlFor={`${idPrefix}-light`}>
                  轻量模型
                </label>
                <select
                  id={`${idPrefix}-light`}
                  value={draft.light}
                  onChange={(event) => patch({ light: event.target.value })}
                >
                  <ModelSelectOptions options={options} current={draft.light} emptyLabel="跟随分析模型" />
                </select>
                <EffortSelect
                  id={`${idPrefix}-light-effort`}
                  label="推理强度"
                  value={draft.lightEffort}
                  emptyLabel="模型默认"
                  onChange={(value) => patch({ lightEffort: value })}
                />
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
                    placeholder="模型默认"
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

          <p className="note">推理强度越高越慢、用的 token 越多。「模型默认」表示不发送这个参数。</p>
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
      </section>
    </>
  );
};
