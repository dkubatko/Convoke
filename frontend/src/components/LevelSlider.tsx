import { KeyboardEvent, PointerEvent } from 'react'

/** A discrete lever: a line with N stops where one level is selected. Click or
    drag anywhere along the line to snap to the nearest stop. The chosen level's
    label shows underneath. */
export function LevelSlider({
  value,
  min,
  max,
  labels,
  onChange,
  disabled,
  ariaLabel,
}: {
  value: number
  min: number
  max: number
  labels: string[]
  onChange: (value: number) => void
  disabled?: boolean
  ariaLabel?: string
}) {
  const steps = Math.max(1, max - min + 1)
  const idx = Math.min(steps - 1, Math.max(0, value - min))
  const pos = (i: number) => (steps > 1 ? (i / (steps - 1)) * 100 : 0)

  const setFromX = (el: HTMLElement, clientX: number) => {
    const rect = el.getBoundingClientRect()
    const frac = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
    const next = min + Math.round(frac * (steps - 1))
    if (next !== value) onChange(next)
  }
  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (disabled) return
    e.currentTarget.setPointerCapture(e.pointerId)
    setFromX(e.currentTarget, e.clientX)
  }
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    if (disabled || !e.currentTarget.hasPointerCapture(e.pointerId)) return
    setFromX(e.currentTarget, e.clientX)
  }
  const onKey = (e: KeyboardEvent) => {
    if (disabled) return
    if (e.key === 'ArrowRight' || e.key === 'ArrowUp') {
      e.preventDefault()
      onChange(Math.min(max, value + 1))
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') {
      e.preventDefault()
      onChange(Math.max(min, value - 1))
    } else if (e.key === 'Home') {
      e.preventDefault()
      onChange(min)
    } else if (e.key === 'End') {
      e.preventDefault()
      onChange(max)
    }
  }

  return (
    <div className={`lever${disabled ? ' lever--disabled' : ''}`}>
      <div
        className="lever-track"
        role="slider"
        aria-label={ariaLabel}
        aria-valuemin={min}
        aria-valuemax={max}
        aria-valuenow={value}
        aria-valuetext={labels[idx]}
        tabIndex={disabled ? -1 : 0}
        onKeyDown={onKey}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
      >
        <span className="lever-rail" />
        <span className="lever-fill" style={{ width: `${pos(idx)}%` }} />
        {Array.from({ length: steps }, (_, i) => (
          <span
            key={i}
            className={`lever-stop${i <= idx ? ' lever-stop--filled' : ''}${
              i === idx ? ' lever-stop--on' : ''
            }`}
            style={{ left: `${pos(i)}%` }}
          />
        ))}
      </div>
      <div className="lever-caption">{labels[idx]}</div>
    </div>
  )
}
