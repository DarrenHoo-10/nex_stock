import { forwardRef } from 'react';
import { PlanIcon } from '../PlanIcon';
import { Stagger } from '../motion';
import type { BoardDetail } from '../../../types/boards';
import { ExcessChart } from './ExcessChart';
import {
  BOARD_STATE_LABEL,
  BOARD_TAG_LABEL,
  BOARD_TYPE_LABEL,
  BOARD_WARN_TAGS,
  DRIVER_KIND_LABEL,
  REPRESENTATIVE_ROLE_LABEL,
  formatPct,
  metricTone,
  signClass,
} from './boardMeta';

export interface BoardDetailPanelProps {
  detail: BoardDetail;
  tradeDate: string;
  benchmark: string;
  onBack: () => void;
  onPick: (boardId: string) => void;
  onAsk: () => void;
  followed: boolean;
  followPending: boolean;
  onToggleFollow: () => void;
}

/** Detail window of a board: verdict, six metrics, excess-return chart, representatives and the reasons. */
export const BoardDetailPanel = forwardRef<HTMLHeadingElement, BoardDetailPanelProps>(
  ({ detail, tradeDate, benchmark, onBack, onPick, onAsk, followed, followPending, onToggleFollow }, headingRef) => {
    const stateLabel = BOARD_STATE_LABEL[detail.state];
    const quiet = detail.state === 'ebbing' || detail.state === 'abnormal_drop';
    const unknownDriver = detail.driverKind === 'unknown' || detail.drivers.startsWith('原因未明');
    return (
      <>
        <button type="button" className="btn quiet back" onClick={onBack}>
          ‹ 返回列表
        </button>
        <Stagger itemClassName="detail-item">
          <header className="d-head">
            <div className="id">
              <h2 ref={headingRef} tabIndex={-1}>
                {detail.name}
              </h2>
              <span className="tag line">{BOARD_TYPE_LABEL[detail.boardType]}</span>
              {detail.tags.map((tag) => (
                <span key={tag} className={`tag${BOARD_WARN_TAGS.has(tag) ? ' warn' : ''}`}>
                  {BOARD_TAG_LABEL[tag]}
                </span>
              ))}
            </div>
            <button
              type="button"
              className={`btn sm follow-btn${followed ? ' on' : ''}`}
              aria-pressed={followed}
              disabled={followPending}
              onClick={onToggleFollow}
            >
              <span aria-hidden="true">{followed ? '★' : '☆'}</span>
              {followed ? '已关注' : '关注'}
            </button>
          </header>
          <div className="hero">
            <div>
              <div className={`verdict v-state${quiet ? ' v-skip' : ''}`}>{stateLabel}</div>
              <p className="summary">{detail.drivers}</p>
              <p className="meta">
                <span className={`tag ${unknownDriver ? 'warn' : 'line'}`}>{DRIVER_KIND_LABEL[detail.driverKind]}</span>{' '}
                {tradeDate} 收盘，超额收益相对{benchmark}
              </p>
            </div>
            <dl className="figs three board-figs">
              {detail.metrics.map((metric) => (
                <div key={metric.label}>
                  <dt>{metric.label}</dt>
                  <dd className={`num ${metricTone(metric.value)}`}>{metric.value}</dd>
                  {metric.hint ? <p className="hint">{metric.hint}</p> : null}
                </div>
              ))}
            </dl>
          </div>
          <div className="body single">
            <div className="plan">
              <section aria-labelledby="board-why">
                <h3 id="board-why">为什么判定为{stateLabel}</h3>
                <p className="situ">{detail.verdictText}</p>
                {detail.evidence.length ? (
                  <ul className="risk-list">
                    {detail.evidence.map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                ) : null}
              </section>
              <section aria-labelledby="board-chart">
                <h3 id="board-chart">持续性：最近 {detail.excessSeries.length || 8} 个交易日的超额收益</h3>
                <ExcessChart series={detail.excessSeries} benchmark={benchmark} />
              </section>
              {detail.representatives.length ? (
                <section aria-labelledby="board-reps">
                  <h3 id="board-reps">代表股票</h3>
                  <ul className="reps">
                    {detail.representatives.map((stock) => (
                      <li key={`${stock.code}-${stock.role}`}>
                        <span>
                          <b>{stock.name}</b> <span className="cd">{stock.code}</span>{' '}
                          <span className={`tag ${stock.role === 'leader' ? 'wait' : 'line'}`}>
                            {REPRESENTATIVE_ROLE_LABEL[stock.role]}
                          </span>
                        </span>
                        <span className="num">{typeof stock.close === 'number' ? stock.close.toFixed(2) : '—'}</span>
                        <span className={`num ${signClass(stock.changePct)}`}>{formatPct(stock.changePct)}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}
              {detail.dataGaps.length ? (
                <section aria-label="数据缺口">
                  <p className="notice" role="note">
                    <b>数据缺口</b>
                    <span>{detail.dataGaps.join('；')}</span>
                  </p>
                </section>
              ) : null}
              <div className="actions">
                {detail.canPick ? (
                  <button type="button" className="btn primary" onClick={() => onPick(detail.id)}>
                    在这个板块里选股
                  </button>
                ) : null}
                <button type="button" className="btn" onClick={onAsk}>
                  <PlanIcon name="assist" size={16} />
                  问助手
                </button>
              </div>
              {!detail.canPick ? (
                <p className="note">这个板块暂不支持按板块选股（涨幅主要来自个别股票，或历史数据不足）。</p>
              ) : null}
            </div>
          </div>
        </Stagger>
      </>
    );
  },
);
BoardDetailPanel.displayName = 'BoardDetailPanel';
