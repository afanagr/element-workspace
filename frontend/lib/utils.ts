import type { WorkNode } from './types';

export function formatBytes(bytes: number): string {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** exponent;
  return `${value >= 10 || exponent === 0 ? Math.round(value) : value.toFixed(1)} ${units[exponent]}`;
}

export function fileGlyph(node: Pick<WorkNode, 'kind' | 'extension'>): string {
  if (node.kind === 'project') return '📁';
  if (node.kind === 'folder') return '📂';
  const ext = (node.extension || '').replace('.', '').toLowerCase();
  if (['xlsx', 'xls', 'xlsm', 'csv'].includes(ext)) return '📊';
  if (['docx', 'doc', 'rtf', 'odt'].includes(ext)) return '📝';
  if (ext === 'pdf') return '📕';
  if (['png', 'jpg', 'jpeg', 'webp', 'tiff', 'tif', 'bmp', 'gif'].includes(ext)) return '🖼️';
  if (['zip', 'gz', 'tar', '7z', 'rar'].includes(ext)) return '🗜️';
  if (['md', 'markdown', 'txt', 'log'].includes(ext)) return '📄';
  if (['json', 'yaml', 'yml', 'toml'].includes(ext)) return '🧾';
  return '📎';
}

export function indexStatusKey(status: string): string {
  switch (status) {
    case 'indexed':
      return 'index.indexed';
    case 'indexing':
      return 'index.indexing';
    case 'pending':
      return 'index.queued';
    case 'unsupported':
      return 'index.notIndexed';
    case 'failed':
      return 'index.failed';
    default:
      return 'index.unknown';
  }
}

export function indexStatusTone(status: string): string {
  switch (status) {
    case 'indexed':
      return 'text-emerald-600 dark:text-emerald-400';
    case 'indexing':
      return 'text-amber-600 dark:text-amber-400';
    case 'pending':
      return 'text-slate-500 dark:text-zinc-400';
    case 'unsupported':
      return 'text-slate-500 dark:text-zinc-500';
    case 'failed':
      return 'text-rose-600 dark:text-rose-400';
    default:
      return 'text-slate-500 dark:text-zinc-400';
  }
}

export function flatten(nodes: WorkNode[], out: WorkNode[] = []): WorkNode[] {
  for (const node of nodes) {
    out.push(node);
    if (node.children?.length) flatten(node.children, out);
  }
  return out;
}

export function findNode(nodes: WorkNode[], id: string): WorkNode | null {
  for (const node of nodes) {
    if (node.id === id) return node;
    const found = findNode(node.children || [], id);
    if (found) return found;
  }
  return null;
}

export function ancestorsOf(nodes: WorkNode[], id: string, trail: WorkNode[] = []): WorkNode[] {
  for (const node of nodes) {
    if (node.id === id) return trail;
    const found = ancestorsOf(node.children || [], id, [...trail, node]);
    if (found.length) return found;
  }
  return [];
}

export function projectOf(nodes: WorkNode[], id: string): WorkNode | null {
  const trail = ancestorsOf(nodes, id);
  return trail.find((node) => node.kind === 'project') ?? null;
}

export function relativeTime(iso?: string | null): string {
  if (!iso) return '';
  const date = new Date(iso);
  const diff = Date.now() - date.getTime();
  const minutes = Math.round(diff / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return date.toLocaleString();
}

export function truncate(text: string, max = 120): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

export function isTextExtension(ext: string): boolean {
  return [
    '.txt', '.md', '.markdown', '.json', '.jsonl', '.csv', '.yaml', '.yml', '.toml', '.ini',
    '.xml', '.html', '.htm', '.css', '.scss', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx',
    '.py', '.rb', '.go', '.rs', '.java', '.kt', '.c', '.h', '.cpp', '.hpp', '.cs', '.php',
    '.sh', '.bash', '.ps1', '.sql', '.graphql', '.vue', '.svelte', '.log', '.env',
  ].includes(ext.toLowerCase());
}
