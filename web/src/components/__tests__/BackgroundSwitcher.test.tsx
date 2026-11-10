import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import BackgroundSwitcher from '../BackgroundSwitcher'

test('choosing a preset sets both --bg and its own --glass density', () => {
  render(<BackgroundSwitcher label="Background" />)
  fireEvent.click(screen.getByRole('button', { name: 'Snow' }))
  const style = document.documentElement.style
  expect(style.getPropertyValue('--bg')).toContain('#9fb2d4')
  expect(style.getPropertyValue('--glass')).toBe('rgba(17,22,38,.78)')
  fireEvent.click(screen.getByRole('button', { name: 'Auto' }))
  expect(style.getPropertyValue('--glass')).toBe('rgba(17,22,38,.55)')
})
