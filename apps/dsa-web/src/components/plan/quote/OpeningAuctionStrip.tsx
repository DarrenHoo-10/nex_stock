import { useEffect, useState } from 'react';
import { stocksApi, type OpeningAuction } from '../../../api/stocks';
import { useUiLanguage } from '../../../contexts/UiLanguageContext';
import { formatVolume } from './klineMath';
import { svgPointer } from './svgPointer';
import { CHART_COLORS } from './chartTheme';

const xOf = (time: string) => {
  const [, minute, second] = time.split(':').map(Number);
  return 4 + ((minute - 15) * 60 + (second || 0)) / 600 * 180;
};

export function OpeningAuctionStrip({ code, date, digits }: { code: string; date: string; digits: number }) {
  const { t } = useUiLanguage();
  const [auction, setAuction] = useState<OpeningAuction | null>(null);
  const [hover, setHover] = useState<number | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    stocksApi.getOpeningAuction(code, date, controller.signal)
      .then((result) => { if (!controller.signal.aborted) setAuction(result); })
      .catch(() => { if (!controller.signal.aborted) setAuction({ source: null, points: [] }); });
    return () => controller.abort();
  }, [code, date]);

  const points = auction?.points ?? [];
  const active = points[hover ?? points.length - 1];
  const prices = points.map((point) => point.price);
  const low = Math.min(...prices);
  const high = Math.max(...prices);
  const yOf = (price: number) => high === low ? 15 : 27 - (price - low) / (high - low) * 24;
  return (
    <div className="qi-auction">
      <strong>{t('quote.auction.title')}</strong>
      {active ? (
        <>
          {points.length > 1 && (
            <svg width="216" height="44" viewBox="0 0 206 44" role="img" aria-label={t('quote.auction.title')}
              onPointerLeave={() => setHover(null)}
              onPointerMove={(event) => {
                const point = svgPointer(event.currentTarget, event.clientX, event.clientY);
                if (!point) return;
                const { x } = point;
                const nearest = points.reduce((best, p, i) =>
                  Math.abs(xOf(p.time) - x) < Math.abs(xOf(points[best].time) - x) ? i : best, 0);
                setHover(nearest);
              }}>
              <polyline fill="none" stroke={CHART_COLORS.ma[3]} strokeWidth="1.6"
                points={points.map((p) => `${xOf(p.time)},${yOf(p.price)}`).join(' ')} />
              <circle cx={xOf(active.time)} cy={yOf(active.price)} r="2.8" fill={CHART_COLORS.ma[3]} />
              <g fill={CHART_COLORS.text} fontSize="10">
                <text x="4" y="42">09:15</text><text x="184" y="42" textAnchor="end">09:25</text>
              </g>
            </svg>
          )}
          <span>{active.time} · {t(auction?.source !== 'ashareapi' && active.time.startsWith('09:25:') ? 'quote.auction.match' : 'quote.intraday.price')} {active.price.toFixed(digits)}</span>
          {active.volume != null && <span>{t('quote.volume')} {formatVolume(active.volume)}</span>}
        </>
      ) : <span>{t(auction ? 'quote.auction.empty' : 'quote.intraday.loading')}</span>}
    </div>
  );
}
