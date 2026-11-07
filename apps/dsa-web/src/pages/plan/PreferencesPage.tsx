import type React from 'react';
import { useEffect, useRef, useState } from 'react';
import { LayoutGroup, motion } from 'motion/react';
import { useSearchParams } from 'react-router-dom';
import { PlanPageHeader } from '../../components/plan/PlanPageHeader';
import { PLAN_INSTANT, PLAN_SPRING } from '../../components/plan/motion/springs';
import { usePlanReducedMotion } from '../../components/plan/motion/usePlanMotion';
import { DataStatusPanel } from '../../components/plan/preferences/DataStatusPanel';
import { ModelsPanel } from '../../components/plan/preferences/ModelsPanel';
import { PositionRulesPanel } from '../../components/plan/preferences/PositionRulesPanel';
import { RunPushPanel } from '../../components/plan/preferences/RunPushPanel';
import { ThresholdsPanel } from '../../components/plan/preferences/ThresholdsPanel';
import { ConfigGate } from '../../components/plan/preferences/shared';
import { usePreferencesConfig } from '../../components/plan/preferences/usePreferencesConfig';
import type { ConfigView } from '../../components/plan/preferences/usePreferencesConfig';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import '../../styles/plan-preferences.css';

const TABS = [
  { id: 'rules', label: '仓位规则' },
  { id: 'models', label: '模型' },
  { id: 'thresholds', label: '异动阈值' },
  { id: 'run', label: '运行和推送' },
  { id: 'data', label: '数据状态' },
] as const;

type TabId = (typeof TABS)[number]['id'];

function readTab(value: string | null): TabId {
  return TABS.find((tab) => tab.id === value)?.id ?? 'rules';
}

/**
 * /preferences: position rules, models (by role), board thresholds, run and push, data status.
 * Tabs deep-link with `?tab=`; panels are mounted on first visit and kept (hidden) so unsaved edits survive
 * switching tabs. Config-backed tabs share one GET /system/config and save with its `config_version`.
 */
const PreferencesPage: React.FC = () => {
  const { t } = useUiLanguage();
  const title = t('plan.page.preferences.title');
  const reduced = usePlanReducedMotion();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = readTab(searchParams.get('tab'));
  const [visited, setVisited] = useState<ReadonlySet<TabId>>(() => new Set([tab]));
  const tabRefs = useRef<Partial<Record<TabId, HTMLButtonElement | null>>>({});
  const { state: configState, reload, save } = usePreferencesConfig();

  useEffect(() => {
    document.title = `${title} - DSA`;
  }, [title]);

  // Panels mount on first visit and stay mounted afterwards (adjusting state while rendering is the React-sanctioned way).
  if (!visited.has(tab)) {
    setVisited(new Set(visited).add(tab));
  }

  const selectTab = (id: TabId, focus = false) => {
    setSearchParams({ tab: id }, { replace: true });
    if (focus) {
      tabRefs.current[id]?.focus();
    }
  };

  const handleKeyDown = (event: React.KeyboardEvent) => {
    const index = TABS.findIndex((item) => item.id === tab);
    let next = -1;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
      next = (index + 1) % TABS.length;
    } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
      next = (index - 1 + TABS.length) % TABS.length;
    } else if (event.key === 'Home') {
      next = 0;
    } else if (event.key === 'End') {
      next = TABS.length - 1;
    }
    if (next >= 0) {
      event.preventDefault();
      selectTab(TABS[next].id, true);
    }
  };

  const withConfig = (render: (view: ConfigView) => React.ReactNode) =>
    configState.status === 'ready' ? (
      render(configState.view)
    ) : (
      <ConfigGate
        status={configState.status}
        message={configState.status === 'error' ? configState.error.message : undefined}
        onRetry={() => void reload()}
      />
    );

  const panels: Record<TabId, () => React.ReactNode> = {
    rules: () =>
      withConfig((view) => <PositionRulesPanel config={view} onSave={save} onReload={() => void reload()} />),
    models: () => withConfig((view) => <ModelsPanel config={view} onSave={save} onReload={() => void reload()} />),
    thresholds: () => <ThresholdsPanel />,
    run: () => withConfig((view) => <RunPushPanel config={view} onSave={save} onReload={() => void reload()} />),
    data: () => <DataStatusPanel />,
  };

  return (
    <>
      <PlanPageHeader title={title} subtitle={t('plan.page.preferences.subtitle')} />
      <section className="win settings pref-settings">
        <LayoutGroup id="pref-tabs">
          <div className="set-nav" role="tablist" aria-label="设置分类" onKeyDown={handleKeyDown}>
            {TABS.map((item) => {
              const selected = item.id === tab;
              return (
                <button
                  key={item.id}
                  ref={(element) => {
                    tabRefs.current[item.id] = element;
                  }}
                  type="button"
                  role="tab"
                  id={`pref-tab-${item.id}`}
                  aria-selected={selected}
                  aria-controls={`pref-panel-${item.id}`}
                  tabIndex={selected ? 0 : -1}
                  onClick={() => selectTab(item.id)}
                >
                  {selected ? (
                    <motion.span
                      className="thumb"
                      layoutId="pref-tab-thumb"
                      transition={reduced ? PLAN_INSTANT : PLAN_SPRING}
                    />
                  ) : null}
                  {item.label}
                </button>
              );
            })}
          </div>
        </LayoutGroup>
        <div className="set-body">
          {TABS.map((item) => {
            const selected = item.id === tab;
            return (
              <div
                key={item.id}
                className="pref-panel"
                role="tabpanel"
                id={`pref-panel-${item.id}`}
                aria-labelledby={`pref-tab-${item.id}`}
                tabIndex={0}
                hidden={!selected}
              >
                {selected || visited.has(item.id) ? panels[item.id]() : null}
              </div>
            );
          })}
        </div>
      </section>
    </>
  );
};

export default PreferencesPage;
