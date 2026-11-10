import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { usePersonalChannels } from '../../../hooks/usePersonalChannels';
import {
  channelsResponse,
  emailChannel,
  feishuChannel,
  primaryChannelsResponse,
} from '../../../test-utils/personalChannelFixtures';
import { PersonalChannelsSection } from '../personal/PersonalChannelsSection';

const api = vi.hoisted(() => ({
  listNotificationChannels: vi.fn(),
  createNotificationChannel: vi.fn(),
  updateNotificationChannel: vi.fn(),
  deleteNotificationChannel: vi.fn(),
  testNotificationChannel: vi.fn(),
}));

vi.mock('../../../api/me', () => ({ meApi: api }));

const rejection = (status: number, data: unknown) => ({ response: { status, data } });

const Harness = () => {
  const channels = usePersonalChannels();
  return <PersonalChannelsSection channels={channels} title="推送渠道" description="渠道说明" />;
};

const LocationProbe = () => <p data-testid="location">{useLocation().pathname}</p>;

function renderSection() {
  return render(
    <MemoryRouter initialEntries={['/settings/personal']}>
      <UiLanguageProvider>
        <Routes>
          <Route path="*" element={<><Harness /><LocationProbe /></>} />
        </Routes>
      </UiLanguageProvider>
    </MemoryRouter>,
  );
}

async function openAddDialog() {
  fireEvent.click(await screen.findByRole('button', { name: '添加渠道' }));
  return screen.findByRole('dialog');
}

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem('dsa.uiLanguage', 'zh');
  vi.clearAllMocks();
  api.listNotificationChannels.mockResolvedValue(channelsResponse());
});

describe('PersonalChannelsSection', () => {
  it('lists the channels with their type, state and masked config', async () => {
    renderSection();

    const feishu = await screen.findByTestId('channel-1');
    expect(within(feishu).getByText('我的飞书群')).toBeInTheDocument();
    expect(within(feishu).getByText('飞书机器人')).toBeInTheDocument();
    expect(within(feishu).getByText('已启用')).toBeInTheDocument();
    // Secrets are only ever the mask; non-secret fields are shown as stored.
    const config = within(feishu).getByTestId('channel-config-1');
    expect(config).toHaveTextContent('Webhook 地址');
    expect(config).toHaveTextContent('******');
    expect(config).toHaveTextContent('计划');

    // The server names an unnamed channel after its type: shown once, in the UI language.
    const email = screen.getByTestId('channel-2');
    expect(within(email).getByText('邮件（自己的 SMTP 账号）')).toBeInTheDocument();
    expect(email).toHaveTextContent('smtp.example.com');
    expect(email).toHaveTextContent('alice@example.com, bob@example.com');
    expect(screen.getByTestId('channel-count')).toHaveTextContent('已添加 2 / 10 个渠道');
  });

  it('shows an empty state that says results stay on the web', async () => {
    api.listNotificationChannels.mockResolvedValue(channelsResponse({ channels: [] }));
    renderSection();

    expect(await screen.findByText(/还没有推送渠道/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '添加渠道' })).toBeEnabled();
  });

  it('offers a retry when the list cannot be loaded', async () => {
    api.listNotificationChannels.mockRejectedValueOnce(rejection(500, { error: 'internal_error', message: 'boom' }));
    renderSection();

    const retry = await screen.findByRole('button', { name: '重试' });
    fireEvent.click(retry);

    expect(await screen.findByTestId('channel-1')).toBeInTheDocument();
    expect(api.listNotificationChannels).toHaveBeenCalledTimes(2);
  });

  describe('add', () => {
    it('shows the fields of the chosen type and creates the channel with snake_case config', async () => {
      const created = feishuChannel({ id: 3, name: '群 B', config: { webhook_url: '******', secret: '', keyword: '' } });
      api.createNotificationChannel.mockResolvedValue(created);
      renderSection();
      const dialog = await openAddDialog();

      fireEvent.change(within(dialog).getByLabelText('渠道类型'), { target: { value: 'feishu' } });
      expect(within(dialog).getByLabelText(/Webhook 地址/)).toBeInTheDocument();
      expect(within(dialog).getByLabelText(/签名密钥/)).toBeInTheDocument();
      expect(within(dialog).getByLabelText(/关键词/)).toBeInTheDocument();
      expect(within(dialog).queryByLabelText(/Bot Token/)).toBeNull();

      fireEvent.change(within(dialog).getByLabelText(/名称/), { target: { value: ' 群 B ' } });
      fireEvent.change(within(dialog).getByLabelText(/Webhook 地址/), {
        target: { value: 'https://open.feishu.cn/hook/abc' },
      });
      fireEvent.click(within(dialog).getByRole('button', { name: '保存渠道' }));

      await waitFor(() => expect(api.createNotificationChannel).toHaveBeenCalledTimes(1));
      expect(api.createNotificationChannel).toHaveBeenCalledWith({
        channelType: 'feishu',
        name: '群 B',
        enabled: true,
        config: { webhook_url: 'https://open.feishu.cn/hook/abc' },
      });
      expect(await screen.findByText('已保存渠道：群 B')).toBeInTheDocument();
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.getByTestId('channel-3')).toBeInTheDocument();
      // The response only carries the mask, so no plaintext address is ever rendered.
      expect(screen.queryByText(/open\.feishu\.cn/)).toBeNull();
    });

    it.each([
      ['telegram', ['Bot Token', 'Chat ID', '话题 ID'], 'Webhook 地址'],
      ['email', ['SMTP 服务器', 'SMTP 端口', '密码 / 授权码', '发件人邮箱', '收件人邮箱'], 'Webhook 地址'],
      ['custom', ['Webhook 地址', 'Bearer Token', '请求体模板'], 'Bot Token'],
      ['pushplus', ['Token', '群组编码'], 'Chat ID'],
      ['serverchan3', ['SendKey'], 'Token'],
    ])('renders the fields of %s only', async (type, labels, absent) => {
      renderSection();
      const dialog = await openAddDialog();

      fireEvent.change(within(dialog).getByLabelText('渠道类型'), { target: { value: type } });

      for (const label of labels) {
        expect(within(dialog).getByLabelText(new RegExp(label))).toBeInTheDocument();
      }
      expect(within(dialog).queryByLabelText(new RegExp(absent))).toBeNull();
    });

    it('keeps secret fields masked as password inputs', async () => {
      renderSection();
      const dialog = await openAddDialog();
      fireEvent.change(within(dialog).getByLabelText('渠道类型'), { target: { value: 'email' } });

      expect((within(dialog).getByLabelText(/密码 \/ 授权码/) as HTMLInputElement).type).toBe('password');
      expect((within(dialog).getByLabelText(/SMTP 服务器/) as HTMLInputElement).type).toBe('text');
    });

    it('requires the required fields before sending anything', async () => {
      renderSection();
      const dialog = await openAddDialog();
      fireEvent.change(within(dialog).getByLabelText('渠道类型'), { target: { value: 'telegram' } });

      fireEvent.click(within(dialog).getByRole('button', { name: '保存渠道' }));

      expect(await within(dialog).findAllByText('此项必填。')).toHaveLength(2);
      expect(api.createNotificationChannel).not.toHaveBeenCalled();
    });

    it('disables adding once the per-user limit is reached', async () => {
      api.listNotificationChannels.mockResolvedValue(channelsResponse({ maxChannels: 2 }));
      renderSection();

      expect(await screen.findByRole('button', { name: '添加渠道' })).toBeDisabled();
      expect(screen.getByTestId('channel-count')).toHaveTextContent('渠道数量已达上限');
    });
  });

  describe('inline validation and error codes', () => {
    async function submitWith(error: unknown) {
      api.createNotificationChannel.mockRejectedValue(error);
      renderSection();
      const dialog = await openAddDialog();
      fireEvent.change(within(dialog).getByLabelText('渠道类型'), { target: { value: 'feishu' } });
      fireEvent.change(within(dialog).getByLabelText(/Webhook 地址/), { target: { value: 'http://10.0.0.1/hook' } });
      fireEvent.click(within(dialog).getByRole('button', { name: '保存渠道' }));
      return dialog;
    }

    it('shows the field problems of invalid_config under the field, in the UI language', async () => {
      const dialog = await submitWith(
        rejection(400, {
          error: 'invalid_config',
          message: 'The channel configuration is not valid',
          detail: { issues: [{ key: 'webhook_url', code: 'invalid_url', message: 'Not an acceptable value', severity: 'error' }] },
        }),
      );

      expect(await within(dialog).findByText('渠道配置不合法')).toBeInTheDocument();
      expect(within(dialog).getByText('地址格式不正确。')).toBeInTheDocument();
      expect(within(dialog).queryByText('Not an acceptable value')).toBeNull();
      // Nothing was stored: the draft is still there to fix.
      expect((within(dialog).getByLabelText(/Webhook 地址/) as HTMLInputElement).value).toBe('http://10.0.0.1/hook');
    });

    it('explains unsafe_target and marks the offending field', async () => {
      const dialog = await submitWith(
        rejection(400, {
          error: 'unsafe_target',
          message: 'The channel target is not allowed',
          detail: { issues: [{ key: 'webhook_url', code: 'private_address', message: 'Not an acceptable value', severity: 'error' }] },
        }),
      );

      expect(await within(dialog).findByText('目标地址不被允许')).toBeInTheDocument();
      expect(within(dialog).getByText(/只允许公网 https 地址/)).toBeInTheDocument();
      expect(within(dialog).getByText('不能是内网、本机或链路本地地址。')).toBeInTheDocument();
    });

    it.each([
      ['channel_limit_reached', 400, '渠道数量已达上限', /请先删除不用的渠道/],
      ['secret_store_unavailable', 503, '暂时无法保存密钥', /密钥存储暂不可用/],
      ['invalid_channel_type', 400, '不支持的渠道类型', /页面版本过旧/],
      ['primary_user_uses_env', 403, '主用户请到系统设置修改', /不能在个人设置里写入/],
    ])('maps %s to its own message', async (code, status, title, text) => {
      const dialog = await submitWith(rejection(status, { error: code, message: 'raw server text' }));

      expect(await within(dialog).findByText(title)).toBeInTheDocument();
      expect(within(dialog).getByText(text)).toBeInTheDocument();
      if (code !== 'primary_user_uses_env') {
        // The channel codes never surface the server wording; the shared alert keeps it in its collapsed details.
        expect(within(dialog).queryByText('raw server text')).toBeNull();
      }
    });

    it('refreshes the list when the channel to edit has disappeared (channel_not_found)', async () => {
      api.updateNotificationChannel.mockRejectedValue(rejection(404, { error: 'channel_not_found', message: 'Push channel not found' }));
      renderSection();
      fireEvent.click(await screen.findByRole('button', { name: '编辑渠道：我的飞书群' }));
      const dialog = await screen.findByRole('dialog');
      fireEvent.change(within(dialog).getByLabelText(/关键词/), { target: { value: '新词' } });

      fireEvent.click(within(dialog).getByRole('button', { name: '保存渠道' }));

      expect(await within(dialog).findByText('渠道不存在')).toBeInTheDocument();
      await waitFor(() => expect(api.listNotificationChannels).toHaveBeenCalledTimes(2));
    });
  });

  describe('edit', () => {
    async function openEdit(name: string) {
      fireEvent.click(await screen.findByRole('button', { name: `编辑渠道：${name}` }));
      return screen.findByRole('dialog');
    }

    it('shows secrets as the mask and sends only what changed (an untouched secret keeps its stored value)', async () => {
      api.updateNotificationChannel.mockResolvedValue(feishuChannel({ config: { webhook_url: '******', secret: '******', keyword: '新词' } }));
      renderSection();
      const dialog = await openEdit('我的飞书群');

      expect(within(dialog).getByTestId('channel-form-type')).toHaveTextContent('飞书机器人');
      expect((within(dialog).getByLabelText(/Webhook 地址/) as HTMLInputElement).value).toBe('******');
      expect((within(dialog).getByLabelText(/签名密钥/) as HTMLInputElement).value).toBe('******');
      expect(within(dialog).getAllByText(/留空或保持 \*{6} 表示保持当前值/).length).toBeGreaterThan(0);
      // Nothing changed yet: nothing to save.
      expect(within(dialog).getByRole('button', { name: '保存渠道' })).toBeDisabled();

      fireEvent.change(within(dialog).getByLabelText(/关键词/), { target: { value: '新词' } });
      fireEvent.click(within(dialog).getByRole('button', { name: '保存渠道' }));

      await waitFor(() => expect(api.updateNotificationChannel).toHaveBeenCalledTimes(1));
      expect(api.updateNotificationChannel).toHaveBeenCalledWith(1, { config: { keyword: '新词' } });
    });

    it('treats an emptied secret as "keep the stored value" and sends nothing for it', async () => {
      renderSection();
      const dialog = await openEdit('我的飞书群');

      fireEvent.change(within(dialog).getByLabelText(/Webhook 地址/), { target: { value: '' } });

      expect(within(dialog).getByRole('button', { name: '保存渠道' })).toBeDisabled();
      expect(api.updateNotificationChannel).not.toHaveBeenCalled();
    });

    it('sends a newly typed secret and an empty string to clear an optional field', async () => {
      api.updateNotificationChannel.mockResolvedValue(feishuChannel());
      renderSection();
      const dialog = await openEdit('我的飞书群');

      fireEvent.change(within(dialog).getByLabelText(/Webhook 地址/), {
        target: { value: 'https://open.feishu.cn/hook/new' },
      });
      fireEvent.change(within(dialog).getByLabelText(/关键词/), { target: { value: '' } });
      fireEvent.change(within(dialog).getByLabelText(/名称/), { target: { value: '换个名字' } });
      fireEvent.click(within(dialog).getByRole('button', { name: '保存渠道' }));

      await waitFor(() => expect(api.updateNotificationChannel).toHaveBeenCalledTimes(1));
      expect(api.updateNotificationChannel).toHaveBeenCalledWith(1, {
        name: '换个名字',
        config: { webhook_url: 'https://open.feishu.cn/hook/new', keyword: '' },
      });
    });

    it('edits the recipient list of an email channel as comma separated text', async () => {
      api.updateNotificationChannel.mockResolvedValue(emailChannel());
      renderSection();
      const dialog = await openEdit('邮件（自己的 SMTP 账号）');

      const receivers = within(dialog).getByLabelText(/收件人邮箱/) as HTMLInputElement;
      expect(receivers.value).toBe('alice@example.com, bob@example.com');
      fireEvent.change(receivers, { target: { value: 'alice@example.com' } });
      fireEvent.click(within(dialog).getByRole('button', { name: '保存渠道' }));

      await waitFor(() => expect(api.updateNotificationChannel).toHaveBeenCalledWith(2, { config: { receivers: 'alice@example.com' } }));
    });

    it('asks to enter the config again when the stored secrets cannot be read', async () => {
      api.listNotificationChannels.mockResolvedValue(
        channelsResponse({ channels: [feishuChannel({ readable: false, config: {} })] }),
      );
      renderSection();
      const row = await screen.findByTestId('channel-1');

      expect(within(row).getAllByText('已保存的配置无法读取，请编辑并重新填写。').length).toBeGreaterThan(0);
      expect(within(row).getByRole('button', { name: /发送测试消息/ })).toBeDisabled();

      fireEvent.click(within(row).getByRole('button', { name: /编辑渠道/ }));
      const dialog = await screen.findByRole('dialog');
      // The required address has to be entered again.
      fireEvent.change(within(dialog).getByLabelText(/名称/), { target: { value: '重填' } });
      fireEvent.click(within(dialog).getByRole('button', { name: '保存渠道' }));
      expect(await within(dialog).findByText('此项必填。')).toBeInTheDocument();
      expect(api.updateNotificationChannel).not.toHaveBeenCalled();
    });

    it('toggles a channel on and off without opening the dialog', async () => {
      api.updateNotificationChannel.mockResolvedValue(feishuChannel({ enabled: false }));
      renderSection();

      fireEvent.click(await screen.findByRole('switch', { name: '启用渠道：我的飞书群' }));

      await waitFor(() => expect(api.updateNotificationChannel).toHaveBeenCalledWith(1, { enabled: false }));
      await waitFor(() => expect(within(screen.getByTestId('channel-1')).getByText('未启用')).toBeInTheDocument());
    });
  });

  describe('delete', () => {
    it('asks for confirmation, then removes the channel and shows the remaining list', async () => {
      api.deleteNotificationChannel.mockResolvedValue(channelsResponse({ channels: [emailChannel()] }));
      renderSection();

      fireEvent.click(await screen.findByRole('button', { name: '删除渠道：我的飞书群' }));
      expect(screen.getByText('删除推送渠道')).toBeInTheDocument();
      expect(api.deleteNotificationChannel).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole('button', { name: '删除' }));

      await waitFor(() => expect(api.deleteNotificationChannel).toHaveBeenCalledWith(1));
      expect(await screen.findByText('已删除渠道：我的飞书群')).toBeInTheDocument();
      expect(screen.queryByTestId('channel-1')).toBeNull();
      expect(screen.getByTestId('channel-2')).toBeInTheDocument();
    });

    it('keeps the channel when the confirmation is cancelled', async () => {
      renderSection();

      fireEvent.click(await screen.findByRole('button', { name: '删除渠道：我的飞书群' }));
      fireEvent.click(screen.getByRole('button', { name: '取消' }));

      expect(api.deleteNotificationChannel).not.toHaveBeenCalled();
      expect(screen.getByTestId('channel-1')).toBeInTheDocument();
    });
  });

  describe('test message', () => {
    async function clickTest() {
      fireEvent.click(await screen.findByRole('button', { name: '发送测试消息：我的飞书群' }));
    }

    it('reports a delivered message without any timing detail', async () => {
      // The server no longer returns latency (it could tell open from closed internal ports).
      api.testNotificationChannel.mockResolvedValue({ success: true, status: 'sent', errorCategory: null });
      renderSection();
      await clickTest();

      expect(await screen.findByText('测试消息已发送，请到渠道里确认收到。')).toBeInTheDocument();
      expect(screen.queryByText(/ms）/)).toBeNull();
      expect(api.testNotificationChannel).toHaveBeenCalledWith(1);
    });

    it.each([
      ['failed', 'send_failed', /渠道没有接受这条消息/],
      ['blocked', 'target_not_allowed', /只能是公网 https 地址/],
      ['failed', 'unreadable', /配置无法读取，请编辑并重新填写后再测试/],
      ['failed', 'auth_failed', /认证失败/],
      ['failed', 'unreachable', /无法连接到渠道地址/],
      ['failed', 'timeout', /没有在限定时间内响应/],
      ['failed', 'bad_response', /返回的结果不可用/],
      ['failed', 'something_new', /测试消息未能送达/],
    ])('shows only the generic %s / %s category', async (status, category, text) => {
      api.testNotificationChannel.mockResolvedValue({ success: false, status, errorCategory: category, latencyMs: null });
      renderSection();
      await clickTest();

      const result = await screen.findByTestId('channel-test-1');
      expect(await within(result).findByText('测试消息未送达')).toBeInTheDocument();
      expect(within(result).getByText(text)).toBeInTheDocument();
    });

    it('maps the 429 rate limit to a wait-and-retry message', async () => {
      api.testNotificationChannel.mockRejectedValue(
        rejection(429, { error: 'rate_limited', message: 'Wait a few seconds before testing this channel again' }),
      );
      renderSection();
      await clickTest();

      const result = await screen.findByTestId('channel-test-1');
      expect(await within(result).findByText('操作太频繁')).toBeInTheDocument();
      expect(within(result).getByText(/每隔几秒只能发送一次/)).toBeInTheDocument();
    });

    it('shows a busy state while the message is on its way', async () => {
      let finish: (value: unknown) => void = () => undefined;
      api.testNotificationChannel.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
      renderSection();
      await clickTest();

      const button = await screen.findByRole('button', { name: '发送测试消息：我的飞书群' });
      expect(button).toBeDisabled();
      expect(within(screen.getByTestId('channel-test-1')).getByText('发送中...')).toBeInTheDocument();
      finish({ success: true, status: 'sent', errorCategory: null, latencyMs: 5 });
      expect(await screen.findByText(/测试消息已发送/)).toBeInTheDocument();
    });
  });

  describe('primary user', () => {
    beforeEach(() => {
      api.listNotificationChannels.mockResolvedValue(primaryChannelsResponse());
    });

    it('lists the .env channel types read-only and points at the system settings', async () => {
      renderSection();

      expect(await screen.findByText('你是主用户，推送渠道来自系统设置')).toBeInTheDocument();
      const feishu = screen.getByTestId('channel-env-feishu');
      expect(within(feishu).getByText('飞书')).toBeInTheDocument();
      expect(within(feishu).getByText('系统配置')).toBeInTheDocument();
      expect(screen.getByTestId('channel-env-wechat')).toBeInTheDocument();
      // No write affordance of any kind, and nothing secret to show.
      expect(screen.queryByRole('button', { name: '添加渠道' })).toBeNull();
      expect(screen.queryByRole('button', { name: /编辑渠道|删除渠道|发送测试消息/ })).toBeNull();
      expect(screen.queryByRole('switch')).toBeNull();
      expect(screen.queryByTestId('channel-count')).toBeNull();
      expect(screen.queryByTestId('channel-config-null')).toBeNull();

      fireEvent.click(screen.getByRole('button', { name: '前往系统设置' }));
      expect(screen.getByTestId('location')).toHaveTextContent('/settings');
    });

    it('says so when the system has no channel configured', async () => {
      api.listNotificationChannels.mockResolvedValue(primaryChannelsResponse([]));
      renderSection();

      expect(await screen.findByText('系统设置里还没有配置推送渠道。')).toBeInTheDocument();
    });
  });

  it('renders in English too', async () => {
    window.localStorage.setItem('dsa.uiLanguage', 'en');
    renderSection();

    const feishu = await screen.findByTestId('channel-1');
    // A name the user chose is shown as typed; only the default (type) name follows the UI language.
    expect(within(feishu).getByText('我的飞书群')).toBeInTheDocument();
    expect(within(feishu).getByText('Feishu bot')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add channel' })).toBeInTheDocument();
    expect(screen.getByTestId('channel-count')).toHaveTextContent('2 of 10 channels added');
    expect(within(screen.getByTestId('channel-2')).getByText('Email (your own SMTP account)')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Add channel' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Channel type'), { target: { value: 'telegram' } });
    expect(within(dialog).getByLabelText(/Bot token/)).toBeInTheDocument();
  });
});
