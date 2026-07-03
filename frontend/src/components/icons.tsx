interface IconProps {
  size?: number
}

const stroke = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.7,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
}

export function IconGauge({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke} aria-hidden>
      <path d="M4 14a8 8 0 1 1 16 0" />
      <path d="M12 14l4-4" />
      <path d="M2.5 18h19" />
    </svg>
  )
}

export function IconPlane({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke} aria-hidden>
      <path d="M21 4L3 11.5l6 2.5 2.5 6L21 4z" />
      <path d="M9 14l12-10" />
    </svg>
  )
}

export function IconBubbles({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke} aria-hidden>
      <path d="M4 5h11v8H8l-4 3.5V5z" />
      <path d="M15 9h5v7h-2.5l-2.5 2.3V9z" />
    </svg>
  )
}

export function IconBolt({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke} aria-hidden>
      <path d="M13 2L5 13.5h5.5L10 22l8-11.5h-5.5L13 2z" />
    </svg>
  )
}

export function IconPlug({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke} aria-hidden>
      <path d="M9 3v5M15 3v5" />
      <path d="M6 8h12v3a6 6 0 0 1-6 6 6 6 0 0 1-6-6V8z" />
      <path d="M12 17v4" />
    </svg>
  )
}

export function IconChip({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke} aria-hidden>
      <rect x="6" y="6" width="12" height="12" rx="2" />
      <path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4" />
    </svg>
  )
}

export function IconSignOut({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke} aria-hidden>
      <path d="M14 4H5v16h9" />
      <path d="M10 12h11m0 0-3.5-3.5M21 12l-3.5 3.5" />
    </svg>
  )
}
