import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { useBoardFollowStore } from '../../../stores/boardFollowStore';
import BoardsPage from '../BoardsPage';

const api = vi.hoisted(() => ({
  getBoardAnomalies: vi.fn(),
  getBoardDetail: vi.fn(),
  getBoardRecommendations: vi.fn(),
  refreshBoardRecommendations: vi.fn(),
  getBoardFollows: vi.fn(),
  followBoard: vi.fn(),
  unfollowBoard: vi.fn(),
}));

vi.mock('../../../api/boards', () => ({ ...api, isPlanServiceUnavailable: () => false }));
vi.mock('../../../api/dailyPlan', () => ({
  getLatestRun: vi.fn().mockResolvedValue(null),
  isPlanServiceUnavailable: () => false,
}));

function row(id: string, name: string, state: string, boardType = 'concept') {
  return {
    id, name, boardType, state, tags: [], changePct: 2.1, change5dPct: 5.2, excessPct: 1.4, amountRatio: 1.9,
    breadthPct: 72, limitUpCount: 3, brokenRatePct: null, reason: `${name}理由`,
  };
}

const SEMI = row('concept:BK1036', '半导体', 'strengthening');
const ROBOT = row('concept:BK1184', '机器人', 'first_launch');
const COAL = row('industry:BK0437', '煤炭', 'none', 'industry');

function detailOf(board: ReturnType<typeof row>) {
  return {
    ...board, verdictText: '判定', drivers: '原因未明', driverKind: 'unknown', metrics: [], excessSeries: [],
    representatives: [], evidence: [], canPick: true, dataGaps: [],
  };
}

function follows(boards: Array<ReturnType<typeof row>>) {
  return {
    tradeDate: '2026-11-06',
    boards: boards.map((board) => ({ id: board.id, name: board.name, boardType: board.boardType, followedAt: null, today: board })),
  };
}

function renderPage() {
  return render(
    <UiLanguageProvider>
      <MemoryRouter initialEntries={['/boards']}>
        <BoardsPage />
      </MemoryRouter>
    </UiLanguageProvider>,
  );
}

beforeEach(() => {
  Object.values(api).forEach((fn) => fn.mockReset());
  localStorage.setItem('dsa.uiLanguage', 'zh');
  useBoardFollowStore.setState({ status: 'idle', boards: [], tradeDate: null, error: null, pending: new Set() });
  api.getBoardAnomalies.mockResolvedValue({ tradeDate: '2026-11-06', market: { indexName: '中证全指' }, boards: [SEMI, ROBOT], historyNote: null });
  api.getBoardDetail.mockImplementation(async (id: string) => detailOf([SEMI, ROBOT, COAL].find((b) => b.id === id) ?? SEMI));
  api.getBoardRecommendations.mockResolvedValue({
    tradeDate: '2026-11-06', status: 'ready', source: 'model', model: 'm', note: null, generatedAt: '2026-11-06T09:10:00Z',
    items: [{ rank: 1, reason: '首次启动，量能放大', board: ROBOT }],
  });
  api.getBoardFollows.mockResolvedValue(follows([COAL]));
});

describe('BoardsPage focus strip', () => {
  it('shows today’s suggestions and followed boards, and opens a followed board outside the list', async () => {
    renderPage();
    const suggestions = await screen.findByRole('region', { name: '今日建议' });
    expect(await within(suggestions).findByText('首次启动，量能放大')).toBeInTheDocument();
    expect(within(suggestions).getByText(/模型挑选/)).toBeInTheDocument();

    const followed = screen.getByRole('region', { name: '我关注的' });
    fireEvent.click(await within(followed).findByRole('button', { name: /煤炭/ }));
    // 煤炭 has no signal today (not in the list) but its detail is shown.
    await waitFor(() => expect(api.getBoardDetail).toHaveBeenCalledWith(COAL.id));
    expect(await screen.findByRole('heading', { name: '煤炭' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '已关注' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('follows the shown board and marks it in the list', async () => {
    api.followBoard.mockResolvedValue(follows([COAL, SEMI]));
    renderPage();
    expect(await screen.findByRole('heading', { name: '半导体' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '关注' }));
    await waitFor(() => expect(api.followBoard).toHaveBeenCalledWith(SEMI.id));
    expect(await screen.findByRole('button', { name: '已关注' })).toBeInTheDocument();
    const list = screen.getByRole('region', { name: '板块列表' });
    expect(within(list).getByRole('img', { name: '已关注' })).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: '我关注的' })).getByRole('button', { name: /半导体/ })).toBeInTheDocument();
  });

  it('regenerates the suggestions in the background and polls until ready', async () => {
    api.getBoardRecommendations
      .mockResolvedValueOnce({ tradeDate: '2026-11-06', status: 'none', source: null, model: null, note: null, generatedAt: null, items: [] })
      .mockResolvedValue({
        tradeDate: '2026-11-06', status: 'ready', source: 'rule', model: null, note: '模型调用失败（timeout），按规则排序',
        generatedAt: null, items: [{ rank: 1, reason: '量价齐升', board: SEMI }],
      });
    api.refreshBoardRecommendations.mockResolvedValue({
      tradeDate: '2026-11-06', status: 'generating', source: null, model: null, note: null, generatedAt: null, items: [],
    });
    renderPage();
    const suggestions = await screen.findByRole('region', { name: '今日建议' });
    fireEvent.click(await within(suggestions).findByRole('button', { name: '重新生成' }));
    await waitFor(() => expect(api.refreshBoardRecommendations).toHaveBeenCalledTimes(1));
    expect(await within(suggestions).findByText('量价齐升')).toBeInTheDocument();
    expect(within(suggestions).getByText(/按规则排序：模型调用失败/)).toBeInTheDocument();
  });
});
