import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { CurrentUserProvider } from '../../../test-utils/currentUser';
import { adminUser, regularUser } from '../../../test-utils/currentUserFixtures';
import type { AnalysisReport, HistoryItem } from '../../../types/analysis';
import type { CurrentUser } from '../../../types/me';
import { ReportProvenance } from '../../report/ReportProvenance';
import { HistoryList } from '../HistoryList';
import { StockHistoryTrendDrawer } from '../StockHistoryTrendDrawer';

const baseProps = {
  isLoading: false,
  isLoadingMore: false,
  hasMore: false,
  selectedIds: new Set<number>(),
  onItemClick: vi.fn(),
  onLoadMore: vi.fn(),
  onToggleItemSelection: vi.fn(),
  onToggleSelectAll: vi.fn(),
  onDeleteSelected: vi.fn(),
};

const mine: HistoryItem = {
  id: 1,
  queryId: 'q-1',
  stockCode: '600519',
  stockName: '贵州茅台',
  sentimentScore: 82,
  operationAdvice: '买入',
  createdAt: '2026-11-09T08:00:00Z',
  requesterUserId: 'u-alice',
  requesterName: 'Alice',
  isMine: true,
  isPrivate: true,
};

const others: HistoryItem = {
  id: 2,
  queryId: 'q-2',
  stockCode: '000001',
  stockName: '平安银行',
  sentimentScore: 60,
  operationAdvice: '持有',
  createdAt: '2026-11-09T09:00:00Z',
  requesterUserId: 'u-bob',
  requesterName: 'Bob',
  isMine: false,
  isPrivate: false,
};

function renderWithUser(ui: React.ReactElement, user: CurrentUser | null) {
  return render(
    <UiLanguageProvider>
      <CurrentUserProvider user={user}>{ui}</CurrentUserProvider>
    </UiLanguageProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem('dsa.uiLanguage', 'zh');
});

describe('HistoryList attribution and the 只看我的 filter', () => {
  it('shows who requested each report and the 私有 badge on private ones', () => {
    renderWithUser(<HistoryList {...baseProps} items={[mine, others]} />, regularUser);

    const badges = screen.getAllByTestId('requester-badge').map((badge) => badge.textContent);
    expect(badges).toEqual(['我发起', '发起：Bob']);
    // Only the private report carries the 私有 badge.
    expect(screen.getAllByTestId('private-badge')).toHaveLength(1);
    expect(screen.getByTestId('private-badge')).toHaveTextContent('私有');
  });

  it('toggles mine_only through the filter', () => {
    const onMineOnlyChange = vi.fn();
    renderWithUser(
      <HistoryList {...baseProps} items={[others]} mineOnly={false} onMineOnlyChange={onMineOnlyChange} />,
      regularUser,
    );

    fireEvent.click(screen.getByRole('checkbox', { name: '只看我的' }));

    expect(onMineOnlyChange).toHaveBeenCalledWith(true);
  });

  it('keeps the filter visible when nothing matches', () => {
    renderWithUser(
      <HistoryList {...baseProps} items={[]} mineOnly onMineOnlyChange={vi.fn()} />,
      regularUser,
    );

    expect(screen.getByRole('checkbox', { name: '只看我的' })).toBeChecked();
    expect(screen.getByText('暂无历史分析记录')).toBeInTheDocument();
  });

  it('looks exactly as before in local mode: no attribution, no private badge, no filter', () => {
    renderWithUser(
      <HistoryList {...baseProps} items={[mine, others]} onMineOnlyChange={vi.fn()} />,
      null,
    );

    expect(screen.queryByTestId('requester-badges')).toBeNull();
    expect(screen.queryByTestId('private-badge')).toBeNull();
    expect(screen.queryByRole('checkbox', { name: '只看我的' })).toBeNull();
  });
});

describe('StockHistoryTrendDrawer', () => {
  const report = {
    meta: {
      id: 1,
      queryId: 'q-1',
      stockCode: '600519',
      stockName: '贵州茅台',
      reportType: 'detailed',
      createdAt: '2026-11-09T08:00:00Z',
    },
    summary: { analysisSummary: '', operationAdvice: '买入', trendPrediction: '', sentimentScore: 82 },
  } as unknown as AnalysisReport;

  const drawerProps = {
    report,
    items: [mine, others],
    total: 2,
    hasMore: false,
    isLoading: false,
    isLoadingMore: false,
    filters: { range: 'all' as const, model: 'all', sort: 'desc' as const },
    onClose: vi.fn(),
    onRangeChange: vi.fn(),
    onLoadMore: vi.fn(),
    onSelectRecord: vi.fn(),
    onRetry: vi.fn(),
  };

  it('labels each record with its requester and offers 只看我的', () => {
    const onMineOnlyChange = vi.fn();
    renderWithUser(<StockHistoryTrendDrawer {...drawerProps} onMineOnlyChange={onMineOnlyChange} />, regularUser);

    expect(screen.getAllByTestId('requester-badge').map((badge) => badge.textContent)).toEqual(['我发起', '发起：Bob']);
    expect(screen.getAllByTestId('private-badge')).toHaveLength(1);

    fireEvent.click(screen.getByRole('checkbox', { name: '只看我的' }));
    expect(onMineOnlyChange).toHaveBeenCalledWith(true);
  });

  it('reflects an active mine_only filter', () => {
    renderWithUser(
      <StockHistoryTrendDrawer
        {...drawerProps}
        filters={{ range: 'all', model: 'all', sort: 'desc', mineOnly: true }}
        onMineOnlyChange={vi.fn()}
      />,
      regularUser,
    );

    expect(screen.getByRole('checkbox', { name: '只看我的' })).toBeChecked();
  });

  it('is unchanged in local mode', () => {
    renderWithUser(<StockHistoryTrendDrawer {...drawerProps} onMineOnlyChange={vi.fn()} />, null);

    expect(screen.queryByTestId('requester-badges')).toBeNull();
    expect(screen.queryByRole('checkbox', { name: '只看我的' })).toBeNull();
  });
});

describe('ReportProvenance', () => {
  it('shows the requester of someone else\'s report', () => {
    renderWithUser(<ReportProvenance meta={others} />, regularUser);

    expect(screen.getByTestId('requester-badge')).toHaveTextContent('发起：Bob');
  });

  it('shows 复用已有报告 when the analysis returned an existing shared report', () => {
    renderWithUser(<ReportProvenance meta={others} reuse={{ reused: true, reusedReportId: 2 }} />, regularUser);

    expect(screen.getByTestId('reused-report-badge')).toHaveTextContent('复用已有报告');
    expect(screen.queryByTestId('reused-metadata-badge')).toBeNull();
  });

  it('shows 已复用数据 when only the collected inputs were reused', () => {
    renderWithUser(<ReportProvenance meta={mine} reuse={{ reused: false, metadataReusedFrom: 7 }} />, adminUser);

    expect(screen.getByTestId('reused-metadata-badge')).toHaveTextContent('已复用数据');
    expect(screen.queryByTestId('reused-report-badge')).toBeNull();
  });

  it('shows the reuse badges in local mode too, but no attribution', () => {
    renderWithUser(<ReportProvenance meta={mine} reuse={{ reused: false, metadataReusedFrom: 7 }} />, null);

    expect(screen.getByTestId('reused-metadata-badge')).toBeInTheDocument();
    expect(screen.queryByTestId('requester-badges')).toBeNull();
  });

  it('renders nothing when there is nothing to say', () => {
    const { container } = renderWithUser(<ReportProvenance meta={{ ...others, isMine: undefined, isPrivate: undefined }} />, null);

    expect(container).toBeEmptyDOMElement();
  });
});
