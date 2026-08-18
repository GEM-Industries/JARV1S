/**
 * Gate focus chrome to real keyboard navigation.
 * Native `:focus-visible` false-positives after menu `.focus()` / some click
 * paths; style only when `html[data-keyboard-nav]` is set.
 */
export function installKeyboardNavFocus(): void {
  const root = document.documentElement
  const focusKeys = new Set([
    'Tab',
    'ArrowUp',
    'ArrowDown',
    'ArrowLeft',
    'ArrowRight',
    'Home',
    'End',
  ])

  const clear = () => root.removeAttribute('data-keyboard-nav')

  window.addEventListener(
    'keydown',
    (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      if (focusKeys.has(event.key)) root.setAttribute('data-keyboard-nav', '')
    },
    true,
  )

  window.addEventListener('pointerdown', clear, true)
}
