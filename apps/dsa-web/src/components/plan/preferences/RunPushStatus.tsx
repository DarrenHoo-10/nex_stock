import type React from 'react';
import type { PushStatus, RunPush } from '../../../types/dailyPlan';
import { formatClock } from './format';

const PUSH_STATUS_LABEL: Record<PushStatus, string> = {
  sent: '已发送',
  failed: '发送失败',
  skipped: '已跳过',
  pending: '等待发送',
};

/** Channel types a personal push can report (`push.channels[].channel`); unknown ones are shown as they come. */
const CHANNEL_TYPE_LABEL: Record<string, string> = {
  feishu: '飞书',
  dingtalk: '钉钉',
  wechat: '企业微信',
  wecom: '企业微信',
  telegram: 'Telegram',
  email: '邮件',
  pushplus: 'PushPlus',
  serverchan3: 'Server酱3',
  custom: '自定义 Webhook',
};

/** Why a personal push did nothing, as the server reports it in `push.reason`. */
const REASON_TEXT: Record<string, string> = {
  no_model: '未配置模型：计划已按规则生成并保存，网页可查看，不推送；配置自己的模型后恢复推送',
};

/**
 * The push line of a plan run. The primary user's run pushes through the system Feishu route (`channel: 'feishu'`,
 * also what older runs without the field mean); a regular user's run pushes to their own channels
 * (`channel: 'personal'`) and says why nothing was sent (no model / no channel) and what each channel type returned.
 */
export const RunPushStatus: React.FC<{ push: RunPush }> = ({ push }) => {
  if (push.channel !== 'personal') {
    if (push.reason === 'managed_feishu') {
      return <p className="pref-push-line" data-testid="push-status">飞书推送：由 NexBot 按订阅投递</p>;
    }
    return (
      <p className="pref-push-line" data-testid="push-status">
        飞书推送：{push.configured ? PUSH_STATUS_LABEL[push.status] : '未配置，没有发送'}
        {push.status === 'sent' && push.sentAt ? `（${formatClock(push.sentAt)}）` : ''}
        {push.status === 'failed' && push.error ? `：${push.error}` : ''}
      </p>
    );
  }

  const reasonText = push.reason ? REASON_TEXT[push.reason] ?? null : null;
  const channels = push.channels ?? [];
  let summary: string;
  if (reasonText) {
    summary = reasonText;
  } else if (!push.configured) {
    summary = '没有可用的推送渠道，计划只在网页里查看（在个人设置的「推送渠道」添加）';
  } else {
    summary = PUSH_STATUS_LABEL[push.status];
    if (push.status === 'sent' && push.sentAt) summary += `（${formatClock(push.sentAt)}）`;
    if (push.status === 'failed' && push.error) summary += `：${push.error}`;
  }

  return (
    <div className="pref-push-line" data-testid="push-status" data-channel="personal">
      <p className="pref-flush">个人渠道推送：{summary}</p>
      {channels.length > 0 ? (
        <ul className="pref-push-channels" aria-label="各渠道推送结果" data-testid="push-channels">
          {channels.map((item) => (
            <li key={item.channel} data-success={item.success ? 'true' : 'false'}>
              {CHANNEL_TYPE_LABEL[item.channel] ?? item.channel}：{item.success ? '已送达' : '发送失败'}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
};
