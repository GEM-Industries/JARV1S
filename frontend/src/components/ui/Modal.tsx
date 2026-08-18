import React from 'react'
import { Dialog } from '@base-ui/react/dialog'
import { cn } from '../../utils/cn'

export interface ModalProps {
  open: boolean
  onClose: () => void
  children: React.ReactNode
  className?: string
  /** Label element id for aria-labelledby */
  labelId?: string
  /** Optional description element id for aria-describedby */
  descriptionId?: string
}

/**
 * Accessible modal shell backed by Base UI Dialog.
 * Uses modal="trap-focus" so StatusBar destinations stay pointer-reachable
 * while focus remains trapped in the dialog. Escape and light-dismiss are
 * handled by Base UI. Does NOT render visual chrome — children supply the Hologram/card.
 *
 * Layering: chrome z-[65], backdrop z-60, popup z-70 (see FRONTEND_ARCHITECTURE §9).
 */
export const Modal: React.FC<ModalProps> = ({
  open,
  onClose,
  children,
  className,
  labelId,
  descriptionId,
}) => (
  <Dialog.Root
    open={open}
    modal="trap-focus"
    onOpenChange={(next) => {
      if (!next) onClose()
    }}
  >
    <Dialog.Portal>
      <Dialog.Backdrop
        className={cn(
          'fixed inset-0 z-[60] bg-transparent',
          'transition-opacity duration-feedback motion-reduce:transition-none',
          'data-starting-style:opacity-0 data-ending-style:opacity-0',
        )}
      />
      <Dialog.Viewport className="fixed inset-0 z-[70] flex items-center justify-center px-4 pt-safe-top pb-safe-bottom pointer-events-none">
        <Dialog.Popup
          aria-labelledby={labelId}
          aria-describedby={descriptionId}
          className={cn(
            'pointer-events-auto outline-none',
            'transition-[opacity,transform] duration-feedback ease-out motion-reduce:transition-none',
            'data-starting-style:opacity-0 data-starting-style:scale-[0.98]',
            'data-ending-style:opacity-0 data-ending-style:scale-[0.98]',
            className,
          )}
        >
          {children}
        </Dialog.Popup>
      </Dialog.Viewport>
    </Dialog.Portal>
  </Dialog.Root>
)
