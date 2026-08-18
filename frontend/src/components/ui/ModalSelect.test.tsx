// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Modal } from './Modal'
import { Select } from './Select'
import { HolographicMenu, MenuItem } from './holographic-menu'

describe('Modal', () => {
  it('exposes a labelled dialog and closes on Escape', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()

    render(
      <Modal open onClose={onClose} labelId="dlg-title">
        <h2 id="dlg-title">Inspect</h2>
        <button type="button">Inside</button>
      </Modal>,
    )

    expect(screen.getByRole('dialog', { name: 'Inspect' })).toBeTruthy()
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalled()
  })
})

describe('Select', () => {
  it('preserves empty-string values used for system-default devices', () => {
    render(
      <Select
        aria-label="Microphone"
        value=""
        onChange={() => {}}
        options={[
          { value: '', label: 'Microphone · Default' },
          { value: 'usb', label: 'USB Mic' },
        ]}
      />,
    )

    expect(screen.getByRole('combobox', { name: 'Microphone' }).textContent).toContain('Microphone · Default')
  })

  it('selects an option via keyboard typeahead', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()

    render(
      <Select
        aria-label="Outcome"
        value="all"
        onChange={onChange}
        options={[
          { value: 'all', label: 'All' },
          { value: 'ok', label: 'Succeeded' },
          { value: 'fail', label: 'Failed' },
        ]}
      />,
    )

    await user.click(screen.getByRole('combobox', { name: 'Outcome' }))
    await user.keyboard('f')
    await user.keyboard('{Enter}')
    expect(onChange).toHaveBeenCalledWith('fail')
  })
})

describe('HolographicMenu', () => {
  it('moves focus with arrow keys and restores on Escape', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()

    render(
      <div>
        <button type="button">Trigger</button>
        <HolographicMenu onClose={onClose} aria-label="Actions">
          <MenuItem>Alpha</MenuItem>
          <MenuItem>Bravo</MenuItem>
          <MenuItem>Charlie</MenuItem>
        </HolographicMenu>
      </div>,
    )

    screen.getByRole('button', { name: 'Trigger' }).focus()
    const menu = await screen.findByRole('menu', { name: 'Actions' })
    expect(menu).toBeTruthy()

    await waitFor(() => {
      expect(document.activeElement?.textContent).toContain('Alpha')
    })

    await user.keyboard('{ArrowDown}')
    expect(document.activeElement?.textContent).toContain('Bravo')

    await user.keyboard('{Escape}')
    menu.dispatchEvent(new Event('transitionend', { bubbles: true }))
    // HolographicMenu listens for opacity transitionend specifically
    menu.dispatchEvent(new TransitionEvent('transitionend', { propertyName: 'opacity', bubbles: true }))
    await waitFor(() => {
      expect(onClose).toHaveBeenCalled()
    })
  })
})
