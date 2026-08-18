import React from 'react'
import { cn } from '../../utils/cn'

export interface PanelSectionProps extends React.HTMLAttributes<HTMLElement> {
  as?: 'section' | 'article' | 'div'
  selected?: boolean
}

export const PanelSection: React.FC<PanelSectionProps> = ({
  as: Component = 'section',
  selected = false,
  className,
  children,
  ...props
}) => (
  <Component
    className={cn(
      'rounded-panel border border-transparent bg-surface/10 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.025)]',
      selected && 'ui-surface-selected bg-brand/[0.07]',
      className,
    )}
    {...props}
  >
    {children}
  </Component>
)

export interface DataFieldProps {
  label: string
  value: React.ReactNode
  className?: string
}

export const DataField: React.FC<DataFieldProps> = ({ label, value, className }) => (
  <div className={cn('min-w-0', className)}>
    <dt className="type-meta text-foreground-subtle">{label}</dt>
    <dd className="mt-1 truncate type-body text-foreground">{value}</dd>
  </div>
)
