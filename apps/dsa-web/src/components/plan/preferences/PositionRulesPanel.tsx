import type React from 'react';
import { useEffect, useId, useState } from 'react';
import { getPlanAccount } from '../../../api/dailyPlan';
import type { PositionRules } from '../../../types/dailyPlan';
import { showPlanToast } from '../../../stores/planToastStore';
import { formatNumber, parseNumber } from './format';
import { PanelHead, SaveProblem } from './shared';
import { RULE_KEYS, resetValues, serverValues, validateRules } from './positionRules';
import type { RuleKey, RuleValues } from './positionRules';
import { SizingTry } from './SizingTry';
import type { ConfigView, SaveOutcome } from './usePreferencesConfig';

interface PositionRulesPanelProps {
  config: ConfigView;
  onSave: (items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>;
  onReload: () => void;
}

export const PositionRulesPanel: React.FC<PositionRulesPanelProps> = ({ config, onSave, onReload }) => {
  const [rules, setRules] = useState<PositionRules | null>(null);
  const [sampleCode, setSampleCode] = useState('');
  const [edits, setEdits] = useState<Partial<RuleValues>>({});
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<RuleKey, string>>>({});
  const [problem, setProblem] = useState<Exclude<SaveOutcome, { ok: true }> | null>(null);
  const [saving, setSaving] = useState(false);
  const idPrefix = useId();

  // Effective rules (and a holding code for the sizing try-out). Missing service just means no defaults.
  useEffect(() => {
    let cancelled = false;
    getPlanAccount()
      .then((account) => {
        if (!cancelled) {
          setRules(account.rules);
          setSampleCode(account.holdings[0]?.code ?? '');
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [config.version]);

  const saved = serverValues(config, rules);
  const values: RuleValues = { ...saved, ...edits };
  const dirtyKeys = RULE_KEYS.filter((key) => values[key].trim() !== saved[key].trim());
  const dirty = dirtyKeys.length > 0;

  const total = parseNumber(values.POSITION_MAX_TOTAL_PCT);
  const holdings = parseNumber(values.POSITION_MAX_HOLDINGS);
  const explicitSingle = parseNumber(values.POSITION_MAX_SINGLE_PCT);
  const derivedSingle = total !== null && holdings !== null && holdings >= 1 ? total / holdings : null;
  const effectiveSingle = values.POSITION_MAX_SINGLE_PCT.trim() === '' ? derivedSingle : explicitSingle;

  const setValue = (key: RuleKey, value: string) => {
    setEdits((current) => ({ ...current, [key]: value }));
    setFieldErrors((current) => ({ ...current, [key]: undefined }));
    setProblem(null);
  };

  const discard = () => {
    setEdits({});
    setFieldErrors({});
    setProblem(null);
  };

  const restoreDefaults = () => {
    setEdits(resetValues(config));
    setFieldErrors({});
    setProblem(null);
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    const errors = validateRules(values);
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0 || !dirty) {
      return;
    }
    setSaving(true);
    const outcome = await onSave(dirtyKeys.map((key) => ({ key, value: values[key].trim() })));
    setSaving(false);
    if (outcome.ok) {
      setEdits({});
      setProblem(null);
      showPlanToast('仓位规则已保存');
      return;
    }
    if (outcome.kind === 'validation') {
      const next: Partial<Record<RuleKey, string>> = {};
      const general: string[] = [];
      for (const issue of outcome.issues) {
        if ((RULE_KEYS as string[]).includes(issue.key)) {
          next[issue.key as RuleKey] = issue.message;
        } else {
          general.push(issue.message);
        }
      }
      setFieldErrors(next);
      if (general.length > 0 || Object.keys(next).length === 0) {
        setProblem({ ...outcome, message: general.join('；') || outcome.message });
      } else {
        setProblem(null);
      }
      return;
    }
    setProblem(outcome);
  };

  const fieldProps = (key: RuleKey) => ({
    id: `${idPrefix}-${key}`,
    name: key,
    'aria-invalid': fieldErrors[key] ? (true as const) : undefined,
    'aria-describedby': fieldErrors[key] ? `${idPrefix}-${key}-err` : undefined,
  });
  const fieldError = (key: RuleKey) =>
    fieldErrors[key] ? (
      <span className="field-err" id={`${idPrefix}-${key}-err`}>
        {fieldErrors[key]}
      </span>
    ) : null;

  return (
    <>
      <PanelHead title="仓位规则" sub="改完立刻用于「我的」里的股数和止损计算" />
      <form className="pref-form" onSubmit={handleSubmit} noValidate>
        <div className="form-grid">
          <div className="f-cell">
            <label className="f" htmlFor={`${idPrefix}-POSITION_STOP_LOSS_PCT`}>
              止损幅度（%）
              <input
                {...fieldProps('POSITION_STOP_LOSS_PCT')}
                inputMode="decimal"
                value={values.POSITION_STOP_LOSS_PCT}
                onChange={(event) => setValue('POSITION_STOP_LOSS_PCT', event.target.value)}
              />
            </label>
            {fieldError('POSITION_STOP_LOSS_PCT')}
          </div>
          <div className="f-cell">
            <label className="f" htmlFor={`${idPrefix}-POSITION_MAX_TOTAL_PCT`}>
              总仓位上限（%）
              <input
                {...fieldProps('POSITION_MAX_TOTAL_PCT')}
                inputMode="decimal"
                value={values.POSITION_MAX_TOTAL_PCT}
                onChange={(event) => setValue('POSITION_MAX_TOTAL_PCT', event.target.value)}
              />
            </label>
            {fieldError('POSITION_MAX_TOTAL_PCT')}
          </div>
          <div className="f-cell">
            <label className="f" htmlFor={`${idPrefix}-POSITION_MAX_HOLDINGS`}>
              最多持有（只）
              <input
                {...fieldProps('POSITION_MAX_HOLDINGS')}
                inputMode="numeric"
                value={values.POSITION_MAX_HOLDINGS}
                onChange={(event) => setValue('POSITION_MAX_HOLDINGS', event.target.value)}
              />
            </label>
            {fieldError('POSITION_MAX_HOLDINGS')}
          </div>
          <div className="f-cell">
            <label className="f" htmlFor={`${idPrefix}-POSITION_MAX_SINGLE_PCT`}>
              单只上限（%）
              <input
                {...fieldProps('POSITION_MAX_SINGLE_PCT')}
                inputMode="decimal"
                value={values.POSITION_MAX_SINGLE_PCT}
                placeholder={derivedSingle !== null ? `留空：${formatNumber(derivedSingle, 1)}` : '留空：自动推出'}
                onChange={(event) => setValue('POSITION_MAX_SINGLE_PCT', event.target.value)}
              />
            </label>
            {fieldError('POSITION_MAX_SINGLE_PCT')}
          </div>
          <div className="f-cell f-wide">
            <label className="f-label" htmlFor={`${idPrefix}-POSITION_STOP_MODE`}>
              止损方式
            </label>
            <select
              {...fieldProps('POSITION_STOP_MODE')}
              value={values.POSITION_STOP_MODE}
              onChange={(event) => setValue('POSITION_STOP_MODE', event.target.value)}
            >
              <option value="fixed">固定比例</option>
              <option value="nearer">固定比例和技术支撑取较近者</option>
            </select>
            {fieldError('POSITION_STOP_MODE')}
          </div>
        </div>

        <p className="note" data-testid="position-derived">
          {values.POSITION_MAX_SINGLE_PCT.trim() === ''
            ? '单只上限留空时，由总仓位 ÷ 最多持有只数得出'
            : '单只上限已单独设置'}
          {effectiveSingle !== null ? `，现在是 ${formatNumber(effectiveSingle, 1)}%` : ''}
          。单笔最大亏损不单独设置，由股数和止损幅度决定。
        </p>
        <p className="note">
          买入股数 = 「单只上限、总仓位、可用现金」三者算出的股数取最小，再按每手 100 股向下取整（科创板单笔至少 200 股）；
          止损价 = 买入价 × (1 − 止损幅度)。具体数字以「我的」里的计划为准。
        </p>

        <SaveProblem outcome={problem} onReload={() => { discard(); onReload(); }} />
        <div className="actions pref-actions">
          <button type="submit" className="btn primary" disabled={!dirty || saving}>
            {saving ? '正在保存…' : '保存规则'}
          </button>
          {dirty ? (
            <button type="button" className="btn quiet" onClick={discard}>
              放弃修改
            </button>
          ) : null}
          <button type="button" className="btn quiet" onClick={restoreDefaults}>
            恢复默认值
          </button>
        </div>
      </form>

      <SizingTry dirty={dirty} initialCode={sampleCode} />
    </>
  );
};
