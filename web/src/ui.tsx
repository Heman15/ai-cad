// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import {
  Box, ChartSpline, CircleDot, Cone, CornerDownRight, FlipHorizontal2, FoldVertical, Grid3x3, Layers, Monitor, Moon, Orbit, Package, PanelTop, PenLine,
  Radius, RotateCw, Spline, Sun, Triangle, TriangleRight, Waves, type LucideIcon,
} from 'lucide-react'

export const FEATURE_ICONS: Record<string, LucideIcon> = {
  sketch: PenLine, extrude: Box, revolve: RotateCw, sweep: Spline, loft: Layers, hole: CircleDot,
  fillet: Radius, chamfer: Triangle, shell: Package, thread: Waves,
  pattern_linear: Grid3x3, pattern_circular: Orbit, mirror: FlipHorizontal2,
  sheet_base: PanelTop, sheet_flange: CornerDownRight, sheet_hem: FoldVertical,
  draft: Cone, rib: TriangleRight, variable_fillet: ChartSpline,
}

/** Small isometric cube with one face (or none, for isometric) highlighted. */
export function ViewIcon({ face }: { face: 'front' | 'top' | 'right' | 'back' | 'bottom' | 'left' | 'iso' }) {
  const on = 'var(--accent)'
  const fill = (f: string) => (face === f || face === 'iso' ? on : 'none')
  const hidden = face === 'back' || face === 'bottom' || face === 'left'
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" aria-hidden="true">
      {hidden && <path d={{ back: 'M8 3 L20 3 L20 15 L8 15 Z', bottom: 'M4 19 L16 19 L20 15 L8 15 Z', left: 'M4 7 L8 3 L8 15 L4 19 Z' }[face]}
        fill={on} fillOpacity={0.45} strokeDasharray="2 2" />}
      <path d="M4 7 L16 7 L16 19 L4 19 Z" fill={fill('front')} fillOpacity={face === 'iso' ? 0.35 : 0.8} />
      <path d="M4 7 L8 3 L20 3 L16 7 Z" fill={fill('top')} fillOpacity={face === 'iso' ? 0.6 : 0.8} />
      <path d="M16 7 L20 3 L20 15 L16 19 Z" fill={fill('right')} fillOpacity={face === 'iso' ? 0.85 : 0.8} />
    </svg>
  )
}

type Theme = 'system' | 'light' | 'dark'
const KEY = 'ai-cad-theme'

function readTheme(): Theme {
  try { return (localStorage.getItem(KEY) as Theme) || 'system' } catch { return 'system' }
}
function applyTheme(t: Theme) {
  if (t === 'system') delete document.documentElement.dataset.theme
  else document.documentElement.dataset.theme = t
}
applyTheme(readTheme())

/** Cycles system → light → dark; remembered per browser/desktop profile. */
export function ThemeButton() {
  const [theme, setTheme] = useState<Theme>(readTheme)
  useEffect(() => {
    applyTheme(theme)
    try { localStorage.setItem(KEY, theme) } catch { /* storage unavailable */ }
  }, [theme])
  const next: Record<Theme, Theme> = { system: 'light', light: 'dark', dark: 'system' }
  const Icon = theme === 'light' ? Sun : theme === 'dark' ? Moon : Monitor
  return (
    <button className="icon-btn" title={`Theme: ${theme} (click to change)`} aria-label="Theme" onClick={() => setTheme(next[theme])}>
      <Icon size={16} />
    </button>
  )
}

/** Ribbon command button: large icon over a label. */
export function RibbonButton({ icon: Icon, label, onClick, disabled, on, title }: {
  icon: LucideIcon | React.FC; label: string; onClick: () => void; disabled?: boolean; on?: boolean; title?: string
}) {
  return (
    <button className={`rb ${on ? 'on' : ''}`} onClick={onClick} disabled={disabled} title={title ?? label} aria-pressed={on}>
      <Icon size={22} strokeWidth={1.6} />
      <span>{label}</span>
    </button>
  )
}

export function RibbonGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rgroup">
      <div className="rbtns">{children}</div>
      <div className="rlabel">{label}</div>
    </div>
  )
}
