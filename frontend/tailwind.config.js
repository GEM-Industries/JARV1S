/** @type {import('tailwindcss').Config} */

export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      // Semantic color utilities only. Palette primitives live in index.css
      // and are referenced through --color-* aliases — do not expose palette
      // names as Tailwind utilities.
      colors: {
        canvas: {
          DEFAULT: 'oklch(var(--color-canvas) / <alpha-value>)',
          sunken: 'oklch(var(--color-canvas-sunken) / <alpha-value>)',
        },
        surface: {
          DEFAULT: 'oklch(var(--color-surface) / <alpha-value>)',
          raised: 'oklch(var(--color-surface-raised) / <alpha-value>)',
          sunken: 'oklch(var(--color-surface-sunken) / <alpha-value>)',
          highlight: 'oklch(var(--color-surface-highlight) / <alpha-value>)',
        },
        outline: {
          DEFAULT: 'oklch(var(--color-outline) / <alpha-value>)',
          strong: 'oklch(var(--color-outline-strong) / <alpha-value>)',
          subtle: 'oklch(var(--color-outline-subtle) / <alpha-value>)',
        },
        foreground: {
          DEFAULT: 'oklch(var(--color-foreground) / <alpha-value>)',
          muted: 'oklch(var(--color-foreground-muted) / <alpha-value>)',
          subtle: 'oklch(var(--color-foreground-subtle) / <alpha-value>)',
          disabled: 'oklch(var(--color-foreground-disabled) / <alpha-value>)',
        },
        brand: {
          DEFAULT: 'oklch(var(--color-brand) / <alpha-value>)',
          fg: 'oklch(var(--color-brand-fg) / <alpha-value>)',
          output: 'oklch(var(--color-brand-output) / <alpha-value>)',
        },
        status: {
          success: 'oklch(var(--color-status-success) / <alpha-value>)',
          warning: 'oklch(var(--color-status-warning) / <alpha-value>)',
          danger: 'oklch(var(--color-status-danger) / <alpha-value>)',
          'danger-fg': 'oklch(var(--color-status-danger-fg) / <alpha-value>)',
        },
        hologram: {
          error: 'oklch(var(--color-hologram-error) / <alpha-value>)',
          'error-inner': 'oklch(var(--color-hologram-error-inner) / <alpha-value>)',
          inactive: 'oklch(var(--color-hologram-inactive) / <alpha-value>)',
          'inactive-inner': 'oklch(var(--color-hologram-inactive-inner) / <alpha-value>)',
        },
      },
      fontFamily: {
        display: ['"Space Grotesk"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        body: ['Camber', '"Space Grotesk"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      fontSize: {
        // Size + line-height primitives for semantic roles. Complete family and
        // weight combinations live in the .type-* utilities in index.css.
        display: ['2rem', { lineHeight: '2.5rem' }],              // 32/40
        title: ['1.5rem', { lineHeight: '2rem' }],                 // 24/32
        section: ['1.25rem', { lineHeight: '1.75rem' }],          // 20/28
        heading: ['1rem', { lineHeight: '1.5rem' }],              // 16/24
        body: ['0.875rem', { lineHeight: '1.25rem' }],            // 14/20
        'body-reading': ['1rem', { lineHeight: '1.5rem' }],       // 16/24
        label: ['0.875rem', { lineHeight: '1.25rem' }],           // 14/20
        'label-small': ['0.75rem', { lineHeight: '1rem' }],       // 12/16
        meta: ['0.75rem', { lineHeight: '1rem' }],                // 12/16
        fui: ['0.625rem', { lineHeight: '0.75rem' }],             // 10/12 ornamental
      },
      borderRadius: {
        control: 'var(--radius-control)', // 12px — inputs, chips, tiles
        panel: 'var(--radius-panel)',     // 16px — PanelSection, cards
        shell: 'var(--radius-shell)',     // 32px — Hologram / overlay shells
      },
      transitionDuration: {
        instant: '100ms',
        feedback: '200ms',
        transition: '300ms',
        400: '400ms',
      },
      boxShadow: {
        'glow-brand': '0 0 10px oklch(var(--color-brand) / 0.6), 0 0 20px oklch(var(--color-brand) / 0.25)',
        'glow-brand-tight': '0 0 4px oklch(var(--color-brand) / 0.8)',
        'glow-output': '0 0 10px oklch(var(--color-brand-output) / 0.6), 0 0 20px oklch(var(--color-brand-output) / 0.25)',
        'glow-success': '0 0 10px oklch(var(--color-status-success) / 0.6), 0 0 20px oklch(var(--color-status-success) / 0.25)',
        'glow-danger': '0 0 12px oklch(var(--color-status-danger) / 0.6)',
        'glow-soft': '0 0 15px oklch(var(--color-brand) / 0.1)',
        'hologram-inset': 'inset 0 0 0 2px oklch(var(--color-canvas)), inset 0 0 0 3px oklch(var(--color-surface))',
        'hologram-inset-warning': 'inset 0 0 0 2px oklch(var(--color-canvas)), inset 0 0 0 3px oklch(var(--color-status-warning) / 0.28)',
        'hologram-inset-error': 'inset 0 0 0 2px oklch(var(--color-canvas)), inset 0 0 0 3px oklch(var(--color-hologram-error-inner))',
        'hologram-inset-inactive': 'inset 0 0 0 2px oklch(var(--color-canvas)), inset 0 0 0 3px oklch(var(--color-hologram-inactive-inner))',
      },
      dropShadow: {
        'glow-brand': '0 0 10px oklch(var(--color-brand) / 0.5)',
        'glow-brand-intense': '0 0 8px oklch(var(--color-brand) / 0.6)',
        'glow-warning': '0 0 15px oklch(var(--color-status-warning) / 0.4)',
        'glow-output': '0 0 10px oklch(var(--color-brand-output) / 0.5)',
        'glow-success': '0 0 10px oklch(var(--color-status-success) / 0.5)',
      },
      transitionTimingFunction: {
        'hologram': 'cubic-bezier(0.16, 1, 0.3, 1)',
        'snappy-out': 'cubic-bezier(0.4, 0.0, 1, 1)',
        'snappy-in': 'cubic-bezier(0.0, 0.0, 0.2, 1)',
      },
      spacing: {
        'safe-top': 'var(--safe-area-top)',
        'safe-bottom': 'var(--safe-area-bottom)',
        'status-bar-inset': 'var(--status-bar-inset)',
        'status-nav': 'var(--status-nav-height)',
        'shell-gap': 'var(--shell-overlay-gap)',
        'shell-overlay': 'var(--shell-overlay-top)',
      },
      typography: ({ theme }) => ({
        DEFAULT: {
          css: {
            maxWidth: 'none',
          },
        },
        invert: {
          css: {
            '--tw-prose-body': theme('colors.foreground.muted'),
            '--tw-prose-headings': theme('colors.foreground.DEFAULT'),
            '--tw-prose-bold': theme('colors.foreground.DEFAULT'),
            '--tw-prose-links': theme('colors.brand.DEFAULT'),
            '--tw-prose-bullets': theme('colors.outline.DEFAULT'),
            '--tw-prose-counters': theme('colors.outline.DEFAULT'),
            '--tw-prose-quote-borders': 'oklch(var(--color-brand) / 0.3)',
            '--tw-prose-quotes': theme('colors.foreground.muted'),
            '--tw-prose-code': theme('colors.brand.DEFAULT'),
            color: theme('colors.foreground.muted'),
            p: {
              fontFamily: theme('fontFamily.body').join(', '),
              lineHeight: theme('lineHeight.relaxed'),
              marginTop: theme('spacing.2'),
              marginBottom: theme('spacing.2'),
            },
            'p:first-child': { marginTop: '0' },
            'p:last-child': { marginBottom: '0' },
            'ul, ol': {
              marginTop: theme('spacing.2'),
              marginBottom: theme('spacing.2'),
            },
            li: {
              marginTop: theme('spacing.0.5'),
              marginBottom: theme('spacing.0.5'),
            },
            h1: {
              fontFamily: theme('fontFamily.display').join(', '),
              fontWeight: '500',
              color: theme('colors.foreground.DEFAULT'),
            },
            h2: {
              fontFamily: theme('fontFamily.display').join(', '),
              fontWeight: '500',
              color: theme('colors.foreground.DEFAULT'),
            },
            h3: {
              fontFamily: theme('fontFamily.display').join(', '),
              fontWeight: '500',
              color: theme('colors.foreground.DEFAULT'),
            },
            strong: {
              fontWeight: '500',
              color: theme('colors.foreground.DEFAULT'),
            },
            code: {
              backgroundColor: 'oklch(var(--color-brand) / 0.1)',
              padding: '0.125rem 0.25rem',
              borderRadius: theme('borderRadius.DEFAULT'),
              fontSize: theme('fontSize.xs'),
              fontWeight: '400',
            },
            'code::before': { content: 'none' },
            'code::after': { content: 'none' },
            a: {
              color: theme('colors.brand.DEFAULT'),
              textDecoration: 'none',
              '&:hover': { textDecoration: 'underline' },
            },
          },
        },
      }),
    },
  },
  plugins: [
    require("tailwindcss-animate"),
    require("@tailwindcss/typography"),
  ],
}
