import React, { lazy, Suspense } from 'react';
import { WidgetDefinition } from './types';

const WeatherWidget = lazy(() => import('./WeatherWidget').then(m => ({ default: m.WeatherWidget.Hero })));
const TodoWidget = lazy(() => import('./TodoWidget').then(m => ({ default: m.TodoWidget.Hero })));
const ConnectWidget = lazy(() => import('./ConnectWidget').then(m => ({ default: m.ConnectWidget.Hero })));
const OAuthWidget = lazy(() => import('./OAuthWidget').then(m => ({ default: m.OAuthWidget.Hero })));
const InboxWidget = lazy(() => import('./InboxWidget').then(m => ({ default: m.InboxWidget.Hero })));
const BackgroundTaskWidget = lazy(() => import('./BackgroundTaskWidget').then(m => ({ default: m.BackgroundTaskWidget.Hero })));
const ContentWidget = lazy(() => import('./ContentWidget').then(m => ({ default: m.ContentWidget.Hero })));
const PendingInputWidget = lazy(() => import('./PendingInputWidget').then(m => ({ default: m.PendingInputWidget.Hero })));

import { WeatherWidget as WeatherDef } from './WeatherWidget';
import { TodoWidget as TodoDef } from './TodoWidget';
import { ConnectWidget as ConnectDef } from './ConnectWidget';
import { OAuthWidget as OAuthDef } from './OAuthWidget';
import { InboxWidget as InboxDef } from './InboxWidget';
import { BackgroundTaskWidget as BackgroundTaskDef } from './BackgroundTaskWidget';
import { ContentWidget as ContentDef } from './ContentWidget';
import { PendingInputWidget as PendingInputDef } from './PendingInputWidget';

const definitionMap: Record<string, WidgetDefinition<any>> = {
  'WeatherWidget': WeatherDef,
  'TodoWidget': TodoDef,
  'ConnectWidget': ConnectDef,
  'OAuthWidget': OAuthDef,
  'InboxWidget': InboxDef,
  'BackgroundTaskWidget': BackgroundTaskDef,
  'ContentWidget': ContentDef,
  'PendingInputWidget': PendingInputDef,
};

const heroMap: Record<string, React.LazyExoticComponent<any>> = {
  'WeatherWidget': WeatherWidget,
  'TodoWidget': TodoWidget,
  'ConnectWidget': ConnectWidget,
  'OAuthWidget': OAuthWidget,
  'InboxWidget': InboxWidget,
  'BackgroundTaskWidget': BackgroundTaskWidget,
  'ContentWidget': ContentWidget,
  'PendingInputWidget': PendingInputWidget,
};

export const getWidgetDefinition = (componentName: string): WidgetDefinition<any> | null => {
  return definitionMap[componentName] || null;
};

export const WidgetLoader: React.FC<{ component: string, props: any }> = ({ component, props }) => {
  const Component = heroMap[component];
  
  if (!Component) {
    return <div className="text-red-500">Widget {component} not found</div>;
  }

  return (
    <Suspense fallback={
      <div className="h-full w-full animate-pulse bg-surface/50 rounded-[24px] flex items-center justify-center">
        <div className="w-8 h-8 rounded-full border-2 border-brand/30 border-t-brand animate-spin" />
      </div>
    }>
      <Component {...props} />
    </Suspense>
  );
};
