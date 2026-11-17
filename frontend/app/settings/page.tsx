import Workspace from '@/components/Workspace';

export const metadata = { title: 'Настройки · Element Workspace' };

export default function SettingsPage() {
  return <Workspace initialPanel="settings" />;
}
