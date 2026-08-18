import { describe, expect, it } from 'vitest'
import { cn } from './cn'

describe('cn / tailwind-merge theme tokens', () => {
  it('keeps semantic text color when paired with a custom font-size token', () => {
    expect(cn('text-brand bg-brand/10 h-11 px-6 text-label')).toBe(
      'text-brand bg-brand/10 h-11 px-6 text-label',
    )
    expect(cn('text-foreground-muted', 'text-label-small')).toBe(
      'text-foreground-muted text-label-small',
    )
  })
})
