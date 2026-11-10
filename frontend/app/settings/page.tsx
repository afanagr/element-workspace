import { SettingsPanel } from '@/components/SettingsPanel';

export const metadata = { title: 'Настройки LLM · Element Workspace' };

export default function SettingsPage() {
  return (
    <div className="min-h-screen">
      <SettingsPanel />
    </div>
  );
}
