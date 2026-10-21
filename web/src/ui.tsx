// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState, type RefObject } from 'react'
import {
  Box, ChartSpline, ChevronDown, CircleDot, Cone, CornerDownRight, Cuboid, FilePlus, FlipHorizontal2, FolderOpen, FoldVertical, Grid3x3, Layers,
  Maximize, Monitor, Moon, Orbit, Package, PanelTop, PenLine, Radius, Redo2, RotateCw, Save, SaveAll, Settings2, Spline, SquareDashed, Sun, Triangle,
  TriangleRight, Undo2, Waves, type LucideIcon,
} from 'lucide-react'
import type { DisplayStyle, StdView, ViewportHandle } from './Viewport'

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

export const VIEW_NAMES: Record<StdView, string> = { iso: 'Isometric', front: 'Front', back: 'Back', top: 'Top', bottom: 'Bottom', right: 'Right', left: 'Left' }
export const DISPLAY: [DisplayStyle, string, LucideIcon][] = [['shaded-edges', 'Shaded with edges', Box], ['shaded', 'Shaded', Cuboid], ['wireframe', 'Wireframe', SquareDashed]]

/** Dark title bar shared by the Part and Assembly workspaces: brand, workspace switch, quick access, document title. */
export function TitleBar({ modeSwitch, kind, title, unsaved, onNew, onOpen, onSave, onSaveAs, onUndo, onRedo, canSave, canUndo, canRedo, onSettings, children }: {
  modeSwitch: React.ReactNode; kind: string; title: string; unsaved: boolean
  onNew: () => void; onOpen: () => void; onSave: () => void; onSaveAs?: () => void; onUndo: () => void; onRedo: () => void
  canSave: boolean; canUndo: boolean; canRedo: boolean; onSettings: () => void; children?: React.ReactNode
}) {
  return (
    <header className="titlebar">
      <span className="brand"><span className="logo"><Cuboid size={16} /></span>AI CAD</span>
      {modeSwitch}
      <div className="qa">
        <button className="icon-btn" aria-label="New" title={`New ${kind.toLowerCase()}`} onClick={onNew}><FilePlus size={16} /></button>
        <button className="icon-btn" aria-label="Open" title="Open (Ctrl+O)" onClick={onOpen}><FolderOpen size={16} /></button>
        <button className="icon-btn" aria-label="Save" title="Save (Ctrl+S)" onClick={onSave} disabled={!canSave}><Save size={16} /></button>
        {onSaveAs && <button className="icon-btn" aria-label="Save as" title="Save as (Ctrl+Shift+S)" onClick={onSaveAs} disabled={!canSave}><SaveAll size={16} /></button>}
        <span className="qa-sep" />
        <button className="icon-btn" aria-label="Undo" title="Undo (Ctrl+Z)" onClick={onUndo} disabled={!canUndo}><Undo2 size={16} /></button>
        <button className="icon-btn" aria-label="Redo" title="Redo (Ctrl+Y)" onClick={onRedo} disabled={!canRedo}><Redo2 size={16} /></button>
        {children}
      </div>
      <span className="doc-title">{title}{unsaved ? ' *' : ''}<span className="muted"> · {kind}</span></span>
      <div className="qa qa-end">
        <ThemeButton />
        <button className="icon-btn" aria-label="AI settings" title="AI settings" onClick={onSettings}><Settings2 size={16} /></button>
      </div>
    </header>
  )
}

/** Heads-up view toolbar over the viewport: fit, standard views, display style; workspace tools go in children. */
export function ViewHud({ viewRef, display, setDisplay, children }: {
  viewRef: RefObject<ViewportHandle | null>; display: DisplayStyle; setDisplay: (d: DisplayStyle) => void; children?: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="hud" role="toolbar" aria-label="View">
      <button className="icon-btn" aria-label="Fit" title="Zoom to fit (F)" onClick={() => viewRef.current?.fit()}><Maximize size={16} /></button>
      {(['iso', 'front', 'top', 'right'] as StdView[]).map((v) => (
        <button key={v} className="icon-btn" aria-label={VIEW_NAMES[v]} title={`${VIEW_NAMES[v]} view`} onClick={() => viewRef.current?.view(v)}><ViewIcon face={v} /></button>
      ))}
      <div className="menu-anchor">
        <button className="icon-btn" aria-label="More views" title="Back, bottom, left" onClick={() => setOpen(!open)}><ChevronDown size={14} /></button>
        {open && (
          <div className="menu hud-menu" onMouseLeave={() => setOpen(false)}>
            {(['back', 'bottom', 'left'] as StdView[]).map((v) => (
              <button key={v} onClick={() => { viewRef.current?.view(v); setOpen(false) }}><ViewIcon face={v} /> {VIEW_NAMES[v]}</button>
            ))}
          </div>
        )}
      </div>
      <span className="hud-sep" />
      {DISPLAY.map(([d, label, Icon]) => (
        <button key={d} className={`icon-btn ${display === d ? 'on' : ''}`} aria-label={label} title={label} aria-pressed={display === d} onClick={() => setDisplay(d)}><Icon size={16} /></button>
      ))}
      {children}
    </div>
  )
}

/** HUD toggle button (selection filters, measure, …). */
export function HudButton({ icon: Icon, label, title, on, disabled, onClick }: {
  icon: LucideIcon; label: string; title?: string; on?: boolean; disabled?: boolean; onClick: () => void
}) {
  return (
    <button className={`icon-btn ${on ? 'on' : ''}`} aria-label={label} title={title ?? label} aria-pressed={!!on} disabled={disabled} onClick={onClick}><Icon size={16} /></button>
  )
}

/** Bottom status bar: last message, a context hint, a readout, units. */
export function StatusBar({ status, hint, readout }: { status: { ok: boolean; text: string } | null; hint: React.ReactNode; readout?: React.ReactNode }) {
  return (
    <footer className="statusbar">
      {status ? <span className={`status ${status.ok ? 'ok' : 'err'}`} title={status.text}>{status.text}</span> : <span className="status muted">Ready</span>}
      <span className="sb-mid muted">{hint}</span>
      {readout && <span className="readout">{readout}</span>}
      <span className="units" title="Units: millimetre, gram, second">MMGS</span>
    </footer>
  )
}
