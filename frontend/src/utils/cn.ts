import { type ClassValue, clsx } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'

/**
 * Custom font-size tokens (`text-label`, etc.) share the `text-` prefix with
 * colors. tailwind-merge does not read Tailwind config, so unknown `text-*`
 * sizes are treated as colors and wipe real color classes. Register size
 * suffixes here when adding fontSize tokens. Custom colors need no config.
 * @see https://github.com/dcastil/tailwind-merge/blob/v3.4.0/docs/configuration.md
 */
const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      text: [
        'display',
        'title',
        'section',
        'heading',
        'body',
        'body-reading',
        'label',
        'label-small',
        'meta',
        'fui',
      ],
    },
  },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
