import React, { useEffect, useMemo, useRef, useState } from 'react'
import { cn } from '../../../utils/cn'
import { MenuContext } from './context'
import { MenuItem, type MenuItemProps } from './MenuItem'

type MenuItemElement = React.ReactElement<MenuItemProps, typeof MenuItem>

function isMenuItem(node: React.ReactNode): node is MenuItemElement {
  return React.isValidElement(node) && node.type === MenuItem
}

export interface StatusBarMenuContentProps {
  children: React.ReactNode
  onClose: () => void
}

/**
 * Keyboard and focus behavior for action-oriented StatusBar surfaces.
 * Shell geometry and lifecycle belong to the surface hosting this content.
 */
export const StatusBarMenuContent: React.FC<StatusBarMenuContentProps> = ({
  children,
  onClose,
}) => {
  const [visible, setVisible] = useState(false)
  const [focusIndex, setFocusIndex] = useState(0)
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([])
  const typeaheadRef = useRef({ query: '', expiresAt: 0 })
  const didInitFocusRef = useRef(false)

  const childArray = useMemo(() => React.Children.toArray(children), [children])
  const menuItemIndexes = useMemo(() => {
    const indexes: number[] = []
    childArray.forEach((child, index) => {
      if (isMenuItem(child) && !child.props.disabled) indexes.push(index)
    })
    return indexes
  }, [childArray])

  const focusItem = (childIndex: number) => {
    setFocusIndex(childIndex)
    itemRefs.current[childIndex]?.focus()
  }

  useEffect(() => {
    const frame = requestAnimationFrame(() => setVisible(true))
    return () => cancelAnimationFrame(frame)
  }, [])

  useEffect(() => {
    if (!visible || didInitFocusRef.current || menuItemIndexes.length === 0) return
    didInitFocusRef.current = true
    const first = menuItemIndexes[0]
    setFocusIndex(first)
    itemRefs.current[first]?.focus()
  }, [menuItemIndexes, visible])

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (menuItemIndexes.length === 0) return

      const currentPosition = menuItemIndexes.indexOf(focusIndex)
      const moveFocus = (direction: 1 | -1) => {
        const nextPosition = currentPosition < 0
          ? (direction === 1 ? 0 : menuItemIndexes.length - 1)
          : (currentPosition + direction + menuItemIndexes.length) % menuItemIndexes.length
        focusItem(menuItemIndexes[nextPosition])
      }

      if (event.key === 'ArrowDown') {
        event.preventDefault()
        moveFocus(1)
      } else if (event.key === 'ArrowUp') {
        event.preventDefault()
        moveFocus(-1)
      } else if (event.key === 'Home') {
        event.preventDefault()
        focusItem(menuItemIndexes[0])
      } else if (event.key === 'End') {
        event.preventDefault()
        focusItem(menuItemIndexes[menuItemIndexes.length - 1])
      } else if (
        event.key.length === 1
        && !event.ctrlKey
        && !event.metaKey
        && !event.altKey
      ) {
        const now = Date.now()
        const query = now > typeaheadRef.current.expiresAt
          ? event.key.toLowerCase()
          : `${typeaheadRef.current.query}${event.key.toLowerCase()}`
        typeaheadRef.current = { query, expiresAt: now + 750 }

        const match = menuItemIndexes.find((index) => {
          const item = childArray[index]
          if (!isMenuItem(item)) return false
          return String(item.props.label ?? item.props.children ?? '')
            .toLowerCase()
            .startsWith(query)
        })
        if (match !== undefined) {
          event.preventDefault()
          focusItem(match)
        }
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [childArray, focusIndex, menuItemIndexes])

  return (
    <MenuContext.Provider value={{ onClose }}>
      <div className="flex flex-col gap-1">
        {childArray.map((child, index) => (
          <div
            key={React.isValidElement(child) && child.key != null ? String(child.key) : index}
            className={cn(
              'transition-[opacity,transform] motion-reduce:transition-none',
              visible
                ? 'translate-x-0 opacity-100 duration-transition ease-hologram'
                : 'translate-x-1 opacity-0 duration-instant ease-in motion-reduce:translate-x-0',
            )}
            style={{ transitionDelay: visible ? `${100 + index * 30}ms` : '0ms' }}
          >
            {isMenuItem(child)
              ? React.cloneElement(child, {
                  role: 'menuitem',
                  tabIndex: focusIndex === index ? 0 : -1,
                  ref: (node: HTMLButtonElement | null) => {
                    itemRefs.current[index] = node
                  },
                  onFocus: (event: React.FocusEvent<HTMLButtonElement>) => {
                    setFocusIndex(index)
                    child.props.onFocus?.(event)
                  },
                } as Partial<MenuItemProps>)
              : child}
          </div>
        ))}
      </div>
    </MenuContext.Provider>
  )
}
