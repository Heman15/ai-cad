// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, desktop, type Model, type Overlay, type SelRef, type Settings } from './api'
import Chat, { type ChatTrigger } from './Chat'
import { DrawingDialog, FlatPatternDialog, SourceDrawingPanel } from './Drawings'
import ErrorBoundary from './ErrorBoundary'
import FeatureDialog, { DIALOGS, type DialogKind } from './FeatureDialog'
import { FeatureProperties, FeatureTree, IntentPanel, MaterialPanel, SelectionPanel, Variables } from './panels'
import AnalysisPanel from './Analysis'
import AssemblyWorkspace from './Assembly'
import AnnotationsPanel from './Annotations'
import SettingsDialog from './Settings'
import SketchEditor from './SketchEditor'
import Viewport, { type DisplayStyle, type MeasureGraphic, type PickMode, type StdView, type ViewportHandle } from './Viewport'
import MeasurePanel from './Measure'
import { RibbonButton, RibbonGroup, ThemeButton, ViewIcon } from './ui'
import {
  Box, ChartSpline, ChevronDown, CircleDot, Cone, CornerDownRight, Cuboid, Download, Drill, FoldVertical, TriangleRight, Factory, FileImage, FilePlus, FileText, FlipHorizontal2, FolderOpen, Gauge,
  Grid3x3, Layers, LayoutTemplate, Maximize, Minus, MousePointer2, Orbit, Package, PanelTop, PenLine, PenTool, Radius, Redo2, RotateCw, Ruler,
  Save, SaveAll, Scale3d, Scan, Settings2, SlidersHorizontal, Sparkles, Spline, Square, SquareDashed, SquareMinus, Triangle, Undo2, Waves, Weight,
  type LucideIcon,
} from 'lucide-react'

export default function App() {
  const [workspace, setWorkspace] = useState<'part' | 'assembly'>('part')
  const modeSwitch = (
    <div className="group seg">
      <button className={workspace === 'part' ? 'on' : ''} onClick={() => setWorkspace('part')}>Part</button>
      <button className={workspace === 'assembly' ? 'on' : ''} onClick={() => setWorkspace('assembly')}>Assembly</button>
    </div>
  )
  if (workspace === 'assembly') return <AssemblyWorkspace modeSwitch={modeSwitch} />
  return <PartWorkspace modeSwitch={modeSwitch} />
}

function PartWorkspace({ modeSwitch }: { modeSwitch: React.ReactNode }) {
  const [model, setModelState] = useState<Model | null>(null)
  const [materials, setMaterials] = useState<Record<string, number>>({})
  const [settings, setSettings] = useState<Settings | null>(null)
  const [status, setStatus] = useState<{ ok: boolean; text: string } | null>(null)
  const [selection, setSelection] = useState<SelRef[]>([])
  const [selectedFeature, setSelectedFeature] = useState<string | null>(null)
  const [mode, setMode] = useState<PickMode>('feature')
  const [dialog, setDialog] = useState<DialogKind | null>(null)
  const [showSettings, setShowSettings] = useState(false)
  const [tab, setTab] = useState<RightTab>('assistant')
  const [overlay, setOverlay] = useState<Overlay | null>(null)
  const [markers, setMarkers] = useState<number[][]>([])
  const [chatTrigger, setChatTrigger] = useState<ChatTrigger | null>(null)
  const [showDrawing, setShowDrawing] = useState(false)
  const [showFlat, setShowFlat] = useState(false)
  const drawingInput = useRef<HTMLInputElement>(null)
  const [rtab, setRtab] = useState<RTab>('features')
  const [viewsOpen, setViewsOpen] = useState(false)
  const [display, setDisplay] = useState<DisplayStyle>('shaded-edges')
  const [showDims, setShowDims] = useState(false)
  const [measureSel, setMeasureSel] = useState<SelRef[]>([])
  const [measureGfx, setMeasureGfx] = useState<MeasureGraphic | null>(null)
  const [sketching, setSketching] = useState<{ editing: string | null } | null>(null)
  const viewRef = useRef<ViewportHandle>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const selRef = useRef(selection)
  selRef.current = selection

  // Every model update re-checks the selection: entities that no longer exist are dropped, never re-guessed.
  const setModel = useCallback(async (m: Model) => {
    setModelState((prev) => {
      if (prev && JSON.stringify(prev.spec.features) !== JSON.stringify(m.spec.features)) { setOverlay(null); setMarkers([]) } // results are stale
      return m
    })
    setSelectedFeature((f) => (f && m.spec.features.some((x) => x.id === f) ? f : null))
    const sel = selRef.current
    if (!sel.length) return
    if (!m.entities) { setSelection([]); return }
    const { items } = await api.resolve(sel)
    const kept: SelRef[] = []
    const lost: string[] = []
    items.forEach((r, i) => {
      if (r.status === 'ok' || r.status === 'changed') kept.push({ ...sel[i], ...r, kind: sel[i].kind } as SelRef)
      else lost.push(r.reason ?? 'selection no longer exists')
    })
    setSelection(kept)
    if (lost.length) setStatus({ ok: false, text: `Selection cleared: ${lost[0]}` })
  }, [])

  // Live sync: changes made outside this window (Claude Code via MCP, another client) appear immediately.
  const seen = useRef<string | null>(null)
  useEffect(() => {
    const t = setInterval(async () => {
      try {
        const v = await api.version()
        if (seen.current !== null && v.part !== seen.current) await setModel(await api.model())
        seen.current = v.part
      } catch { /* backend restarting */ }
    }, 1000)
    return () => clearInterval(t)
  }, [setModel])
  // Share the 3D selection so external assistants can resolve "this".
  useEffect(() => { api.shareSelection(selection).catch(() => {}) }, [selection])

  useEffect(() => {
    api.model().then(setModel).catch((e) => setStatus({ ok: false, text: `Backend not reachable: ${e.message}` }))
    api.info().then((i) => { setMaterials(i.materials); setSettings(i.ai) }).catch(() => {})
  }, [setModel])

  const apply = useCallback(async (p: Promise<{ result?: { ok: boolean; message: string }; model: Model }>) => {
    try {
      const r = await p
      await setModel(r.model)
      if (r.result) setStatus({ ok: r.result.ok, text: r.result.message })
      return r.result?.ok ?? true
    } catch (e) {
      setStatus({ ok: false, text: (e as Error).message })
      return false
    }
  }, [setModel])

  const run = useCallback((command: string, args: Record<string, unknown>) => apply(api.command(command, args)), [apply])
  const undo = useCallback(() => apply(api.undo()), [apply])
  const redo = useCallback(() => apply(api.redo()), [apply])

  const save = useCallback(async (saveAs = false) => {
    if (!model) return
    try {
      if (desktop) {
        let path = saveAs ? null : model.path
        if (!path) path = await desktop.saveProjectDialog(`${model.spec.name || 'part'}.aicad`)
        if (!path) return
        const r = await api.saveTo(path)
        await setModel(r.model)
        setStatus({ ok: true, text: `saved ${r.path}` })
      } else {
        const spec = await api.projectJson()
        const url = URL.createObjectURL(new Blob([JSON.stringify(spec, null, 2)], { type: 'application/json' }))
        Object.assign(document.createElement('a'), { href: url, download: `${model.spec.name || 'part'}.aicad` }).click()
        URL.revokeObjectURL(url)
        setStatus({ ok: true, text: 'project downloaded' })
      }
    } catch (e) {
      setStatus({ ok: false, text: (e as Error).message })
    }
  }, [model, setModel])

  const open = useCallback(async () => {
    if (desktop) {
      const path = await desktop.openProjectDialog()
      if (!path) return
      if (await apply(api.openFrom(path))) setStatus({ ok: true, text: `opened ${path}` })
    } else fileRef.current?.click()
    setSelection([])
  }, [apply])

  const exportFile = useCallback(async (fmt: 'step' | 'stl') => {
    if (!model?.entities) return
    if (desktop) {
      const path = await desktop.saveExportDialog(`${model.spec.name || 'part'}.${fmt}`, fmt)
      if (!path) return
      try {
        const r = await api.exportTo(fmt, path)
        setStatus({ ok: r.ok, text: r.message })
      } catch (e) {
        setStatus({ ok: false, text: (e as Error).message })
      }
    } else {
      Object.assign(document.createElement('a'), { href: `/api/export/${fmt}`, download: '' }).click()
    }
  }, [model])

  const importDrawing = useCallback(async (file: File) => {
    const types: Record<string, string> = { pdf: 'application/pdf', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp' }
    const media = file.type || types[file.name.split('.').pop()?.toLowerCase() ?? ''] || ''
    const buf = new Uint8Array(await file.arrayBuffer())
    let bin = ''
    for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000))
    setStatus({ ok: true, text: `reading ${file.name}…` })
    if (await apply(api.uploadDrawing(file.name, media, btoa(bin)))) setTab('drawing')
  }, [apply])

  // Keyboard shortcuts.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest('input, textarea, select')) return
      const mod = e.ctrlKey || e.metaKey
      if (mod && e.key === 'z') { e.preventDefault(); if (e.shiftKey) redo(); else undo() }
      else if (mod && e.key === 'y') { e.preventDefault(); redo() }
      else if (mod && e.key === 's') { e.preventDefault(); save(e.shiftKey) }
      else if (mod && e.key === 'o') { e.preventDefault(); open() }
      else if (e.key === 'Escape') {
        if (modeRef.current === 'measure') { if (measureRef.current.length) setMeasureSel([]); else { setMode('feature'); setMeasureGfx(null) } }
        else { setSelection([]); setSelectedFeature(null) }
      }
      else if (e.key === 'f') viewRef.current?.fit()
      else if (e.key === 'm' && !mod) { setMode((m) => (m === 'measure' ? 'feature' : 'measure')); setMeasureSel([]); setMeasureGfx(null) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [undo, redo, save, open])

  const modeRef = useRef(mode)
  modeRef.current = mode
  const measureRef = useRef(measureSel)
  measureRef.current = measureSel

  const onPick = useCallback((ref: SelRef | null, additive: boolean) => {
    if (mode === 'measure') {
      // Measure keeps its own picks: no Shift needed, a third pick starts a new measurement.
      setMeasureSel((s) => {
        if (!ref) return []
        if (s.some((x) => x.kind === ref.kind && x.index === ref.index)) return s.filter((x) => !(x.kind === ref.kind && x.index === ref.index))
        return s.length >= 2 ? [ref] : [...s, ref]
      })
      return
    }
    if (!ref) {
      if (!additive) { setSelection([]); setSelectedFeature(null) }
      return
    }
    setSelectedFeature(ref.feature)
    setSelection((s) => {
      if (mode === 'feature' || !additive) return [ref]
      const exists = s.some((x) => x.kind === ref.kind && x.index === ref.index)
      return exists ? s.filter((x) => !(x.kind === ref.kind && x.index === ref.index)) : [...s, ref]
    })
  }, [mode])

  const hasSolid = !!model?.entities
  const feature = model?.spec.features.find((f) => f.id === selectedFeature) ?? null
  const dialogOf = (k: DialogKind) => DIALOGS.find((d) => d.kind === k)!
  const cmd = (k: DialogKind, label: string, icon: LucideIcon) => (
    <RibbonButton icon={icon} label={label} title={dialogOf(k).label} disabled={dialogOf(k).needsSolid && !hasSolid} onClick={() => setDialog(k)} />
  )
  const measuring = mode === 'measure'
  const setPick = (m: PickMode) => { setMode(m); if (m !== 'measure') { setMeasureSel([]); setMeasureGfx(null) } }

  return (
    <div className={`app part ${tab === 'drawing' || tab === 'analysis' ? 'wide-right' : ''}`}>
      <header className="titlebar">
        <span className="brand"><span className="logo"><Cuboid size={16} /></span>AI CAD</span>
        {modeSwitch}
        <div className="qa">
          <button className="icon-btn" aria-label="New" title="New part" onClick={() => { apply(api.newProject()); setSelection([]) }}><FilePlus size={16} /></button>
          <button className="icon-btn" aria-label="Open" title="Open (Ctrl+O)" onClick={open}><FolderOpen size={16} /></button>
          <button className="icon-btn" aria-label="Save" title="Save (Ctrl+S)" onClick={() => save(false)} disabled={!model}><Save size={16} /></button>
          <button className="icon-btn" aria-label="Save as" title="Save as (Ctrl+Shift+S)" onClick={() => save(true)} disabled={!model}><SaveAll size={16} /></button>
          <span className="qa-sep" />
          <button className="icon-btn" aria-label="Undo" title="Undo (Ctrl+Z)" onClick={undo} disabled={!model?.can_undo}><Undo2 size={16} /></button>
          <button className="icon-btn" aria-label="Redo" title="Redo (Ctrl+Y)" onClick={redo} disabled={!model?.can_redo}><Redo2 size={16} /></button>
          <input ref={fileRef} type="file" accept=".aicad,.json" hidden onChange={async (e) => {
            const f = e.target.files?.[0]
            e.target.value = ''
            if (!f) return
            try { await apply(api.uploadProject(JSON.parse(await f.text()))); setStatus({ ok: true, text: `opened ${f.name}` }) }
            catch (err) { setStatus({ ok: false, text: `could not open ${f.name}: ${(err as Error).message}` }) }
          }} />
        </div>
        <span className="doc-title">{model?.spec.name || 'Untitled'}{model?.path ? '' : ' *'}<span className="muted"> · Part</span></span>
        <div className="qa qa-end">
          <ThemeButton />
          <button className="icon-btn" aria-label="AI settings" title="AI settings" onClick={() => setShowSettings(true)}><Settings2 size={16} /></button>
        </div>
      </header>

      <nav className="ribbon">
        <div className="rtabs" role="tablist">
          {RTABS.map(([k, label]) => (
            <button key={k} role="tab" aria-selected={rtab === k} className={rtab === k ? 'on' : ''} onClick={() => setRtab(k)}>{label}</button>
          ))}
        </div>
        <div className="rpanel">
          {rtab === 'features' && <>
            <RibbonGroup label="Start">
              <RibbonButton icon={PenLine} label="New Sketch" title="Constraint sketch on a plane or selected face" onClick={() => setSketching({ editing: null })} />
              {cmd('template', 'Template', LayoutTemplate)}
            </RibbonGroup>
            <RibbonGroup label="Boss / Base">
              {cmd('extrude', 'Extrude', Box)}{cmd('revolve', 'Revolve', RotateCw)}{cmd('sweep', 'Sweep', Spline)}{cmd('loft', 'Loft', Layers)}
            </RibbonGroup>
            <RibbonGroup label="Cut">
              {cmd('cut', 'Extruded Cut', SquareMinus)}{cmd('hole', 'Hole', CircleDot)}{cmd('hole_wizard', 'Hole Wizard', Drill)}
            </RibbonGroup>
            <RibbonGroup label="Modify">
              {cmd('fillet', 'Fillet', Radius)}{cmd('variable_fillet', 'Variable Fillet', ChartSpline)}{cmd('chamfer', 'Chamfer', Triangle)}
              {cmd('shell', 'Shell', Package)}{cmd('draft', 'Draft', Cone)}{cmd('rib', 'Rib', TriangleRight)}{cmd('thread', 'Thread', Waves)}
            </RibbonGroup>
            <RibbonGroup label="Pattern">
              {cmd('pattern_linear', 'Linear Pattern', Grid3x3)}{cmd('pattern_circular', 'Circular Pattern', Orbit)}{cmd('mirror', 'Mirror', FlipHorizontal2)}
            </RibbonGroup>
          </>}
          {rtab === 'sketch' && <>
            <RibbonGroup label="Sketch">
              <RibbonButton icon={PenLine} label="New Sketch" title="Constraint sketch on a plane or selected face" onClick={() => setSketching({ editing: null })} />
              <RibbonButton icon={PenTool} label="Edit Sketch" disabled={feature?.type !== 'sketch'} title="Select a sketch in the tree first"
                onClick={() => feature && setSketching({ editing: feature.id })} />
            </RibbonGroup>
            <div className="rnote">Lines, arcs, circles and splines with 13 constraint types and driving dimensions. Sketch on the XY/YZ/XZ plane, an offset or inclined plane, or a selected face.</div>
          </>}
          {rtab === 'evaluate' && <>
            <RibbonGroup label="Measure">
              <RibbonButton icon={Ruler} label="Measure" on={measuring} title="Measure distances, angles, diameters and areas (M)" onClick={() => setPick(measuring ? 'feature' : 'measure')} disabled={!hasSolid} />
              <RibbonButton icon={Scale3d} label="Dimensions" on={showDims} title="Show the overall size of the part in 3D" onClick={() => setShowDims(!showDims)} disabled={!hasSolid} />
            </RibbonGroup>
            <RibbonGroup label="Properties">
              <RibbonButton icon={Weight} label="Mass Properties" title="Material, mass and volume" onClick={() => setTab('properties')} disabled={!hasSolid} />
            </RibbonGroup>
            <RibbonGroup label="Analysis">
              <RibbonButton icon={Factory} label="Manufacturability" title="CNC, 3D printing and sheet-metal checks" onClick={() => setTab('analysis')} disabled={!hasSolid} />
              <RibbonButton icon={Gauge} label="Stress (FEA)" title="Linear static stress analysis" onClick={() => setTab('analysis')} disabled={!hasSolid} />
            </RibbonGroup>
          </>}
          {rtab === 'sheet' && <>
            <RibbonGroup label="Sheet Metal">
              {cmd('sheet_setup', 'Parameters', SlidersHorizontal)}{cmd('sheet_base', 'Base Flange', PanelTop)}{cmd('sheet_flange', 'Edge Flange', CornerDownRight)}{cmd('sheet_hem', 'Hem', FoldVertical)}
              <RibbonButton icon={Scan} label="Flat Pattern" disabled={!model?.sheet} title={model?.sheet ? 'Unfolded blank with bend lines' : 'Add a base flange first'} onClick={() => setShowFlat(true)} />
            </RibbonGroup>
          </>}
          {rtab === 'io' && <>
            <RibbonGroup label="Drawings">
              <RibbonButton icon={FileImage} label="Import Drawing" title="Import a PDF or image drawing and turn it into CAD" onClick={() => drawingInput.current?.click()} />
              <RibbonButton icon={FileText} label="2D Drawing" title="Generate a dimensioned 2D drawing" disabled={!hasSolid} onClick={() => setShowDrawing(true)} />
            </RibbonGroup>
            <RibbonGroup label="Export">
              <RibbonButton icon={Download} label="Export STEP" disabled={!hasSolid} onClick={() => exportFile('step')} />
              <RibbonButton icon={Download} label="Export STL" disabled={!hasSolid} onClick={() => exportFile('stl')} />
            </RibbonGroup>
          </>}
          <input ref={drawingInput} type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" hidden
            onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) importDrawing(f) }} />
        </div>
      </nav>

      <aside className="left">
        {model && (
          <>
            <div className="fm-head">FeatureManager</div>
            <div className="part-name">
              <Cuboid size={15} className="accent" />
              <input value={model.spec.name} aria-label="Part name" onChange={(e) => setModelState({ ...model, spec: { ...model.spec, name: e.target.value } })}
                onBlur={(e) => run('set_name', { name: e.target.value })} />
              {model.path && <span className="muted small" title={model.path}>{model.path.split('/').pop()}</span>}
            </div>
            {hasSolid && (
              <button className="tree-row" onClick={() => setTab('properties')} title="Material and mass">
                <Weight size={14} /> {model.spec.material?.name ?? <span className="muted">Material &lt;not specified&gt;</span>}
              </button>
            )}
            {!hasSolid && <button className="primary wide" onClick={() => setDialog('template')}>Create part…</button>}
            <FeatureTree model={model} selected={selectedFeature} run={run}
              onSelect={(id) => { setSelectedFeature(id); setSelection([]); if (id) setTab('properties') }} />
            <Variables model={model} run={run} />
          </>
        )}
      </aside>

      <main className="center">
        <ErrorBoundary name="3D view">
          <Viewport ref={viewRef} model={model} mode={mode} selection={measuring ? measureSel : selection} selectedFeature={selectedFeature} onPick={onPick}
            overlay={overlay} markers={markers} display={display} showDims={showDims} measure={measuring ? measureGfx : null} />
        </ErrorBoundary>
        <div className="hud" role="toolbar" aria-label="View">
          <button className="icon-btn" aria-label="Fit" title="Zoom to fit (F)" onClick={() => viewRef.current?.fit()}><Maximize size={16} /></button>
          {(['iso', 'front', 'top', 'right'] as StdView[]).map((v) => (
            <button key={v} className="icon-btn" aria-label={VIEW_NAMES[v]} title={`${VIEW_NAMES[v]} view`} onClick={() => viewRef.current?.view(v)}><ViewIcon face={v} /></button>
          ))}
          <div className="menu-anchor">
            <button className="icon-btn" aria-label="More views" title="Back, bottom, left" onClick={() => setViewsOpen(!viewsOpen)}><ChevronDown size={14} /></button>
            {viewsOpen && (
              <div className="menu hud-menu" onMouseLeave={() => setViewsOpen(false)}>
                {(['back', 'bottom', 'left'] as StdView[]).map((v) => (
                  <button key={v} onClick={() => { viewRef.current?.view(v); setViewsOpen(false) }}><ViewIcon face={v} /> {VIEW_NAMES[v]}</button>
                ))}
              </div>
            )}
          </div>
          <span className="hud-sep" />
          {DISPLAY.map(([d, label, Icon]) => (
            <button key={d} className={`icon-btn ${display === d ? 'on' : ''}`} aria-label={label} title={label} aria-pressed={display === d} onClick={() => setDisplay(d)}><Icon size={16} /></button>
          ))}
          <span className="hud-sep" />
          {PICKS.map(([m, label, Icon]) => (
            <button key={m} className={`icon-btn ${mode === m ? 'on' : ''}`} aria-label={label} title={`Select ${label.toLowerCase()}s`} aria-pressed={mode === m} onClick={() => setPick(m)}><Icon size={16} /></button>
          ))}
          <span className="hud-sep" />
          <button className={`icon-btn ${measuring ? 'on' : ''}`} aria-label="Measure tool" title="Measure (M)" aria-pressed={measuring} disabled={!hasSolid}
            onClick={() => setPick(measuring ? 'feature' : 'measure')}><Ruler size={16} /></button>
          <button className={`icon-btn ${showDims ? 'on' : ''}`} aria-label="Show dimensions" title="Overall dimensions" aria-pressed={showDims} disabled={!hasSolid}
            onClick={() => setShowDims(!showDims)}><Scale3d size={16} /></button>
        </div>
        {measuring && model && (
          <MeasurePanel selection={measureSel} model={model} onGraphic={setMeasureGfx}
            onClear={() => setMeasureSel([])} onClose={() => setPick('feature')} />
        )}
      </main>

      <aside className="right">
        <div className="tabs">
          {RIGHT_TABS.map(([k, label, Icon]) => (
            <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}><Icon size={14} /> {label}</button>
          ))}
        </div>
        <div className={`tab-body ${tab === 'assistant' ? '' : 'hidden'}`}>
          <Chat selection={selection} onModel={setModel} aiReady={!!settings?.ready} openSettings={() => setShowSettings(true)} trigger={chatTrigger} />
        </div>
        {model && tab === 'properties' && (
          <div className="tab-body scroll">
            <SelectionPanel selection={selection} model={model} onClear={() => setSelection([])} />
            {feature?.type === 'sketch' && (
              <button className="wide" onClick={() => setSketching({ editing: feature.id })}>Edit sketch…</button>
            )}
            {feature && <FeatureProperties feature={feature} model={model} run={run} />}
            {hasSolid && <MaterialPanel model={model} materials={materials} run={run} />}
            {hasSolid && <AnnotationsPanel model={model} selection={selection} selectedFeature={selectedFeature} run={run} />}
            <IntentPanel model={model} />
          </div>
        )}
        {model && tab === 'analysis' && (
          <div className="tab-body scroll">
            {hasSolid ? <AnalysisPanel model={model} selection={selection} run={run} setOverlay={setOverlay} setMarkers={setMarkers} />
              : <p className="muted">Create a part first.</p>}
          </div>
        )}
        {model && tab === 'drawing' && (
          <div className="tab-body scroll">
            <SourceDrawingPanel model={model} run={run} aiReady={!!settings?.ready}
              onImport={() => drawingInput.current?.click()}
              onInterpret={() => { setTab('assistant'); setChatTrigger({ nonce: Date.now(), text: '', interpret: true }) }} />
          </div>
        )}
      </aside>

      <footer className="statusbar">
        {status ? <span className={`status ${status.ok ? 'ok' : 'err'}`} title={status.text}>{status.text}</span> : <span className="status muted">Ready</span>}
        <span className="sb-mid muted">
          {measuring ? 'Measure: click points, edges or faces · Esc clears'
            : selection.length ? `${selection.length} ${selection[0].kind}${selection.length > 1 ? 's' : ''} selected · Shift-click to add`
            : selectedFeature ? <>Feature <b>{selectedFeature}</b></> : `Click selects ${mode}s · drag rotates · right-drag pans · wheel zooms`}
        </span>
        {model?.bbox_mm && (
          <span className="readout">
            {model.bbox_mm.map((v) => v.toFixed(1)).join(' × ')} mm · {model.volume_mm3!.toFixed(1)} mm³
            {model.mass_kg !== null && ` · ${(model.mass_kg * 1000).toFixed(2)} g`}
          </span>
        )}
        <span className="units" title="Units: millimetre, gram, second">MMGS</span>
      </footer>

      {showFlat && model && <FlatPatternDialog model={model} onClose={() => setShowFlat(false)} setStatus={setStatus} />}
      {showDrawing && model && <DrawingDialog model={model} onClose={() => setShowDrawing(false)} setStatus={setStatus} />}
      {dialog && <FeatureDialog kind={dialog} model={model} selection={selection} run={run} onClose={() => setDialog(null)} />}
      {sketching && (
        <SketchEditor model={model} selection={selection} run={run}
          editing={sketching.editing ? model?.spec.features.find((f) => f.id === sketching.editing) ?? null : null}
          apply={(commands, label) => apply(api.batch(commands, label))}
          onClose={() => setSketching(null)} />
      )}
      {showSettings && settings && <SettingsDialog settings={settings} onSaved={setSettings} onClose={() => setShowSettings(false)} />}
    </div>
  )
}

type RTab = 'features' | 'sketch' | 'evaluate' | 'sheet' | 'io'
const RTABS: [RTab, string][] = [['features', 'Features'], ['sketch', 'Sketch'], ['evaluate', 'Evaluate'], ['sheet', 'Sheet Metal'], ['io', 'Import / Export']]
const VIEW_NAMES: Record<StdView, string> = { iso: 'Isometric', front: 'Front', back: 'Back', top: 'Top', bottom: 'Bottom', right: 'Right', left: 'Left' }
const DISPLAY: [DisplayStyle, string, LucideIcon][] = [['shaded-edges', 'Shaded with edges', Box], ['shaded', 'Shaded', Cuboid], ['wireframe', 'Wireframe', SquareDashed]]
const PICKS: [PickMode, string, LucideIcon][] = [['feature', 'Feature', MousePointer2], ['face', 'Face', Square], ['edge', 'Edge', Minus]]
type RightTab = 'assistant' | 'properties' | 'drawing' | 'analysis'
const RIGHT_TABS: [RightTab, string, LucideIcon][] = [['assistant', 'Assistant', Sparkles], ['properties', 'Properties', SlidersHorizontal], ['drawing', 'Drawing', FileImage], ['analysis', 'Analysis', Gauge]]
