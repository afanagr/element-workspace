import { copyFileSync, existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(here, '..');
const source = path.join(projectRoot, 'node_modules', 'pdfjs-dist', 'build', 'pdf.worker.min.mjs');
const targetDir = path.join(projectRoot, 'public');
const target = path.join(targetDir, 'pdf.worker.min.mjs');

if (!existsSync(source)) {
  console.warn('[pdfjs] worker bundle not found - PDF rendering will use the CDN fallback');
  process.exit(0);
}

mkdirSync(targetDir, { recursive: true });
copyFileSync(source, target);
console.log('[pdfjs] copied pdf.worker.min.mjs to public/');
