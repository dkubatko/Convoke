import { KeyboardEvent, useEffect, useRef, useState } from 'react'

export type SelectOption = {
  value: string
  label: string
  hint?: string // muted trailing text on the same line (e.g. "· 512d · recommended")
  disabled?: boolean
}

/** App-styled dropdown replacing the native <select> — same control height as
    inputs/buttons, keyboard-navigable, closes on outside click or Escape. */
export function Select({
  value,
  options,
  onChange,
  disabled,
  placeholder,
  ariaLabel,
  mono,
}: {
  value: string
  options: SelectOption[]
  onChange: (value: string) => void
  disabled?: boolean
  placeholder?: string
  ariaLabel?: string
  mono?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)
  const root = useRef<HTMLDivElement>(null)
  const current = options.find((o) => o.value === value)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  const openMenu = () => {
    if (disabled) return
    setActive(Math.max(0, options.findIndex((o) => o.value === value)))
    setOpen(true)
  }
  const choose = (o: SelectOption) => {
    if (o.disabled) return
    onChange(o.value)
    setOpen(false)
  }

  const onKey = (e: KeyboardEvent) => {
    if (disabled) return
    if (!open) {
      if (['Enter', ' ', 'ArrowDown', 'ArrowUp'].includes(e.key)) {
        e.preventDefault()
        openMenu()
      }
      return
    }
    if (e.key === 'Escape' || e.key === 'Tab') setOpen(false)
    else if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((i) => Math.min(options.length - 1, i + 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((i) => Math.max(0, i - 1))
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      if (options[active]) choose(options[active])
    }
  }

  return (
    <div className={`select${open ? ' select--open' : ''}`} ref={root}>
      <button
        type="button"
        className={`select-btn${mono ? ' mono' : ''}`}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        onClick={() => (open ? setOpen(false) : openMenu())}
        onKeyDown={onKey}
      >
        <span className={`select-value${current ? '' : ' muted'}`}>
          {current ? current.label : placeholder ?? 'Select…'}
          {current?.hint && <span className="select-hint"> {current.hint}</span>}
        </span>
        <span className="select-caret" aria-hidden>
          ⌄
        </span>
      </button>
      {open && (
        <ul className={`select-menu${mono ? ' mono' : ''}`} role="listbox">
          {options.map((o, i) => (
            <li
              key={o.value}
              role="option"
              aria-selected={o.value === value}
              className={`select-opt${o.value === value ? ' select-opt--on' : ''}${
                i === active ? ' select-opt--active' : ''
              }${o.disabled ? ' select-opt--disabled' : ''}`}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => {
                e.preventDefault()
                choose(o)
              }}
            >
              <span className="select-opt-check" aria-hidden>
                {o.value === value ? '✓' : ''}
              </span>
              <span className="select-opt-label">
                {o.label}
                {o.hint && <span className="select-hint"> {o.hint}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
