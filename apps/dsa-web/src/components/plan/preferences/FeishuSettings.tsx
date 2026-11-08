import type React from 'react';
import { useId, useState } from 'react';
import { showPlanToast } from '../../../stores/planToastStore';
import { SaveProblem } from './shared';
import type { ConfigView, SaveOutcome } from './usePreferencesConfig';

const TRUE_VALUES = new Set(['1', 'true', 'yes', 'on']);

/** Secrets are never read back: an empty input keeps the saved value. */
const SECRET_KEYS = new Set(['FEISHU_APP_SECRET', 'FEISHU_WEBHOOK_URL', 'FEISHU_WEBHOOK_SECRET']);

interface FieldSpec {
  key: string;
  label: string;
  placeholder: string;
}

const APP_FIELDS: FieldSpec[] = [
  { key: 'FEISHU_APP_ID', label: 'App ID', placeholder: 'cli_xxxxxxxx' },
  { key: 'FEISHU_APP_SECRET', label: 'App Secret', placeholder: '飞书开放平台里的应用凭证' },
];

const WEBHOOK_FIELDS: FieldSpec[] = [
  { key: 'FEISHU_WEBHOOK_URL', label: 'Webhook 地址', placeholder: 'https://open.feishu.cn/open-apis/bot/v2/hook/…' },
  { key: 'FEISHU_WEBHOOK_SECRET', label: '签名密钥', placeholder: '群机器人开了签名校验时填写' },
  { key: 'FEISHU_WEBHOOK_KEYWORD', label: '关键词', placeholder: '群机器人设置了关键词时填写' },
];

interface FeishuSettingsProps {
  config: ConfigView;
  onSave: (items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>;
  onReload: () => void;
}

/**
 * Feishu delivery on the 运行和推送 tab: the app bot (App ID / Secret + long connection, recommended; the target chat is
 * bound by sending /bind to the bot) and the group webhook fallback. Saved straight into the config.
 */
export const FeishuSettings: React.FC<FeishuSettingsProps> = ({ config, onSave, onReload }) => {
  const idPrefix = useId();
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [streamEdit, setStreamEdit] = useState<boolean | null>(null);
  const [problem, setProblem] = useState<Exclude<SaveOutcome, { ok: true }> | null>(null);
  const [saving, setSaving] = useState(false);

  const savedStream = TRUE_VALUES.has(config.value('FEISHU_STREAM_ENABLED').trim().toLowerCase());
  const stream = streamEdit ?? savedStream;
  const appReady = config.isSet('FEISHU_APP_ID') && config.isSet('FEISHU_APP_SECRET');
  const webhookReady = config.isSet('FEISHU_WEBHOOK_URL');

  const valueOf = (key: string) => edits[key] ?? (SECRET_KEYS.has(key) ? '' : config.value(key));

  const changes: Array<{ key: string; value: string }> = [];
  for (const [key, value] of Object.entries(edits)) {
    const trimmed = value.trim();
    if (SECRET_KEYS.has(key) ? trimmed !== '' : trimmed !== config.value(key).trim()) {
      changes.push({ key, value: trimmed });
    }
  }
  if (streamEdit !== null && streamEdit !== savedStream) {
    changes.push({ key: 'FEISHU_STREAM_ENABLED', value: streamEdit ? 'true' : 'false' });
  }
  const dirty = changes.length > 0;

  const discard = () => {
    setEdits({});
    setStreamEdit(null);
    setProblem(null);
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!dirty) {
      return;
    }
    setSaving(true);
    const outcome = await onSave(changes);
    setSaving(false);
    if (outcome.ok) {
      discard();
      showPlanToast('飞书设置已保存');
      return;
    }
    setProblem(outcome);
  };

  const renderField = (field: FieldSpec) => {
    const secret = SECRET_KEYS.has(field.key);
    const isSaved = config.isSet(field.key);
    return (
      <label className="f" htmlFor={`${idPrefix}-${field.key}`} key={field.key}>
        {field.label}
        <input
          id={`${idPrefix}-${field.key}`}
          type={secret ? 'password' : 'text'}
          autoComplete="off"
          value={valueOf(field.key)}
          placeholder={secret && isSaved ? '已保存，留空表示不修改' : field.placeholder}
          onChange={(event) => {
            setEdits((all) => ({ ...all, [field.key]: event.target.value }));
            setProblem(null);
          }}
        />
      </label>
    );
  };

  return (
    <form className="feishu-form" onSubmit={save} noValidate>
      <div className="feishu-group">
        <div className="sec-head">
          <h4>应用机器人（推荐）</h4>
          <span className={`tag ${appReady ? 'ok' : 'warn'}`} data-testid="feishu-app-status">
            {appReady ? '已配置' : '未配置'}
          </span>
        </div>
        <div className="form-grid feishu-grid">{APP_FIELDS.map(renderField)}</div>
        <label className="switch" htmlFor={`${idPrefix}-stream`}>
          <input
            id={`${idPrefix}-stream`}
            type="checkbox"
            role="switch"
            checked={stream}
            onChange={(event) => {
              setStreamEdit(event.target.checked);
              setProblem(null);
            }}
          />
          <span className="switch-track" aria-hidden="true" />
          <span>开启长连接：在飞书里直接问助手，发送 /bind 设定推送到哪个会话</span>
        </label>
        <p className="note">长连接在服务启动时建立，改动 App ID、Secret 或这个开关后需要重启服务才生效。</p>
      </div>

      <div className="feishu-group">
        <div className="sec-head">
          <h4>群 Webhook（备选）</h4>
          <span className={`tag ${webhookReady ? 'ok' : 'line'}`} data-testid="feishu-webhook-status">
            {webhookReady ? '已配置' : '未配置'}
          </span>
        </div>
        <div className="form-grid feishu-grid">{WEBHOOK_FIELDS.map(renderField)}</div>
        <p className="note">没有用 /bind 绑定会话时，计划推送到这个群机器人。</p>
      </div>

      <SaveProblem outcome={problem} onReload={() => { discard(); onReload(); }} />
      <div className="actions pref-actions">
        <button type="submit" className="btn primary" disabled={!dirty || saving}>
          {saving ? '正在保存…' : '保存飞书设置'}
        </button>
        {dirty ? (
          <button type="button" className="btn quiet" onClick={discard}>
            放弃修改
          </button>
        ) : null}
      </div>
    </form>
  );
};
