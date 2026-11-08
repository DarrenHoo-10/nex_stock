import type React from 'react';
import { useUiLanguage } from '../../../contexts/UiLanguageContext';
import { formatVolume } from './klineMath';
import type { ChartBar, KlinePeriod } from './klineMath';

export interface KlineBarTipProps {
  bar: ChartBar;
  /** Previous candle's close; prices are colored against it. */
  prevClose: number | null;
  digits: number;
  period: KlinePeriod;
  /** Inline position (CSS left/right/top), chosen so the panel sits opposite the cursor. */
  style: React.CSSProperties;
}

const WEEKDAYS_ZH = ['日', '一', '二', '三', '四', '五', '六'];
const WEEKDAYS_EN = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

function tone(value: number, base: number | null): string | undefined {
  if (base === null || value === base) {
    return undefined;
  }
  return value > base ? 'up' : 'down';
}

/** Floating info panel for the candle under the crosshair's vertical line. */
export const KlineBarTip: React.FC<KlineBarTipProps> = ({ bar, prevClose, digits, period, style }) => {
  const { t, language } = useUiLanguage();
  const date = bar.date.slice(0, 10);
  const [y, m, d] = date.split('-').map(Number);
  const weekday = new Date(Date.UTC(y, (m || 1) - 1, d || 1)).getUTCDay();
  const dateLabel =
    period === 'monthly'
      ? date.slice(0, 7)
      : `${date} ${language === 'en' ? WEEKDAYS_EN[weekday] : `周${WEEKDAYS_ZH[weekday]}`}`;

  const change = prevClose !== null ? bar.close - prevClose : null;
  const changePct = prevClose ? (change! / prevClose) * 100 : bar.changePercent;
  const amplitude = prevClose ? ((bar.high - bar.low) / prevClose) * 100 : null;
  const changeTone = change === null ? undefined : change > 0 ? 'up' : change < 0 ? 'down' : undefined;
  const sign = (value: number) => (value > 0 ? '+' : '');

  const rows: Array<{ label: string; value: string; tone?: string }> = [
    { label: t('quote.open'), value: bar.open.toFixed(digits), tone: tone(bar.open, prevClose) },
    { label: t('quote.high'), value: bar.high.toFixed(digits), tone: tone(bar.high, prevClose) },
    { label: t('quote.low'), value: bar.low.toFixed(digits), tone: tone(bar.low, prevClose) },
    { label: t('quote.close'), value: bar.close.toFixed(digits), tone: tone(bar.close, prevClose) },
    {
      label: t('quote.changeAmount'),
      value: change === null ? '--' : `${sign(change)}${change.toFixed(digits)}`,
      tone: changeTone,
    },
    {
      label: t('quote.change'),
      value: changePct === null || changePct === undefined ? '--' : `${sign(changePct)}${changePct.toFixed(2)}%`,
      tone: changeTone,
    },
    { label: t('quote.amplitude'), value: amplitude === null ? '--' : `${amplitude.toFixed(2)}%` },
    { label: t('quote.volume'), value: formatVolume(bar.volume) },
    { label: t('quote.amount'), value: formatVolume(bar.amount) },
    { label: t('quote.turnover'), value: bar.turnoverRate !== null ? `${bar.turnoverRate.toFixed(2)}%` : '--' },
  ];

  return (
    <div className="kc-tip" style={style} aria-hidden="true">
      <div className="kc-tip-date">{dateLabel}</div>
      <dl>
        {rows.map((row) => (
          <div key={row.label}>
            <dt>{row.label}</dt>
            <dd className={row.tone}>{row.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
};
