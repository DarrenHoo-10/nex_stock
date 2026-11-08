import type React from 'react';
import { useId, useRef, useState } from 'react';
import { getPlanAccount, putPlanHoldings } from '../../../api/dailyPlan';
import { showPlanToast } from '../../../stores/planToastStore';
import type { AccountResponse, Horizon } from '../../../types/dailyPlan';
import { GlassDialog } from '../GlassDialog';
import { fmtNum, fmtPrice, todayLocalDate } from './format';
import { mergeRecordedBuy, parseNumber } from './holdingsDraft';
import { holdingsSaveErrorText } from './saveErrors';

export interface RecordBuyDialogProps {
  open: boolean;
  onClose: () => void;
  code: string;
  name: string;
  /** Prefill: sizing shares (0 or null when the plan gave none) and the planned entry price. */
  suggestedShares: number | null;
  suggestedPrice: number | null;
  horizon: Horizon;
  /** Called with the saved account after a successful PUT. */
  onSaved: (account: AccountResponse) => void;
}

const FORM_ID = 'plan-record-buy-form';

interface FieldErrors {
  shares?: string;
  price?: string;
  form?: string;
}

/**
 * 「记录买入」: shares and price are prefilled from the plan and editable. Confirming reads the current account,
 * appends (or merges into) the holding list, takes the amount out of cash and overwrites via `PUT /plan/holdings`.
 * Mount with a fresh `key` per open.
 */
export const RecordBuyDialog: React.FC<RecordBuyDialogProps> = ({
  open,
  onClose,
  code,
  name,
  suggestedShares,
  suggestedPrice,
  horizon,
  onSaved,
}) => {
  const [shares, setShares] = useState(suggestedShares && suggestedShares > 0 ? String(suggestedShares) : '');
  const [price, setPrice] = useState(suggestedPrice != null && suggestedPrice > 0 ? suggestedPrice.toFixed(2) : '');
  const [buyDate, setBuyDate] = useState(() => todayLocalDate());
  const [chosenHorizon, setChosenHorizon] = useState<Horizon>(horizon);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [saving, setSaving] = useState(false);
  const idPrefix = useId();
  const sharesRef = useRef<HTMLInputElement>(null);

  const sharesNumber = parseNumber(shares);
  const priceNumber = parseNumber(price);
  const amount = sharesNumber > 0 && priceNumber > 0 ? Math.round(sharesNumber * priceNumber * 100) / 100 : null;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving) {
      return;
    }
    const found: FieldErrors = {};
    if (!(Number.isInteger(sharesNumber) && sharesNumber > 0)) {
      found.shares = '数量要填大于 0 的整数。';
    }
    if (!(priceNumber > 0)) {
      found.price = '买入价要大于 0。';
    }
    setErrors(found);
    if (found.shares || found.price) {
      return;
    }
    setSaving(true);
    try {
      // Read the account again so rows saved elsewhere are kept and cash is current.
      const account = await getPlanAccount();
      const merged = mergeRecordedBuy(account, {
        code,
        shares: sharesNumber,
        price: priceNumber,
        buyDate: buyDate || null,
        horizon: chosenHorizon,
      });
      if (!merged.snapshot) {
        setErrors({
          form: `这笔买入需要 ${fmtNum(merged.amount, 2)} 元，可用现金只有 ${fmtNum(account.cash, 2)} 元。可以先在「持仓与资金」里调整现金。`,
        });
        return;
      }
      const saved = await putPlanHoldings(merged.snapshot);
      showPlanToast(`已记录买入，现金扣减 ${fmtNum(merged.amount, 2)} 元`);
      onSaved(saved);
      onClose();
    } catch (error: unknown) {
      setErrors({ form: holdingsSaveErrorText(error) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <GlassDialog
      open={open}
      onClose={onClose}
      title={`记录买入：${name}`}
      width={520}
      initialFocusRef={sharesRef}
      footer={
        <>
          <button type="button" className="btn quiet" onClick={onClose}>
            取消
          </button>
          <button type="submit" form={FORM_ID} className="btn primary" disabled={saving}>
            {saving ? '保存中…' : '确认买入'}
          </button>
        </>
      }
    >
      <form id={FORM_ID} noValidate onSubmit={submit}>
        <p className="sub">
          {code} {name}。已经按计划买入后在这里记一笔，会加进持仓，并从现金里扣掉这笔金额。
        </p>
        <div className="form-grid record-grid">
          <label className="f">
            买入数量（股）
            <input
              ref={sharesRef}
              name="shares"
              inputMode="numeric"
              autoComplete="off"
              value={shares}
              aria-invalid={errors.shares ? true : undefined}
              aria-describedby={errors.shares ? `${idPrefix}-shares-err` : undefined}
              onChange={(event) => setShares(event.target.value)}
            />
          </label>
          <label className="f">
            买入价
            <input
              name="price"
              inputMode="decimal"
              autoComplete="off"
              value={price}
              aria-invalid={errors.price ? true : undefined}
              aria-describedby={errors.price ? `${idPrefix}-price-err` : undefined}
              onChange={(event) => setPrice(event.target.value)}
            />
          </label>
          <label className="f">
            买入日期
            <input name="buyDate" type="date" value={buyDate} onChange={(event) => setBuyDate(event.target.value)} />
          </label>
          <label className="f">
            计划周期
            <select
              name="horizon"
              value={chosenHorizon}
              onChange={(event) => setChosenHorizon(event.target.value as Horizon)}
            >
              <option value="short">短线</option>
              <option value="swing">波段</option>
            </select>
          </label>
        </div>
        {errors.shares ? (
          <p className="err" id={`${idPrefix}-shares-err`}>
            {errors.shares}
          </p>
        ) : null}
        {errors.price ? (
          <p className="err" id={`${idPrefix}-price-err`}>
            {errors.price}
          </p>
        ) : null}
        <p className="note record-amount" aria-live="polite">
          {amount != null ? `买入金额 ${fmtNum(amount, 2)} 元（${fmtNum(sharesNumber)} 股 × ${fmtPrice(priceNumber)}）` : '填好数量和买入价后显示金额。'}
        </p>
        <p className="err" role="alert">
          {errors.form}
        </p>
      </form>
    </GlassDialog>
  );
};
