export interface TaskTraceItem {
  kind: 'tool_call' | 'tool_result' | 'text' | 'reasoning' | 'ui' | 'artifact' | 'error' | string;
  ts: number;
  span_id?: string | null;
  parent_id?: string | null;
  tool?: string | null;
  code?: string | null;
  args_preview?: Record<string, unknown> | null;
  text_preview?: string | null;
  result_preview?: string | null;
  status?: 'running' | 'completed' | 'failed' | string | null;
}

export interface TraceFileRef {
  path: string;
  name: string;
}

export type WorkLogEntry =
  | { kind: 'reply'; key: string; text: string; thinking?: boolean }
  | { kind: 'batch'; key: string; action: string; files: TraceFileRef[]; detail?: string; failed?: boolean }
  | { kind: 'error'; key: string; text: string };

const PATH_KEYS = ['file_path', 'path', 'target_file'] as const;

const ACTION_BY_TOOL: Record<string, string> = {
  read: 'Read',
  readfile: 'Read',
  glob: 'Read',
  globtool: 'Read',
  grep: 'Search',
  grepsearch: 'Search',
  edit: 'Edited',
  write: 'Edited',
  strreplace: 'Edited',
  str_replace: 'Edited',
  strreplacebasededittool: 'Edited',
  multiedit: 'Edited',
  bash: 'Ran',
  shell: 'Ran',
};

const fileNameFromPath = (path: string): string => path.split('/').filter(Boolean).pop() || path;

const joinChunks = (left: string, right: string): string => {
  if (!left) return right;
  if (!right) return left;
  const a = left.slice(-1);
  const b = right[0];
  if (/\s/.test(a) || /\s/.test(b) || '[({/'.includes(a) || ',.;:!?)]}'.includes(b)) {
    return left + right;
  }
  return `${left} ${right}`;
};

const toolName = (item: TaskTraceItem): string => {
  const raw = (item.tool ?? '').trim();
  const colon = raw.indexOf(':');
  return (colon > 0 ? raw.slice(0, colon) : raw).trim().toLowerCase();
};

export const toolPath = (item: TaskTraceItem): string | null => {
  const args = item.args_preview;
  if (args) {
    for (const key of PATH_KEYS) {
      const value = args[key];
      if (typeof value === 'string' && value.trim()) return value.trim();
    }
  }
  const raw = (item.tool ?? '').trim();
  const colon = raw.indexOf(':');
  if (colon <= 0) return null;
  const rest = raw.slice(colon + 1).trim();
  if (!rest || rest === 'context') return null;
  if (rest.includes('/') || /\.\w+$/.test(rest)) return rest;
  return null;
};

const classifyAction = (item: TaskTraceItem): string => {
  const name = toolName(item).replace(/\s+/g, '');
  return ACTION_BY_TOOL[name] ?? (name ? name.charAt(0).toUpperCase() + name.slice(1) : 'Used');
};

const toolDetail = (item: TaskTraceItem): string | undefined => {
  if (toolPath(item)) return undefined;
  const raw = (item.tool ?? '').trim();
  const colon = raw.indexOf(':');
  if (colon > 0) return raw.slice(colon + 1).trim() || undefined;
  return raw || undefined;
};

const isToolKind = (kind: string): boolean => kind === 'tool_call' || kind === 'tool_result';
const isReplyKind = (kind: string): boolean => kind === 'text' || kind === 'reasoning';

const pushFile = (files: TraceFileRef[], path: string): void => {
  if (files.some((file) => file.path === path)) return;
  files.push({ path, name: fileNameFromPath(path) });
};

export const presentTaskTrace = (items: TaskTraceItem[]): WorkLogEntry[] => {
  const entries: WorkLogEntry[] = [];
  for (const [index, item] of items.entries()) {
    const prev = entries[entries.length - 1];
    if (isReplyKind(item.kind)) {
      const text = (item.text_preview ?? '').trim();
      if (!text) continue;
      if (prev?.kind === 'reply' && Boolean(prev.thinking) === (item.kind === 'reasoning')) {
        prev.text = joinChunks(prev.text, text);
        continue;
      }
      entries.push({
        kind: 'reply',
        key: `${item.kind}-${item.ts}-${index}`,
        text,
        thinking: item.kind === 'reasoning',
      });
      continue;
    }
    if (item.kind === 'error') {
      const text = (item.text_preview ?? item.result_preview ?? item.tool ?? 'Error').toString();
      entries.push({ kind: 'error', key: `error-${item.ts}-${index}`, text });
      continue;
    }
    if (item.kind === 'artifact') continue;
    if (!isToolKind(item.kind)) continue;

    const action = classifyAction(item);
    const path = toolPath(item);
    const detail = toolDetail(item);
    const failed = item.status === 'failed' || item.kind === 'error';
    if (prev?.kind === 'batch' && prev.action === action && !failed) {
      if (path) pushFile(prev.files, path);
      else if (detail && !prev.detail) prev.detail = detail;
      continue;
    }
    const files: TraceFileRef[] = [];
    if (path) pushFile(files, path);
    entries.push({
      kind: 'batch',
      key: `${item.kind}-${item.ts}-${index}`,
      action,
      files,
      detail,
      failed,
    });
  }
  return entries;
};

export const describeBatch = (entry: Extract<WorkLogEntry, { kind: 'batch' }>): string => {
  if (entry.files.length === 1) return `${entry.action} ${entry.files[0].name}`;
  if (entry.files.length > 1 && entry.files.length <= 3) {
    return `${entry.action} ${entry.files.map((file) => file.name).join(', ')}`;
  }
  if (entry.files.length > 3) return `${entry.action} ${entry.files.length} files`;
  if (entry.detail) return `${entry.action} ${entry.detail}`;
  return entry.action;
};
