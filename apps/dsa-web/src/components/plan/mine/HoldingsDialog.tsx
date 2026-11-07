import type React from 'react';
import { useId, useRef, useState } from 'react';
import { putPlanHoldings } from '../../../api/dailyPlan';
import { showPlanToast } from '../../../stores/planToastStore';
import type { AccountResponse } from '../../../types/dailyPlan';
import { GlassDialog } from '../GlassDialog';
import { PlanIcon } from '../PlanIcon';
import { draftFromAccount, emptyRow, hasErrors, snapshotFromDraft, validateDraft } from './holdingsDraft';
import type { DraftErrors, HoldingDraftRow } from './holdingsDraft';
import { holdingsSaveErrorText } from './saveErrors';

export interface HoldingsDialogProps {
  open: boolean;
  onClose: () => void;
  /** Current account the draft starts from (null while it failed to load). */
  account: AccountResponse | null;
  /** Called with the saved account after a successful PUT. */
  onSaved: (account: AccountResponse) => void;
}

const FORM_ID = 'plan-holdings-form';

/**
 * 「持仓与资金」: edit cash and the full holding list, then overwrite with `PUT /plan/holdings`.
 * Mount with a fresh `key` per open so the draft restarts from the current account.
 */
export const HoldingsDialog: React.FC<HoldingsDialogProps> = ({ open, onClose, account, onSaved }) => {
  const [initial] = useState(() => draftFromAccount(account));
  const [cash, setCash] = useState(initial.cash);
  const [rows, setRows] = useState<HoldingDraftRow[]>(initial.rows);
  const [errors, setErrors] = useState<DraftErrors>({ rows: {} });
  const [serverError, setServerError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const idPrefix = useId();
  const cashRef = useRef<HTMLInputElement>(null);
  const rowsRef = useRef<HTMLDivElement>(null);
  const maxHoldings = account?.rules.maxHoldings;

  const updateRow = (key: string, patch: Partial<HoldingDraftRow>) => {
    setRows((current) => current.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  };

  const focusField = (key: string, field: string) => {
    window.setTimeout(() => {
      rowsRef.current?.querySelector<HTMLElement>(`[data-row="${key}"] [name="${field}"]`)?.focus();
    }, 0);
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving) {
      return;
    }
    setServerError(null);
    const found = validateDraft(cash, rows);
    setErrors(found);
    if (hasErrors(found)) {
      if (found.cash) {
        cashRef.current?.focus();
      } else {
        const firstKey = rows.find((row) => found.rows[row.key])?.key;
        if (firstKey) {
          focusField(firstKey, found.rows[firstKey].field);
        }
      }
      return;
    }
    setSaving(true);
    try {
      const saved = await putPlanHoldings(snapshotFromDraft(cash, rows));
      showPlanToast('已保存持仓');
      onSaved(saved);
      onClose();
    } catch (error: unknown) {
      setServerError(holdingsSaveErrorText(error));
    } finally {
      setSaving(false);
    }
  };

  const addRow = () => {
    const row = emptyRow();
    setRows((current) => [...current, row]);
    focusField(row.key, 'code');
  };

  const removeRow = (key: string) => {
    setRows((current) => current.filter((row) => row.key !== key));
    setErrors((current) => {
      const next = { ...current.rows };
      delete next[key];
      return { ...current, rows: next };
    });
  };

  const errorText = serverError ?? errors.form ?? null;

  return (
    <GlassDialog
      open={open}
      onClose={onClose}
      title="持仓与资金"
      width={900}
      initialFocusRef={cashRef}
      footer={
        <>
          <button type="button" className="btn quiet" onClick={onClose}>
            取消
          </button>
          <button type="submit" form={FORM_ID} className="btn primary" disabled={saving}>
            {saving ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      <form id={FORM_ID} className="holdings-form" noValidate onSubmit={submit}>
        <p className="sub">
          先手动录入持仓快照，保存后会整体覆盖现有持仓。
          {maxHoldings ? `当前规则最多持有 ${maxHoldings} 只。` : ''}
        </p>
        <div className="form-grid cash-field">
          <label className="f">
            可用现金（元）
            <input
              ref={cashRef}
              name="cash"
              inputMode="decimal"
              autoComplete="off"
              value={cash}
              aria-invalid={errors.cash ? true : undefined}
              aria-describedby={errors.cash ? `${idPrefix}-cash-err` : undefined}
              onChange={(event) => setCash(event.target.value)}
            />
          </label>
        </div>
        {errors.cash ? (
          <p className="err" id={`${idPrefix}-cash-err`}>
            {errors.cash}
          </p>
        ) : null}

        <div className="hold-rows" ref={rowsRef}>
          {rows.length === 0 ? <p className="note">现在没有持仓。点「添加持仓」录入一只。</p> : null}
          {rows.map((row, index) => {
            const rowError = errors.rows[row.key];
            const invalid = (field: string) => (rowError?.field === field ? true : undefined);
            const errId = `${idPrefix}-${row.key}-err`;
            return (
              <div key={row.key} data-row={row.key} className="holding-edit">
                <label className="f">
                  股票代码{row.name ? `（${row.name}）` : ''}
                  <input
                    name="code"
                    inputMode="numeric"
                    maxLength={6}
                    autoComplete="off"
                    placeholder="6 位代码"
                    value={row.code}
                    aria-invalid={invalid('code')}
                    aria-describedby={rowError ? errId : undefined}
                    onChange={(event) => updateRow(row.key, { code: event.target.value.replace(/\D/g, ''), name: '' })}
                  />
                </label>
                <label className="f">
                  数量（股）
                  <input
                    name="shares"
                    inputMode="numeric"
                    autoComplete="off"
                    value={row.shares}
                    aria-invalid={invalid('shares')}
                    aria-describedby={rowError ? errId : undefined}
                    onChange={(event) => updateRow(row.key, { shares: event.target.value })}
                  />
                </label>
                <label className="f">
                  成本价
                  <input
                    name="cost"
                    inputMode="decimal"
                    autoComplete="off"
                    value={row.cost}
                    aria-invalid={invalid('cost')}
                    aria-describedby={rowError ? errId : undefined}
                    onChange={(event) => updateRow(row.key, { cost: event.target.value })}
                  />
                </label>
                <label className="f">
                  买入日（选填）
                  <input
                    name="buyDate"
                    type="date"
                    value={row.buyDate}
                    aria-invalid={invalid('buyDate')}
                    aria-describedby={rowError ? errId : undefined}
                    onChange={(event) => updateRow(row.key, { buyDate: event.target.value })}
                  />
                </label>
                <label className="f">
                  计划周期（选填）
                  <select
                    name="horizon"
                    value={row.horizon}
                    onChange={(event) => updateRow(row.key, { horizon: event.target.value as HoldingDraftRow['horizon'] })}
                  >
                    <option value="">未填</option>
                    <option value="short">短线</option>
                    <option value="swing">波段</option>
                  </select>
                </label>
                <button
                  type="button"
                  className="btn quiet sm"
                  aria-label={`删除第 ${index + 1} 条持仓`}
                  onClick={() => removeRow(row.key)}
                >
                  <PlanIcon name="close" size={14} />
                  删除
                </button>
                {rowError ? (
                  <p className="err row-err" id={errId}>
                    {rowError.message}
                  </p>
                ) : null}
              </div>
            );
          })}
        </div>
        <p className="err" role="alert">
          {errorText}
        </p>
        <div className="actions">
          <button type="button" className="btn" onClick={addRow}>
            + 添加持仓
          </button>
        </div>
      </form>
    </GlassDialog>
  );
};
