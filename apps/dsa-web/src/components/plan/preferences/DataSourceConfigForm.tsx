import type React from 'react';
import { useId, useState } from 'react';
import type { ConfigView, SaveOutcome } from './usePreferencesConfig';
import { SaveProblem } from './shared';

const SECRET_KEY = /(KEY|SECRET|TOKEN|LICEN[CS]E|PASSWORD)$/i;

/** Readable names for the data-source keys (the backend schema only has English titles, some none). */
const KEY_LABELS: Record<string, string> = {
  TUSHARE_TOKEN: 'Tushare Token',
  TICKFLOW_API_KEY: 'TickFlow API Key',
  TICKFLOW_PRIORITY: '日 K 优先级（数字越小越优先）',
  BIYING_LICENCE: '必盈 licence',
  BIYING_TIMEOUT_SECONDS: '单次请求超时（秒）',
  LONGBRIDGE_APP_KEY: 'App Key',
  LONGBRIDGE_APP_SECRET: 'App Secret',
  LONGBRIDGE_ACCESS_TOKEN: 'Access Token',
  LONGBRIDGE_OAUTH_CLIENT_ID: 'OAuth Client ID（与上面三项二选一）',
  FUTU_OPEND_HOST: 'OpenD 地址',
  FUTU_OPEND_PORT: 'OpenD 端口',
  FINNHUB_API_KEY: 'API Key',
  ALPHAVANTAGE_API_KEY: 'API Key',
};

function isSecret(key: string, config: ConfigView): boolean {
  const schema = config.get(key)?.schema;
  return schema ? Boolean(schema.isSensitive) : SECRET_KEY.test(key);
}

export interface DataSourceConfigFormProps {
  label: string;
  keys: string[];
  config: ConfigView;
  onSave: (items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>;
  onReload: () => void;
  /** Called after a successful save (refresh the status list) and on cancel. */
  onDone: (saved: boolean) => void;
}

/**
 * Edits the config keys of one data source in place (PUT /system/config with the page's config version).
 * Secrets are never shown: a configured one shows 「已配置」 and is only replaced when a new value is typed.
 */
export const DataSourceConfigForm: React.FC<DataSourceConfigFormProps> = ({
  label,
  keys,
  config,
  onSave,
  onReload,
  onDone,
}) => {
  const idPrefix = useId();
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(keys.map((key) => [key, isSecret(key, config) ? '' : config.value(key)])),
  );
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<Exclude<SaveOutcome, { ok: true }> | null>(null);

  const changed = keys
    .filter((key) => (isSecret(key, config) ? values[key].trim() !== '' : values[key].trim() !== config.value(key).trim()))
    .map((key) => ({ key, value: values[key].trim() }));

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (changed.length === 0 || saving) {
      return;
    }
    setSaving(true);
    const outcome = await onSave(changed);
    setSaving(false);
    if (outcome.ok) {
      setProblem(null);
      onDone(true);
      return;
    }
    if (outcome.kind === 'validation' && outcome.issues.length > 0) {
      setProblem({ ...outcome, message: outcome.issues.map((issue) => `${issue.key}：${issue.message}`).join('；') });
      return;
    }
    setProblem(outcome);
  };

  return (
    <form className="pref-form src-config" onSubmit={handleSubmit} noValidate aria-label={`${label} 配置`}>
      <div className="form-grid">
        {keys.map((key) => {
          const secret = isSecret(key, config);
          const id = `${idPrefix}-${key}`;
          return (
            <div className="f-cell" key={key}>
              <label className="f" htmlFor={id}>
                {KEY_LABELS[key] ?? config.get(key)?.schema?.title ?? key}
                <input
                  id={id}
                  name={key}
                  type={secret ? 'password' : 'text'}
                  autoComplete="off"
                  value={values[key]}
                  placeholder={secret ? (config.isSet(key) ? '已配置，留空不修改' : '未配置') : config.defaultOf(key) ?? ''}
                  onChange={(event) => setValues({ ...values, [key]: event.target.value })}
                />
              </label>
              <span className="src-key">{key}</span>
            </div>
          );
        })}
      </div>
      <SaveProblem outcome={problem} onReload={onReload} />
      <div className="src-actions">
        <button type="submit" className="btn sm primary" disabled={changed.length === 0 || saving}>
          {saving ? '正在保存…' : '保存'}
        </button>
        <button type="button" className="btn sm" onClick={() => onDone(false)} disabled={saving}>
          取消
        </button>
      </div>
      <p className="note">保存后立即生效，不需要重启。</p>
    </form>
  );
};
