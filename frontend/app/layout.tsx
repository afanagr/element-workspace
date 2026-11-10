import type { Metadata, Viewport } from 'next';
import './globals.css';

import { AppProviders } from '@/components/AppProviders';

export const metadata: Metadata = {
  title: 'Element Workspace',
  description:
    'Private workspace for documents with multi-provider AI, RAG citations and real file editing.',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  themeColor: [
    { media: '(prefers-color-scheme: light)', color: '#f8fafc' },
    { media: '(prefers-color-scheme: dark)', color: '#09090b' },
  ],
};

const themeScript = `(function(){try{var stored=localStorage.getItem('afw-theme');var dark=window.matchMedia('(prefers-color-scheme: dark)').matches;if(stored==='dark'||(!stored&&dark)){document.documentElement.classList.add('dark');}}catch(e){}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="h-full">
        <AppProviders>{children}</AppProviders>
      </body>
    </html>
  );
}

