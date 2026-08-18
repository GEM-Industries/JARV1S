export interface MarkdownSection {
  type: 'markdown';
  content: string;
}

export interface TableSection {
  type: 'table';
  headers: string[];
  rows: string[][];
}

export interface ListSection {
  type: 'list';
  items: string[];
  ordered?: boolean;
}

export interface CodeSection {
  type: 'code';
  language?: string;
  content: string;
}

export interface KVSection {
  type: 'kv';
  pairs: Record<string, string>;
}

export interface MetricItem {
  label: string;
  value: string;
  percent?: number | null;
  status?: 'good' | 'warning' | 'critical';
  sublabel?: string;
}

export interface MetricSection {
  type: 'metric';
  items: MetricItem[];
}

export type ContentSection =
  | MarkdownSection
  | TableSection
  | ListSection
  | CodeSection
  | KVSection
  | MetricSection;

export interface ContentData {
  title: string;
  sections: ContentSection[];
  display?: 'content' | 'receipt';
  line?: string;
  sublabel?: string;
  receipt_kind?: string;
  ref_id?: string;
  status?: string;
  attention?: string;
  action?: Record<string, unknown>;
}
