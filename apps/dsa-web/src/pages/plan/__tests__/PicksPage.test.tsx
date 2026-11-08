import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { useBoardFollowStore } from '../../../stores/boardFollowStore';
import PicksPage from '../PicksPage';

const api = vi.hoisted(() => ({
  getStatus: vi.fn(),
  getStrategies: vi.fn(),
  listScreenTasks: vi.fn(),
  getScreenTask: vi.fn(),
  startScreen: vi.fn(),
  getBoardAnomalies: vi.fn(),
  getBoardDetail: vi.fn(),
  getBoardRecommendations: vi.fn(),
  getBoardFollows: vi.fn(),
  getWatchlist: vi.fn(),
}));

vi.mock('../../../api/screening', () => ({
  screeningApi: {
    getStatus: api.getStatus,
    getStrategies: api.getStrategies,
    listScreenTasks: api.listScreenTasks,
    getScreenTask: api.getScreenTask,
    startScreen: api.startScreen,
  },
}));
vi.mock('../../../api/boards', () => ({
  getBoardAnomalies: api.getBoardAnomalies,
  getBoardDetail: api.getBoardDetail,
  getBoardRecommendations: api.getBoardRecommendations,
  getBoardFollows: api.getBoardFollows,
  followBoard: vi.fn(),
  unfollowBoard: vi.fn(),
  isPlanServiceUnavailable: () => false,
}));
vi.mock('../../../api/systemConfig', () => ({
  systemConfigApi: { getWatchlist: api.getWatchlist, addToWatchlist: vi.fn(), getConfig: vi.fn(), update: vi.fn() },
}));
vi.mock('../../../api/dailyPlan', () => ({
  getLatestRun: vi.fn().mockResolvedValue(null),
  isPlanServiceUnavailable: () => false,
}));

const STRATEGIES = [
  { id: 'volume_breakout', title: '放量突破', name: 'volume_breakout' },
  { id: 'dual_low', title: '双低', name: 'dual_low' },
];

const SEMI = { id: 'concept:BK1036', name: '半导体', boardType: 'concept', state: 'strengthening', tags: [] };
const BANK = { id: 'industry:BK0475', name: '银行', boardType: 'industry', state: 'none', tags: [] };
const ROBOT = { id: 'concept:BK1184', name: '机器人', boardType: 'concept', state: 'first_launch', tags: [] };
const COAL = { id: 'industry:BK0437', name: '煤炭', boardType: 'industry', state: 'none', tags: [] };

const RUNNING = {
  taskId: 't-run',
  status: 'processing',
  progress: 45,
  message: '正在排序',
  strategy: 'volume_breakout',
  scope: { boards: [], topics: [{ name: '半导体', source: 'concept' }], topicMatch: 'any' },
  createdAt: '2026-11-08T08:00:00Z',
};
const DONE = {
  taskId: 't-done',
  status: 'completed',
  progress: 100,
  strategy: 'dual_low',
  scope: { boards: [], topics: [], topicMatch: 'any' },
  candidateCount: 1,
  createdAt: '2026-11-08T07:00:00Z',
  finishedAt: '2026-11-08T07:05:00Z',
};

const Where: React.FC = () => {
  const location = useLocation();
  return <output data-testid="where">{location.search}</output>;
};

function renderPage(path = '/picks') {
  return render(
    <UiLanguageProvider>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/picks"
            element={
              <>
                <PicksPage />
                <Where />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </UiLanguageProvider>,
  );
}

beforeEach(() => {
  Object.values(api).forEach((fn) => fn.mockReset());
  useBoardFollowStore.setState({ status: 'idle', boards: [], tradeDate: null, error: null, pending: new Set() });
  api.getBoardRecommendations.mockResolvedValue({
    tradeDate: '2026-11-06',
    status: 'ready',
    source: 'model',
    model: 'anthropic/claude-opus-5-5',
    note: null,
    generatedAt: '2026-11-06T09:10:00Z',
    items: [{ rank: 1, reason: '首次启动，量能放大', board: ROBOT }],
  });
  api.getBoardFollows.mockResolvedValue({
    tradeDate: '2026-11-06',
    boards: [{ id: COAL.id, name: '煤炭', boardType: 'industry', followedAt: null, today: COAL }],
  });
  localStorage.setItem('dsa.uiLanguage', 'zh');
  api.getStatus.mockResolvedValue({ enabled: true, available: true });
  api.getStrategies.mockResolvedValue({ strategies: STRATEGIES });
  api.getWatchlist.mockResolvedValue([]);
  api.listScreenTasks.mockResolvedValue({ tasks: [RUNNING, DONE], total: 2 });
  api.getBoardAnomalies.mockImplementation(async (query: { state?: string } = {}) => ({
    tradeDate: '2026-11-06',
    market: {},
    boards: query.state === 'none' ? [BANK, COAL] : [SEMI, ROBOT],
    historyNote: null,
  }));
});

describe('PicksPage', () => {
  it('lists persisted tasks with their status and opens one for its picks', async () => {
    api.getScreenTask.mockResolvedValue({
      ...DONE,
      result: {
        enabled: true,
        candidateCount: 1,
        candidates: [{ rank: 1, code: '000988', name: '华工科技', reason: '放量', raw: {} }],
      },
    });
    renderPage();

    const list = await screen.findByRole('list', { name: '选股任务列表' });
    const rows = within(list).getAllByRole('button');
    expect(rows[0]).toHaveTextContent('放量突破');
    expect(rows[0]).toHaveTextContent('半导体');
    expect(rows[0]).toHaveTextContent('进行中 45%');
    expect(rows[1]).toHaveTextContent('双低');
    expect(rows[1]).toHaveTextContent('全市场');
    expect(rows[1]).toHaveTextContent('完成 · 1 只');
    // Results are only loaded after opening a task.
    expect(api.getScreenTask).not.toHaveBeenCalled();

    fireEvent.click(rows[1]);
    expect(await screen.findByText('华工科技')).toBeInTheDocument();
    expect(api.getScreenTask).toHaveBeenCalledWith('t-done');
    expect(screen.getByTestId('where')).toHaveTextContent('task=t-done');
    expect(screen.getByTestId('task-head')).toHaveTextContent('策略 双低，范围 全市场');

    fireEvent.click(screen.getByRole('button', { name: '← 全部任务' }));
    expect(await screen.findByRole('list', { name: '选股任务列表' })).toBeInTheDocument();
  });

  it('starts a task scoped to the chosen boards and refreshes the list', async () => {
    api.startScreen.mockResolvedValue({ taskId: 't-new', status: 'pending', message: '已提交' });
    renderPage();
    await screen.findByRole('list', { name: '选股任务列表' });

    fireEvent.click(screen.getByRole('button', { name: '选择板块' }));
    const signalled = await screen.findByRole('list', { name: '今日异动' });
    // The recommended (机器人) and followed (煤炭) boards get their own sections and are not repeated below.
    expect(within(screen.getByRole('list', { name: '模型推荐' })).getByRole('button', { name: /机器人/ })).toHaveTextContent(
      '首次启动，量能放大',
    );
    expect(within(screen.getByRole('list', { name: '我关注的' })).getByRole('button', { name: /煤炭/ })).toBeInTheDocument();
    expect(within(signalled).queryByRole('button', { name: /机器人/ })).toBeNull();
    fireEvent.click(within(signalled).getByRole('button', { name: /半导体/ }));
    fireEvent.change(screen.getByLabelText('搜索板块'), { target: { value: '银行' } });
    fireEvent.click(within(screen.getByRole('list', { name: '搜索结果' })).getByRole('button', { name: /银行/ }));
    expect(screen.getByRole('button', { name: '移除 半导体' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '移除 银行' })).toBeInTheDocument();

    const before = api.listScreenTasks.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: '开始选股' }));
    await waitFor(() => expect(api.startScreen).toHaveBeenCalledTimes(1));
    expect(api.startScreen).toHaveBeenCalledWith({
      market: 'cn',
      strategy: 'volume_breakout',
      maxResults: 10,
      scope: {
        boards: [],
        topics: [
          { name: '半导体', source: 'concept' },
          { name: '银行', source: 'industry' },
        ],
        topicMatch: 'any',
      },
    });
    await waitFor(() => expect(api.listScreenTasks.mock.calls.length).toBeGreaterThan(before));
  });

  it('adds the recommended boards with one click', async () => {
    renderPage();
    await screen.findByRole('list', { name: '选股任务列表' });
    fireEvent.click(screen.getByRole('button', { name: '选择板块' }));
    fireEvent.click(await screen.findByRole('button', { name: '选上推荐板块' }));
    expect(screen.getByRole('button', { name: '移除 机器人' })).toBeInTheDocument();
  });

  it('shows progress for a running task and the reason for an interrupted one', async () => {
    api.getScreenTask.mockResolvedValueOnce(RUNNING);
    const { unmount } = renderPage('/picks?task=t-run');
    expect(await screen.findByTestId('pick-progress')).toHaveTextContent('正在排序');
    unmount();

    api.getScreenTask.mockResolvedValueOnce({ ...RUNNING, status: 'interrupted', error: '服务重启，任务没有完成，请重新发起' });
    renderPage('/picks?task=t-run');
    expect(await screen.findByText('选股任务被中断')).toBeInTheDocument();
    expect(screen.getByText('服务重启，任务没有完成，请重新发起')).toBeInTheDocument();
  });

  it('preselects the board passed from the boards page', async () => {
    api.getBoardDetail.mockResolvedValue({ id: SEMI.id, name: '半导体', boardType: 'concept', canPick: true });
    renderPage('/picks?board=concept:BK1036');
    expect(await screen.findByRole('button', { name: '移除 半导体' })).toBeInTheDocument();
  });
});
