import * as React from 'react'
import { ArrowSquareOutIcon } from '@phosphor-icons/react'
import { cn } from '../../utils/cn'

const linkClass = cn(
  'inline-flex items-baseline gap-1 rounded-sm font-medium text-brand',
  'underline decoration-brand/35 underline-offset-[3px]',
  'transition-[color,text-decoration-color] duration-feedback ease-hologram',
  'hover:text-brand-fg hover:decoration-brand/70',
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/60 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas',
  'disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:text-brand disabled:hover:decoration-brand/35',
  'motion-reduce:transition-none',
)

export type TextLinkProps = {
  children: React.ReactNode
  className?: string
  /** Shows a trailing external-affordance icon. */
  external?: boolean
} & (
  | (Omit<React.AnchorHTMLAttributes<HTMLAnchorElement>, 'children' | 'className'> & {
      href: string
    })
  | (Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'children' | 'className' | 'type'> & {
      href?: undefined
      type?: 'button' | 'submit' | 'reset'
    })
)

/**
 * Inline text link that flows with surrounding copy.
 * Use for secondary navigation / open-in-browser actions — not primary CTAs (`Button`).
 */
export const TextLink = React.forwardRef<HTMLAnchorElement | HTMLButtonElement, TextLinkProps>(
  ({ children, className, external = false, ...props }, ref) => {
    const content = (
      <>
        <span>{children}</span>
        {external && (
          <ArrowSquareOutIcon
            size={11}
            weight="bold"
            className="relative top-[0.05em] shrink-0 opacity-70"
            aria-hidden
          />
        )}
      </>
    )

    if ('href' in props && props.href != null) {
      const { href, ...anchorProps } = props
      return (
        <a
          ref={ref as React.Ref<HTMLAnchorElement>}
          href={href}
          className={cn(linkClass, className)}
          {...(external ? { target: '_blank', rel: 'noreferrer' } : {})}
          {...anchorProps}
        >
          {content}
        </a>
      )
    }

    const { type = 'button', ...buttonProps } = props
    return (
      <button
        ref={ref as React.Ref<HTMLButtonElement>}
        type={type}
        className={cn(linkClass, className)}
        {...buttonProps}
      >
        {content}
      </button>
    )
  },
)
TextLink.displayName = 'TextLink'
