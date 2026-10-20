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
import Viewport, { type PickMode, type StdView, type ViewportHandle } from './Viewport'

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
  const [tab, setTab] = useState<'assistant' | 'properties' | 'drawing' | 'analysis'>('assistant')
  const [overlay, setOverlay] = useState<Overlay | null>(null)
  const [markers, setMarkers] = useState<number[][]>([])
  const [chatTrigger, setChatTrigger] = useState<ChatTrigger | null>(null)
  const [showDrawing, setShowDrawing] = useState(false)
  const [showFlat, setShowFlat] = useState(false)
  const drawingInput = useRef<HTMLInputElement>(null)
  const [addOpen, setAddOpen] = useState(false)
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
      else if (e.key === 'Escape') { setSelection([]); setSelectedFeature(null) }
      else if (e.key === 'f') viewRef.current?.fit()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [undo, redo, save, open])

  const onPick = useCallback((ref: SelRef | null, additive: boolean) => {
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
  const views: StdView[] = ['iso', 'front', 'top', 'right']

  return (
    <div className={`app ${tab === 'drawing' || tab === 'analysis' ? 'wide-right' : ''}`}>
      <header className="toolbar">
        <span className="brand">AI CAD</span>
        {modeSwitch}
        <div className="group">
          <button onClick={() => { apply(api.newProject()); setSelection([]) }} title="New part">New</button>
          <button onClick={open} title="Open (Ctrl+O)">Open</button>
          <button onClick={() => save(false)} disabled={!model} title="Save (Ctrl+S)">Save</button>
          <button onClick={() => save(true)} disabled={!model} title="Save as (Ctrl+Shift+S)">Save as</button>
          <input ref={fileRef} type="file" accept=".aicad,.json" hidden onChange={async (e) => {
            const f = e.target.files?.[0]
            e.target.value = ''
            if (!f) return
            try { await apply(api.uploadProject(JSON.parse(await f.text()))); setStatus({ ok: true, text: `opened ${f.name}` }) }
            catch (err) { setStatus({ ok: false, text: `could not open ${f.name}: ${(err as Error).message}` }) }
          }} />
        </div>
        <div className="group">
          <button onClick={undo} disabled={!model?.can_undo} title="Undo (Ctrl+Z)">↶ Undo</button>
          <button onClick={redo} disabled={!model?.can_redo} title="Redo (Ctrl+Y)">↷ Redo</button>
        </div>
        <div className="group menu-anchor">
          <button className="primary" onClick={() => setAddOpen(!addOpen)}>＋ Feature ▾</button>
          {addOpen && (
            <div className="menu" onMouseLeave={() => setAddOpen(false)}>
              <button onClick={() => { setSketching({ editing: null }); setAddOpen(false) }}>Sketch (constraints)…</button>
              {DIALOGS.map((d) => (
                <button key={d.kind} disabled={d.needsSolid && !hasSolid} onClick={() => { setDialog(d.kind); setAddOpen(false) }}>{d.label}</button>
              ))}
              <div className="menu-note">Not supported: draft, rib and variable-radius fillet features; flanges on flanges.</div>
            </div>
          )}
        </div>
        <div className="group seg" title="What a click selects">
          {(['feature', 'face', 'edge'] as PickMode[]).map((m) => (
            <button key={m} className={mode === m ? 'on' : ''} onClick={() => setMode(m)}>{m}</button>
          ))}
        </div>
        <div className="group seg">
          {views.map((v) => <button key={v} onClick={() => viewRef.current?.view(v)}>{v}</button>)}
          <button onClick={() => viewRef.current?.fit()} title="Fit (F)">fit</button>
        </div>
        <div className="group">
          <button onClick={() => drawingInput.current?.click()} title="Import a PDF or image drawing">Import drawing</button>
          <input ref={drawingInput} type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" hidden
            onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) importDrawing(f) }} />
          <button onClick={() => setShowDrawing(true)} disabled={!hasSolid}>2D drawing</button>
          {model?.sheet && <button onClick={() => setShowFlat(true)}>Flat pattern</button>}
        </div>
        <div className="group">
          <button onClick={() => exportFile('step')} disabled={!hasSolid}>Export STEP</button>
          <button onClick={() => exportFile('stl')} disabled={!hasSolid}>STL</button>
          <button onClick={() => setShowSettings(true)} title="AI settings">⚙</button>
        </div>
        {status && <span className={`status ${status.ok ? 'ok' : 'err'}`} title={status.text}>{status.text}</span>}
      </header>

      <aside className="left">
        {model && (
          <>
            <div className="part-name">
              <input value={model.spec.name} onChange={(e) => setModelState({ ...model, spec: { ...model.spec, name: e.target.value } })}
                onBlur={(e) => run('set_name', { name: e.target.value })} />
              {model.path && <span className="muted small" title={model.path}>{model.path.split('/').pop()}</span>}
            </div>
            {!hasSolid && <button className="primary wide" onClick={() => setDialog('template')}>Create part…</button>}
            <FeatureTree model={model} selected={selectedFeature} run={run}
              onSelect={(id) => { setSelectedFeature(id); setSelection([]); if (id) setTab('properties') }} />
            <Variables model={model} run={run} />
          </>
        )}
      </aside>

      <main className="center">
        <ErrorBoundary name="3D view">
          <Viewport ref={viewRef} model={model} mode={mode} selection={selection} selectedFeature={selectedFeature} onPick={onPick}
            overlay={overlay} markers={markers} />
        </ErrorBoundary>
        {model?.bbox_mm && (
          <div className="readout">
            {model.bbox_mm.map((v) => v.toFixed(1)).join(' × ')} mm · {model.volume_mm3!.toFixed(1)} mm³
            {model.mass_kg !== null && ` · ${(model.mass_kg * 1000).toFixed(2)} g`}
            {selectedFeature && <> · <b>{selectedFeature}</b></>}
          </div>
        )}
      </main>

      <aside className="right">
        <div className="tabs">
          <button className={tab === 'assistant' ? 'on' : ''} onClick={() => setTab('assistant')}>Assistant</button>
          <button className={tab === 'properties' ? 'on' : ''} onClick={() => setTab('properties')}>Properties</button>
          <button className={tab === 'drawing' ? 'on' : ''} onClick={() => setTab('drawing')}>Drawing</button>
          <button className={tab === 'analysis' ? 'on' : ''} onClick={() => setTab('analysis')}>Analysis</button>
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
