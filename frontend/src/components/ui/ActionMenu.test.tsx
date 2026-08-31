// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ActionMenu } from './ActionMenu'

describe('ActionMenu', () => {
  it('opens from the overflow trigger and runs the chosen action', async () => {
    const user = userEvent.setup()
    const onCopy = vi.fn()

    render(
      <ActionMenu label="More actions for Bedroom">
        <ActionMenu.Item onClick={onCopy}>Copy address</ActionMenu.Item>
        <ActionMenu.Separator />
        <ActionMenu.Item tone="danger">Remove access</ActionMenu.Item>
      </ActionMenu>,
    )

    await user.click(screen.getByRole('button', { name: 'More actions for Bedroom' }))
    const copyItem = await screen.findByRole('menuitem', { name: 'Copy address' })
    await user.click(copyItem)
    expect(onCopy).toHaveBeenCalledTimes(1)

    await waitFor(() => {
      expect(screen.queryByRole('menuitem', { name: 'Copy address' })).toBeNull()
    })
  })
})
