import type React from 'react';
import { StaggerItem } from '../motion';
import { PlanIcon } from '../PlanIcon';
import { formatPct, signClass } from '../boards/boardMeta';
import { HORIZON_LABEL, HORIZON_SPAN } from './pickMeta';
import type { PickView } from './pickMeta';

export interface PickCardProps {
  pick: PickView;
  index: number;
  inWatchlist: boolean;
  adding: boolean;
  onAdd: (pick: PickView) => void;
  onAsk: (pick: PickView) => void;
}

const Dash: React.FC = () => <span className="dim">—</span>;

/** One candidate: identity and horizon tags, the condition lines and the actions. */
export const PickCard: React.FC<PickCardProps> = ({ pick, index, inWatchlist, adding, onAdd, onAsk }) => (
  <StaggerItem as="li" index={index} className="cand">
    <div>
      <span className="nm">{pick.name}</span>
      <span className="cd">{pick.code}</span>
      {pick.price !== null || pick.changePct !== null ? (
        <p className="px num">
          <span>{pick.price !== null ? pick.price.toFixed(2) : '—'}</span>
          <span className={signClass(pick.changePct)}>{formatPct(pick.changePct)}</span>
        </p>
      ) : null}
      {pick.horizons.length ? (
        <p className="tags">
          {pick.horizons.map((horizon) => (
            <span key={horizon} className={`tag ${horizon === 'short' ? 'wait' : 'line'}`}>
              {HORIZON_LABEL[horizon]} {HORIZON_SPAN[horizon]}
            </span>
          ))}
        </p>
      ) : null}
    </div>
    <dl>
      <dt>入选理由</dt>
      <dd>
        {pick.reasons.length ? (
          <ul>
            {pick.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        ) : (
          <Dash />
        )}
      </dd>
      <dt>买入条件</dt>
      <dd>{pick.entryCondition ?? <Dash />}</dd>
      <dt>失效条件</dt>
      <dd>{pick.invalidCondition ?? <Dash />}</dd>
      <dt>风险</dt>
      <dd>
        {pick.risks.length ? (
          <ul>
            {pick.risks.map((risk) => (
              <li key={risk}>{risk}</li>
            ))}
          </ul>
        ) : (
          <Dash />
        )}
      </dd>
      {pick.note ? (
        <>
          <dt>备注</dt>
          <dd>{pick.note}</dd>
        </>
      ) : null}
    </dl>
    <div className="acts">
      <button
        type="button"
        className="btn sm"
        disabled={inWatchlist || adding}
        aria-label={inWatchlist ? `${pick.name} 已在自选` : `把 ${pick.name} 加入自选`}
        onClick={() => onAdd(pick)}
      >
        {inWatchlist ? '已在自选' : adding ? '加入中…' : '加入自选'}
      </button>
      <button type="button" className="btn quiet sm" aria-label={`问助手：${pick.name}`} onClick={() => onAsk(pick)}>
        <PlanIcon name="assist" size={14} />
        问助手
      </button>
    </div>
  </StaggerItem>
);
