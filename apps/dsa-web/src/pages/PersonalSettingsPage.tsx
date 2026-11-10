import type React from 'react';
import { useEffect } from 'react';
import { PersonalSettingsPanel } from '../components/settings/PersonalSettingsPanel';
import { SettingsScopeTabs } from '../components/settings/SettingsScopeTabs';
import { useUiLanguage } from '../contexts/UiLanguageContext';

/** `/settings/personal`: 个人设置, available to every signed-in user (system settings stay administrator-only). */
const PersonalSettingsPage: React.FC = () => {
  const { t } = useUiLanguage();
  const title = t('personalSettings.pageTitle');

  useEffect(() => {
    document.title = `${title} - DSA`;
  }, [title]);

  return (
    <div className="min-h-full">
      <div className="px-4 pt-4 md:px-6">
        <SettingsScopeTabs className="mb-0" />
      </div>
      <PersonalSettingsPanel />
    </div>
  );
};

export default PersonalSettingsPage;
