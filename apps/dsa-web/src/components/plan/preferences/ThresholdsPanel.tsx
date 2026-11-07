import type React from 'react';
import { useCallback, useEffect, useId, useState } from 'react';
import { isPlanServiceUnavailable } from '../../../api/boards';
import { showPlanToast } from '../../../stores/planToastStore';
import type { ThresholdItem } from '../../../types/boards';
import { GlassDialog } from '../GlassDialog';
import { parseNumber } from './format';
import { PanelHead } from './shared';
import {
  EVIDENCE_INFO,
  buildOverrides,
  currentText,
  describeThresholdError,
  editedKeys,
  groupThresholds,
  invalidNumbers,
  isNumericThreshold,
  loadThresholds,
  saveThresholdOverrides,
  sameValue,
} from './thresholds';

type LoadState =
  | { status: 'loading' }
  | { status: 'ready'; items: ThresholdItem[] }
  | { status: 'unavailable' }
  | { status: 'error' };

type Problem = { kind: 'validation' | 'conflict' | 'unavailable' | 'error'; lines: string[] } | null;

export const ThresholdsPanel: React.FC = () => {
  const [load, setLoad] = useState<LoadState>({ status: 'loading' });
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [invalid, setInvalid] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<Problem>(null);
  const [saving, setSaving] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const idPrefix = useId();

  const fetchItems = useCallback(async () => {
    try {
      setLoad({ status: 'ready', items: await loadThresholds() });
    } catch (error) {
      setLoad(isPlanServiceUnavailable(error) ? { status: 'unavailable' } : { status: 'error' });
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    loadThresholds()
      .then((items) => {
        if (!cancelled) {
          setLoad({ status: 'ready', items });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoad(isPlanServiceUnavailable(error) ? { status: 'unavailable' } : { status: 'error' });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const retry = () => {
    setLoad({ status: 'loading' });
    void fetchItems();
  };

  const items = load.status === 'ready' ? load.items : [];
  const dirtyKeys = editedKeys(items, edits);
  const dirty = dirtyKeys.length > 0;
  const anyOverridden = items.some((item) => item.overridden);

  const setEdit = (key: string, text: string) => {
    setEdits((current) => ({ ...current, [key]: text }));
    setInvalid((current) => {
      const { [key]: removed, ...rest } = current;
      void removed;
      return rest;
    });
    setProblem(null);
  };

  const resetOne = (item: ThresholdItem) => setEdit(item.key, String(item.default));

  const discard = () => {
    setEdits({});
    setInvalid({});
    setProblem(null);
  };

  const submit = async (overrides: ReturnType<typeof buildOverrides>, successMessage: string) => {
    setSaving(true);
    setProblem(null);
    try {
      await saveThresholdOverrides(overrides);
      setEdits({});
      setInvalid({});
      await fetchItems();
      showPlanToast(successMessage);
    } catch (error) {
      const described = describeThresholdError(error);
      setProblem(described);
      if (described.kind === 'conflict') {
        await fetchItems();
      }
    } finally {
      setSaving(false);
    }
  };

  const handleSave = async (event: React.FormEvent) => {
    event.preventDefault();
    const errors = invalidNumbers(items, edits);
    setInvalid(errors);
    if (Object.keys(errors).length > 0 || !dirty) {
      return;
    }
    await submit(buildOverrides(items, edits), '异动阈值已保存');
  };

  const restoreAll = async () => {
    setConfirmOpen(false);
    await submit({}, '已全部恢复默认');
  };

  if (load.status === 'loading') {
    return (
      <>
        <PanelHead title="异动阈值" />
        <p className="note" role="status">
          正在读取阈值…
        </p>
      </>
    );
  }
  if (load.status === 'unavailable' || load.status === 'error') {
    return (
      <>
        <PanelHead title="异动阈值" />
        <div className="notice pref-notice" role="alert">
          <b>{load.status === 'unavailable' ? '次日计划服务未就绪' : '阈值读取失败'}</b>
          <p>
            {load.status === 'unavailable'
              ? '后端的板块异动服务还没有启用，阈值暂时不能查看和修改。'
              : '请稍后重试。'}
          </p>
          <button type="button" className="btn sm" onClick={retry}>
            重试
          </button>
        </div>
      </>
    );
  }

  const groups = groupThresholds(items);

  return (
    <>
      <PanelHead
        title="异动阈值"
        sub="默认值来自调研，「依据」说明可信程度；正式使用前要用历史数据回测校准"
      />
      <dl className="evidence-legend" aria-label="依据等级说明">
        {(['medium', 'low', 'inferred'] as const).map((level) => (
          <div key={level}>
            <dt>
              <span className={`ev-tag ev-${level}`}>{EVIDENCE_INFO[level].label}</span>
            </dt>
            <dd>{EVIDENCE_INFO[level].explain}</dd>
          </div>
        ))}
      </dl>

      <form className="pref-form" onSubmit={handleSave} noValidate>
        {groups.map((group) => (
          <section className="thr-group" key={group.id} aria-labelledby={`${idPrefix}-${group.id}`}>
            <h3 id={`${idPrefix}-${group.id}`}>{group.title}</h3>
            <ul className="thr-list">
              {group.items.map((item) => {
                const numeric = isNumericThreshold(item);
                const text = currentText(item, edits);
                const parsed = parseNumber(text);
                const atDefault = numeric ? parsed !== null && sameValue(parsed, item.default) : !item.overridden;
                const inputId = `${idPrefix}-${item.key}`;
                return (
                  <li className="thr-row" key={item.key} data-key={item.key}>
                    <div className="thr-main">
                      <p className="thr-name">
                        <b>{item.label}</b>
                        <span className={`ev-tag ev-${item.evidence}`}>依据：{EVIDENCE_INFO[item.evidence].label}</span>
                        {item.overridden ? <span className="tag warn">已修改</span> : null}
                      </p>
                      <p className="thr-desc">{item.display}</p>
                      {item.note ? <p className="thr-desc">{item.note}</p> : null}
                    </div>
                    <div className="thr-side">
                      {numeric ? (
                        <div className="f-cell thr-input">
                          <label className="vh" htmlFor={inputId}>
                            {item.label} 当前值
                          </label>
                          <span className="thr-field">
                            <input
                              id={inputId}
                              inputMode="decimal"
                              value={text}
                              aria-invalid={invalid[item.key] ? true : undefined}
                              aria-describedby={invalid[item.key] ? `${inputId}-err` : undefined}
                              onChange={(event) => setEdit(item.key, event.target.value)}
                            />
                            {item.unit ? <span className="thr-unit">{item.unit}</span> : null}
                          </span>
                          {invalid[item.key] ? (
                            <span className="field-err" id={`${inputId}-err`}>
                              {invalid[item.key]}
                            </span>
                          ) : null}
                        </div>
                      ) : (
                        <span className="thr-readonly">暂不支持在这里修改</span>
                      )}
                      <p className="thr-default">
                        默认 <span className="num">{typeof item.default === 'object' ? '—' : String(item.default)}</span>
                        {item.unit ?? ''}
                      </p>
                      {numeric ? (
                        <button
                          type="button"
                          className="btn quiet sm"
                          disabled={atDefault}
                          aria-label={`${item.label} 恢复默认`}
                          onClick={() => resetOne(item)}
                        >
                          恢复默认
                        </button>
                      ) : null}
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}

        <p className="note">行业板块不用涨停条件。主力资金流只展示，不参与判定。修改后下一次运行生效。</p>

        {problem?.kind === 'conflict' ? (
          <div className="notice pref-notice" role="alert">
            <b>配置已变化</b>
            <p>阈值在别处被修改过，已重新读取最新值。再改一次后保存。</p>
          </div>
        ) : null}
        {problem && problem.kind !== 'conflict' ? (
          <div className="err pref-errlist" role="alert" data-testid="threshold-errors">
            <p>{problem.kind === 'validation' ? '阈值没有保存，请修改下面的问题：' : '阈值没有保存：'}</p>
            {problem.lines.length > 1 || problem.kind === 'validation' ? (
              <ul>
                {problem.lines.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            ) : (
              <p>{problem.lines[0]}</p>
            )}
          </div>
        ) : null}

        <div className="actions pref-actions">
          <button type="submit" className="btn primary" disabled={!dirty || saving}>
            {saving ? '正在保存…' : '保存阈值'}
          </button>
          {dirty ? (
            <button type="button" className="btn quiet" onClick={discard}>
              放弃修改
            </button>
          ) : null}
          <button
            type="button"
            className="btn quiet"
            disabled={saving || (!anyOverridden && !dirty)}
            onClick={() => setConfirmOpen(true)}
          >
            全部恢复默认
          </button>
        </div>
      </form>

      <GlassDialog
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title="全部恢复默认"
        width={440}
        footer={
          <>
            <button type="button" className="btn quiet" onClick={() => setConfirmOpen(false)}>
              取消
            </button>
            <button type="button" className="btn primary" onClick={() => void restoreAll()}>
              确认恢复
            </button>
          </>
        }
      >
        <p>所有改过的阈值都会回到默认值，下一次运行生效。</p>
      </GlassDialog>
    </>
  );
};
