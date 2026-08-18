import { isDesktopApp } from '../runtime/clientSurface'
import { openExternalUrlViaHost } from '../runtime/desktopBridge'

const EXTERNAL_PROTOCOLS = /^(https?:|mailto:|tel:)/i

export function isExternalUrl(url: string): boolean {
  return EXTERNAL_PROTOCOLS.test(url.trim())
}

export async function openExternalUrl(url: string): Promise<void> {
  const target = url.trim()
  if (!isExternalUrl(target)) {
    return
  }

  if (isDesktopApp()) {
    await openExternalUrlViaHost(target)
    return
  }

  window.open(target, '_blank', 'noopener,noreferrer')
}

export function installExternalLinkHandler(): void {
  if (!isDesktopApp()) {
    return
  }

  document.addEventListener(
    'click',
    (event) => {
      const anchor = (event.target as Element | null)?.closest('a[href]')
      if (!(anchor instanceof HTMLAnchorElement)) {
        return
      }

      const href = anchor.getAttribute('href')
      if (!href || !isExternalUrl(href)) {
        return
      }

      event.preventDefault()
      void openExternalUrl(href).catch((error) => {
        console.error('Failed to open external URL', href, error)
      })
    },
    true,
  )
}
