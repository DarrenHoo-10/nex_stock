import { createServer, type Server } from 'node:http';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import apiClient from '../src/api';
import { FeishuSettings } from '../src/components/plan/preferences/FeishuSettings';
import { buildConfigView } from '../src/components/plan/preferences/usePreferencesConfig';

let server: Server;
let endpoint = '';
let status = 200;
let state: Record<string, unknown>;
let writes: string[];
const originalBase = apiClient.defaults.baseURL;
const path = '/api/v1/finance/assistant/feishu-settings';
const view = buildConfigView([], 'synthetic-v1');

beforeAll(async () => {
  server = createServer((req, res) => {
    res.setHeader('Access-Control-Allow-Origin', req.headers.origin || 'http://localhost:3000');
    res.setHeader('Access-Control-Allow-Credentials', 'true');
    res.setHeader('Access-Control-Allow-Headers', 'content-type');
    if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }
    res.setHeader('Content-Type', 'application/json');
    if (req.method !== 'GET') writes.push(req.url || '');
    if (req.url === path) { res.writeHead(status); res.end(JSON.stringify(state)); return; }
    if (req.url === '/api/v1/system/feishu/register' && req.method === 'POST') {
      res.end(JSON.stringify({ session_id: 'synthetic', verification_url: 'https://open.feishu.cn/synthetic', interval: 1 })); return;
    }
    res.end(JSON.stringify({ status: 'pending', interval: 1 }));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Missing fixture listener');
  endpoint = `http://127.0.0.1:${address.port}`;
  apiClient.defaults.baseURL = endpoint;
});
afterAll(async () => {
  apiClient.defaults.baseURL = originalBase;
  await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
});
beforeEach(() => {
  status = 200; writes = [];
  state = { managed: true, owner: 'core', version: 3, appliesToCurrentApp: true,
    currentAppId: 'selected-app', appId: 'selected-app', settingsUrl: 'https://nexbot.example.test/?settings=feishu' };
});
afterEach(cleanup);

function mount() {
  return render(<FeishuSettings config={view} onSave={vi.fn().mockResolvedValue({ ok: true })} onReload={vi.fn()} />);
}
describe('Feishu management through real loopback HTTP and the production API client', () => {
  it.each(['core', 'paused'])('shows %s ownership and only the trusted NexBot entry', async (owner) => {
    state.owner = owner;
    mount();
    expect(await screen.findByTestId('feishu-ownership')).toHaveTextContent(owner === 'core' ? 'NexBot 承接' : '已暂停');
    expect(screen.getByRole('link', { name: '打开 NexBot 飞书设置' })).toHaveAttribute('href', state.settingsUrl);
    expect(screen.queryByTestId('feishu-scan-start')).not.toBeInTheDocument();
    expect(screen.queryByText('手动配置（已有机器人 / 群 Webhook）')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '保存飞书设置' })).not.toBeInTheDocument();
    expect(writes).toEqual([]);
  });
  it.each(['stock', 'unrelated', 'legacy'])('preserves the original local controls for %s', async (kind) => {
    if (kind === 'stock') state.owner = 'stock';
    if (kind === 'unrelated') { state.currentAppId = 'other-app'; state.appliesToCurrentApp = false; }
    if (kind === 'legacy') { status = 404; state = { error: 'FINANCE_RESOURCE_NOT_FOUND' }; }
    mount();
    expect(await screen.findByTestId('feishu-scan-start')).toBeEnabled();
    expect(screen.getByTestId('feishu-ownership')).toHaveTextContent('Stock 本地管理');
    if (kind === 'unrelated') expect(screen.getByTestId('feishu-ownership')).toHaveTextContent('另一个飞书应用');
  });
  it.each([null, 'javascript:alert(1)', 'https://nexbot.example.test/?token=synthetic'])('uses a navigation hint for missing or unsafe URLs: %s', async (url) => {
    state.settingsUrl = url;
    mount();
    expect(await screen.findByText(/尚未提供网页入口/)).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
  it('fails closed for unavailable or malformed current state instead of exposing legacy writes', async () => {
    status = 503; state = { message: 'Synthetic ownership unavailable' };
    mount();
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByTestId('feishu-scan-start')).not.toBeInTheDocument();
    status = 200; state = {};
    fireEvent.click(screen.getByText('重新读取'));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('飞书管理状态响应无效'));
    expect(writes).toEqual([]);
  });
  it('rechecks ownership before scanning and removes stale local controls after handoff', async () => {
    state.owner = 'stock';
    mount();
    const scan = await screen.findByTestId('feishu-scan-start');
    state.owner = 'core'; state.version = 4;
    fireEvent.click(scan);
    await waitFor(() => expect(screen.getByTestId('feishu-ownership')).toHaveTextContent('NexBot 承接'));
    expect(screen.queryByTestId('feishu-scan-start')).not.toBeInTheDocument();
    expect(writes).toEqual([]);
  });
  it('unmounts an existing QR flow when focus refresh discovers a pause', async () => {
    state.owner = 'stock';
    mount();
    fireEvent.click(await screen.findByTestId('feishu-scan-start'));
    expect(await screen.findByTestId('feishu-qr')).toBeInTheDocument();
    expect(writes).toEqual(['/api/v1/system/feishu/register']);
    state.owner = 'paused';
    await act(async () => window.dispatchEvent(new Event('focus')));
    await waitFor(() => expect(screen.queryByTestId('feishu-qr')).not.toBeInTheDocument());
    expect(screen.getByTestId('feishu-ownership')).toHaveTextContent('已暂停');
  });
});
