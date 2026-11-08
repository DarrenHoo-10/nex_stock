import type React from 'react';
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { QRCodeSVG } from 'qrcode.react';
import { systemConfigApi } from '../../../api/systemConfig';
import { getParsedApiError } from '../../../api/error';
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

type ScanState =
  | { phase: 'idle' }
  | { phase: 'starting' }
  | { phase: 'waiting'; url: string }
  | { phase: 'done'; restartRequired: boolean }
  | { phase: 'failed'; message: string };

const SCAN_FAILURE: Record<string, string> = {
  denied: '你在飞书里取消了授权，需要的话重新扫码。',
  expired: '二维码已过期，请重新生成。',
};

/**
 * Scan-to-create flow: the backend asks Feishu for a QR link, polls until the scan is authorised and saves the new
 * bot's credentials itself (they never reach the browser).
 */
function useFeishuScan(onConnected: () => void) {
  const [scan, setScan] = useState<ScanState>({ phase: 'idle' });
  const runRef = useRef(0);

  const cancel = useCallback(() => {
    runRef.current += 1;
  }, []);
  useEffect(() => cancel, [cancel]);

  const start = useCallback(async () => {
    const run = ++runRef.current;
    setScan({ phase: 'starting' });
    try {
      const session = await systemConfigApi.startFeishuRegistration();
      if (run !== runRef.current) return;
      setScan({ phase: 'waiting', url: session.verificationUrl });
      let interval = session.interval;
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, Math.max(interval, 1) * 1000));
        if (run !== runRef.current) return;
        const status = await systemConfigApi.pollFeishuRegistration(session.sessionId);
        if (run !== runRef.current) return;
        if (status.status === 'pending') {
          interval = status.interval ?? interval;
          continue;
        }
        if (status.status === 'success') {
          setScan({ phase: 'done', restartRequired: status.restartRequired });
          showPlanToast(status.restartRequired ? '飞书机器人已连接，重启服务后长连接生效' : '飞书机器人已连接');
          onConnected();
        } else {
          setScan({ phase: 'failed', message: SCAN_FAILURE[status.status] ?? status.message ?? '扫码创建失败，请重试。' });
        }
        return;
      }
    } catch (error) {
      if (run === runRef.current) {
        setScan({ phase: 'failed', message: getParsedApiError(error).message });
      }
    }
  }, [onConnected]);

  const reset = useCallback(() => {
    cancel();
    setScan({ phase: 'idle' });
  }, [cancel]);

  return { scan, start, reset };
}

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

  const { scan, start: startScan, reset: resetScan } = useFeishuScan(onReload);
  const scanning = scan.phase === 'starting' || scan.phase === 'waiting';

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
          <h4>扫码连接飞书（推荐）</h4>
          <span className={`tag ${appReady ? 'ok' : 'warn'}`} data-testid="feishu-app-status">
            {appReady ? '已连接' : '未配置'}
          </span>
        </div>
        <p className="note">
          用飞书扫一下二维码，会自动创建你自己的机器人并填好凭证，计划默认推送到你和机器人的私聊。
          在飞书里发 /bind 可以改成推送到某个群。
        </p>
        {scan.phase === 'waiting' ? (
          <div className="feishu-qr" data-testid="feishu-qr">
            <QRCodeSVG value={scan.url} size={168} marginSize={2} />
            <p className="note">打开飞书，扫描二维码并确认。扫码后这里会自动完成。</p>
          </div>
        ) : null}
        {scan.phase === 'done' ? (
          <p className="note" data-testid="feishu-scan-done">
            已连接飞书机器人。
            {scan.restartRequired ? '长连接需要重启服务后才会用上新机器人。' : '长连接已启动，现在可以在飞书里直接问助手。'}
          </p>
        ) : null}
        {scan.phase === 'failed' ? (
          <p className="note" role="alert" data-testid="feishu-scan-error">{scan.message}</p>
        ) : null}
        <div className="actions pref-actions">
          {scanning ? (
            <button type="button" className="btn quiet" onClick={resetScan}>
              取消扫码
            </button>
          ) : (
            <button type="button" className="btn primary" onClick={() => void startScan()} data-testid="feishu-scan-start">
              {appReady ? '重新扫码创建' : '扫码创建飞书机器人'}
            </button>
          )}
          {scan.phase === 'starting' ? <span className="note">正在生成二维码…</span> : null}
        </div>
      </div>

      <details className="feishu-group feishu-manual">
        <summary>手动配置（已有机器人 / 群 Webhook）</summary>
        <div className="sec-head">
          <h4>应用机器人</h4>
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

      <div className="feishu-sub">
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
      </details>
    </form>
  );
};
