import React from 'react';

export type WidgetMode = 'hero' | 'compressed';

/**
 * Props that every widget in the system MUST handle.
 * These are injected by the WidgetWrapper at runtime.
 */
export interface BaseWidgetProps {
  mode: WidgetMode;
  widgetId: string;
}

/**
 * A definition of a widget's views and configuration.
 */
export interface WidgetDefinition<T = any> {
  Hero: React.FC<T & BaseWidgetProps>;
  getCompressedConfig: (data: T) => {
    icon?: React.ReactNode;
    label: string | number;
    labelVariant?: 'display' | 'mono';
    eyebrow?: string;
    subLabel?: string;
    variant?: 'default' | 'receipt';
    width?: 'square' | 'wide';
    indicator?: 'running' | 'warning' | 'success' | 'error';
  };
}

/**
 * A standardized Functional Component type for JARV1S widgets.
 * T is the generic data structure defined by the backend's UIEnvelope.
 */
export type WidgetFC<T = Record<string, any>> = React.FC<T & BaseWidgetProps>;

export interface TodoItem {
  id: string;
  text: string;
  completed: boolean;
  created_at?: number;
}

export interface TodoData {
  groupId: string;
  title: string;
  items: TodoItem[];
  progress: number;
}
