import React from 'react'
import { XIcon } from '@phosphor-icons/react'
import { cn } from '../../utils/cn'
import { Button } from './Button'
import { Divider } from './Divider'

export interface StatusBarWorkspaceHeaderProps {
  title: string
  titleId?: string
  subtitle?: string
  leading?: React.ReactNode
  trailing?: React.ReactNode
  onClose: () => void
  closeLabel?: string
  className?: string
}

/**
 * Shared chrome for StatusBar workspace surfaces.
 * Host owns outside-click / Escape dismiss; this header owns the explicit close.
 */
export const StatusBarWorkspaceHeader: React.FC<StatusBarWorkspaceHeaderProps> = ({
  title,
  titleId,
  subtitle,
  leading,
  trailing,
  onClose,
  closeLabel = 'Close',
  className,
}) => (
  <>
    <header
      className={cn(
        // Workspace content gutters match this inset (px-6).
        'flex shrink-0 flex-wrap items-center gap-3 px-6 py-3',
        className,
      )}
    >
      {leading}
      <div className="min-w-0 flex-1">
        <h2 id={titleId} className="type-title text-foreground">
          {title}
        </h2>
        {subtitle && (
          <p className="mt-1 hidden type-body text-foreground-muted sm:block">
            {subtitle}
          </p>
        )}
      </div>
      {trailing}
      <Button
        variant="ghost"
        color="action"
        size="icon"
        onClick={onClose}
        aria-label={closeLabel}
        icon={<XIcon size={16} weight="bold" />}
      />
    </header>
    <Divider variant="accented" className="mx-6" />
  </>
)
