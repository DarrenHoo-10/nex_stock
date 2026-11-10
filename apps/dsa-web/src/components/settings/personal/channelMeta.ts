import { Bell, BellRing, Mail, MessageSquare, Radio, Send, Smartphone, Webhook, type LucideIcon } from 'lucide-react';
import { UI_TEXT, type UiTextKey } from '../../../i18n/uiText';
import type { ChannelConfig, ChannelFieldInfo, ChannelTypeInfo, PersonalChannel } from '../../../types/me';

/** The backend masks a configured secret with this token; sending it (or an empty string) keeps the stored value. */
export const CHANNEL_SECRET_MASK = '******';

const TYPE_ICONS: Record<string, LucideIcon> = {
  feishu: MessageSquare,
  dingtalk: BellRing,
  wecom: Smartphone,
  wechat: Smartphone,
  telegram: Send,
  pushplus: Bell,
  serverchan3: Radio,
  email: Mail,
  custom: Webhook,
};

export function channelTypeIcon(channelType: string): LucideIcon {
  return TYPE_ICONS[channelType] ?? Bell;
}

/** `key` when it is a registered UI text key (the catalog from the API can be newer than this bundle). */
export function knownTextKey(key: string): UiTextKey | null {
  return Object.prototype.hasOwnProperty.call(UI_TEXT.zh, key) ? (key as UiTextKey) : null;
}

export function channelTypeLabelKey(channelType: string): UiTextKey | null {
  return knownTextKey(`personalChannels.type.${channelType}`);
}

export function channelFieldLabelKey(fieldName: string): UiTextKey | null {
  return knownTextKey(`personalChannels.field.${fieldName}`);
}

/**
 * Per-field validation codes (`detail.issues[].code`) of the channel config -> UI text. The server sends English
 * placeholders only, so the message shown to the user is always the translated one.
 */
export function channelIssueTextKey(code: string): UiTextKey {
  return knownTextKey(`personalChannels.issue.${code}`) ?? 'personalChannels.issue.fallback';
}

/** Generic delivery categories of a test message -> UI text; anything else is shown as "could not be delivered". */
export function channelTestFailureKey(category: string | null | undefined): UiTextKey {
  return knownTextKey(`personalChannels.test.failure.${category ?? ''}`) ?? 'personalChannels.test.failure.unknown';
}

/** Value of a config field as the text shown in an input (lists are comma separated). */
export function configFieldText(config: ChannelConfig, name: string): string {
  const value = config[name];
  if (Array.isArray(value)) return value.join(', ');
  if (value === null || value === undefined) return '';
  return String(value);
}

export function isSecretSet(config: ChannelConfig, field: ChannelFieldInfo): boolean {
  return field.secret && configFieldText(config, field.name) !== '';
}

export function findChannelType(types: ChannelTypeInfo[], channelType: string): ChannelTypeInfo | undefined {
  return types.find((item) => item.channelType === channelType);
}

/** Longer free text goes in a textarea. */
export function isMultilineField(field: ChannelFieldInfo): boolean {
  return field.kind === 'template';
}

export function isEnabledChannel(channel: PersonalChannel): boolean {
  return channel.enabled && channel.readable;
}
