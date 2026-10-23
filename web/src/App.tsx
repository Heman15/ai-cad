// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, desktop, fileBase64, MESH_EXT, CAD_EXT, type Model, type Overlay, type SelRef, type Settings } from './api'
import Chat, { type ChatTrigger } from './Chat'
import { DrawingDialog, FlatPatternDialog, SourceDrawingPanel } from './Drawings'
import ErrorBoundary from './ErrorBoundary'
import FeatureDialog, { DIALOGS, type DialogKind } from './FeatureDialog'
import { FeatureProperties, FeatureTree, IntentPanel, MaterialPanel, SelectionPanel, Variables } from './panels'
import AnalysisPanel from './Analysis'
import AssemblyWorkspace from './Assembly'
import ConvertDialog from './Convert'
import PrintWorkspace from './Print'
import AnnotationsPanel from './Annotations'
import SettingsDialog from './Settings'
import SketchEditor from './SketchEditor'
import Viewport, { type DisplayStyle, type MeasureGraphic, type PickMode, type ViewportHandle } from './Viewport'
import MeasurePanel from './Measure'
import { HudButton, RibbonButton, RibbonGroup, StatusBar, TitleBar, ViewHud } from './ui'
import {
  Box, ChartSpline, CircleDot, FolderInput, Import, Cone, CornerDownRight, Cuboid, Download, Drill, Factory, FileImage, FileText, FlipHorizontal2, FoldVertical, Gauge,
  Grid3x3, Layers, LayoutTemplate, Minus, MousePointer2, Orbit, Package, PanelTop, PenLine, PenTool, Radius, RotateCw, Ruler, Scale3d, Scan,
  Printer, Repeat, SlidersHorizontal, Sparkles, Spline, Square, SquareMinus, Triangle, TriangleRight, Waves, Weight,
  type LucideIcon,
} from 'lucide-react'

export default function App() {
  const [workspace, setWorkspace] = useState<'part' | 'assembly' | 'print'>('part')
  const [aiSettings, setAiSettings] = useState<Settings | null>(null)
  const modeSwitch = (
    <div className="group seg">
      <button className={workspace === 'part' ? 'on' : ''} onClick={() => setWorkspace('part')}>Part</button>
      <button className={workspace === 'assembly' ? 'on' : ''} onClick={() => setWorkspace('assembly')}>Assembly</button>
      <button className={workspace === 'print' ? 'on' : ''} onClick={() => setWorkspace('print')}>Print</button>
    </div>
  )
  if (workspace === 'assembly') return <AssemblyWorkspace modeSwitch={modeSwitch} />
  if (workspace === 'print') return (
    <>
      <PrintWorkspace modeSwitch={modeSwitch} onSettings={() => api.settings().then(setAiSettings).catch(() => {})} />
      {aiSettings && <SettingsDialog settings={aiSettings} onSaved={setAiSettings} onClose={() => setAiSettings(null)} />}
    </>
  )
  return <PartWorkspace modeSwitch={modeSwitch} onPrint={() => setWorkspace('print')} />
}

const IMPORT_EXT = [...MESH_EXT, ...CAD_EXT]

function PartWorkspace({ modeSwitch, onPrint }: { modeSwitch: React.ReactNode; onPrint: () => void }) {
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
  const [showConvert, setShowConvert] = useState(false)
  const drawingInput = useRef<HTMLInputElement>(null)
  const [rtab, setRtab] = useState<RTab>('features')
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

  // Opens an AI CAD part, or a mesh/CAD file as a new part with one imported body.
  const opened = useCallback(async (r: { model: Model; result?: { ok: boolean; message: string } }, name: string) => {
    await setModel(r.model)
    const big = r.model.bbox_mm ? Math.max(...r.model.bbox_mm) : 1
    const hint = r.result && big < 2 ? ' · very small: if the file is in metres or inches, change the body\'s units in Properties' : ''
    setStatus({ ok: true, text: r.result ? `${r.result.message}${hint}` : `opened ${name}` })
  }, [setModel])
  const open = useCallback(async () => {
    if (desktop) {
      const path = await desktop.openProjectDialog()
      if (!path) return
      try { await opened(await api.openFrom(path), path) } catch (e) { setStatus({ ok: false, text: (e as Error).message }) }
    } else fileRef.current?.click()
    setSelection([])
  }, [opened])

  const sendToPrint = useCallback(async () => {
    try {
      await api.toPrint()
      onPrint()
    } catch (e) { setStatus({ ok: false, text: (e as Error).message }) }
  }, [onPrint])

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

  const insertBody = useCallback(async () => {
    const path = desktop ? await desktop.openFileDialog('Insert a body from a file', [{ name: 'Meshes and CAD files', extensions: IMPORT_EXT }])
      : window.prompt('Path of the STL/STEP/… file on this computer')
    if (path) run('import_file', { file: path })
  }, [run])

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
      <TitleBar modeSwitch={modeSwitch} kind="Part" title={model?.spec.name || 'Untitled'} unsaved={!model?.path}
        onNew={() => { apply(api.newProject()); setSelection([]) }} onOpen={open} onSave={() => save(false)} onSaveAs={() => save(true)}
        onUndo={undo} onRedo={redo} canSave={!!model} canUndo={!!model?.can_undo} canRedo={!!model?.can_redo} onSettings={() => setShowSettings(true)}>
        <input ref={fileRef} type="file" accept={['.aicad', '.json', ...IMPORT_EXT.map((x) => `.${x}`)].join(',')} hidden onChange={async (e) => {
          const f = e.target.files?.[0]
          e.target.value = ''
          if (!f) return
          try {
            if (IMPORT_EXT.includes(f.name.split('.').pop()?.toLowerCase() ?? '')) {
              setStatus({ ok: true, text: `reading ${f.name}…` })
              await opened(await api.importUpload(f.name, await fileBase64(f)), f.name)
            } else { await apply(api.uploadProject(JSON.parse(await f.text()))); setStatus({ ok: true, text: `opened ${f.name}` }) }
          }
          catch (err) { setStatus({ ok: false, text: `could not open ${f.name}: ${(err as Error).message}` }) }
        }} />
      </TitleBar>

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
            <RibbonGroup label="Import">
              <RibbonButton icon={FolderInput} label="Open 3D File" title="Open STL, OBJ, 3MF, PLY, STEP, IGES or BREP as a part" onClick={open} />
              <RibbonButton icon={Import} label="Insert Body" title="Add a body from a mesh or CAD file to this part (cut or add to it afterwards)" onClick={insertBody} />
            </RibbonGroup>
            <RibbonGroup label="Export">
              <RibbonButton icon={Download} label="Export STEP" disabled={!hasSolid} onClick={() => exportFile('step')} />
              <RibbonButton icon={Download} label="Export STL" disabled={!hasSolid} onClick={() => exportFile('stl')} />
              <RibbonButton icon={Repeat} label="Convert to STL" title="Convert any STEP, IGES, OBJ, 3MF, part or assembly file to STL" onClick={() => setShowConvert(true)} />
            </RibbonGroup>
            <RibbonGroup label="3D Printing">
              <RibbonButton icon={Printer} label="3D Print" title="Slice this part for a 3D printer (printer, material, quality, G-code)" disabled={!hasSolid} onClick={sendToPrint} />
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
        <ViewHud viewRef={viewRef} display={display} setDisplay={setDisplay}>
          <span className="hud-sep" />
          {PICKS.map(([m, label, Icon]) => (
            <HudButton key={m} icon={Icon} label={label} title={`Select ${label.toLowerCase()}s`} on={mode === m} onClick={() => setPick(m)} />
          ))}
          <span className="hud-sep" />
          <HudButton icon={Ruler} label="Measure tool" title="Measure (M)" on={measuring} disabled={!hasSolid} onClick={() => setPick(measuring ? 'feature' : 'measure')} />
          <HudButton icon={Scale3d} label="Show dimensions" title="Overall dimensions" on={showDims} disabled={!hasSolid} onClick={() => setShowDims(!showDims)} />
        </ViewHud>
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

      <StatusBar status={status}
        hint={measuring ? 'Measure: click points, edges or faces · Esc clears'
          : selection.length ? `${selection.length} ${selection[0].kind}${selection.length > 1 ? 's' : ''} selected · Shift-click to add`
          : selectedFeature ? <>Feature <b>{selectedFeature}</b></> : `Click selects ${mode}s · drag rotates · right-drag pans · wheel zooms`}
        readout={model?.bbox_mm && <>
          {model.bbox_mm.map((v) => v.toFixed(1)).join(' × ')} mm · {model.volume_mm3!.toFixed(1)} mm³
          {model.mass_kg !== null && ` · ${(model.mass_kg * 1000).toFixed(2)} g`}
        </>} />

      {showConvert && <ConvertDialog onClose={() => setShowConvert(false)} setStatus={setStatus} partName={model?.spec.name ?? ''} hasPart={hasSolid} />}
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
const PICKS: [PickMode, string, LucideIcon][] = [['feature', 'Feature', MousePointer2], ['face', 'Face', Square], ['edge', 'Edge', Minus]]
type RightTab = 'assistant' | 'properties' | 'drawing' | 'analysis'
const RIGHT_TABS: [RightTab, string, LucideIcon][] = [['assistant', 'Assistant', Sparkles], ['properties', 'Properties', SlidersHorizontal], ['drawing', 'Drawing', FileImage], ['analysis', 'Analysis', Gauge]]
