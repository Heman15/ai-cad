// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import {
  Anchor, Ban, Blend, Bot, Boxes, ChevronDown, ChevronRight, Combine, Cuboid, Download, Expand, Eye, EyeOff, FileSpreadsheet, Focus,
  FolderGit2, Grid3x3, Magnet, Minus, MousePointer2, Move, Orbit, PackagePlus, Pin, PinOff, Rotate3d, Ruler, SlidersHorizontal,
  Sparkles, Square, Table, Trash2, Weight, X, type LucideIcon,
} from 'lucide-react'
import { api, desktop, fmt, type Json, type Model, type SelRef, type Settings } from './api'
import Chat from './Chat'
import SettingsDialog from './Settings'
import ErrorBoundary from './ErrorBoundary'
import Viewport, { type DisplayStyle, type MeasureGraphic, type PickMode, type ViewportHandle } from './Viewport'
import MeasurePanel from './Measure'
import { HudButton, RibbonButton, RibbonGroup, StatusBar, TitleBar, ViewHud } from './ui'
import {
  BomPanel, ComponentPanel, ExplodePanel, InterferencePanel, MassPanel, MATES, MateEditor, MatePanel, MovePanel, PatternPanel, UrdfReportPanel,
  faceDirection, mateLabel, type AsmModel, type Clash, type Joint, type MassProps, type UrdfReport,
} from './AssemblyPanels'

/* Assembly workspace (SolidWorks-style): ribbon, FeatureManager tree with components, mates and robot joints,
   PropertyManager for the active command, heads-up view toolbar, measure and exploded views.
   Components reference part files; mates are solved on the server. */

type Result = { ok: boolean; message: string }

async function call<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, body === undefined ? undefined : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const d = await r.json()
  if (!r.ok) throw new Error(d.detail ?? `failed (${r.status})`)
  return d
}

const ASM = [{ name: 'AI CAD assembly', extensions: ['aicadasm'] }]
const PARTS = [{ name: 'Parts', extensions: ['aicad', 'json', 'step', 'stp', 'brep'] }]
const URDF = [{ name: 'Robot description (URDF, xacro)', extensions: ['urdf', 'xacro'] }]
const CSV = [{ name: 'CSV', extensions: ['csv'] }]
const MOVABLE = ['revolute', 'continuous', 'prismatic']

type Command = 'insert' | 'mate' | 'move' | 'rotate' | 'pattern_linear' | 'pattern_circular' | 'explode' | 'interference' | 'mass' | 'bom' | 'urdf' | 'github'
const COMMANDS: Record<Command, [string, LucideIcon]> = {
  insert: ['Insert Components', PackagePlus], mate: ['Mate', Magnet], move: ['Move Component', Move], rotate: ['Rotate Component', Rotate3d],
  pattern_linear: ['Linear Component Pattern', Grid3x3], pattern_circular: ['Circular Component Pattern', Orbit], explode: ['Exploded View', Expand],
  interference: ['Interference Detection', Combine], mass: ['Mass Properties', Weight], bom: ['Bill of Materials', Table],
  urdf: ['URDF Import', Bot], github: ['From GitHub', FolderGit2],
}
type RTab = 'assembly' | 'evaluate' | 'io'
const RTABS: [RTab, string][] = [['assembly', 'Assembly'], ['evaluate', 'Evaluate'], ['io', 'Import / Export']]
const PICKS: [PickMode, string, LucideIcon][] = [['feature', 'Component', MousePointer2], ['face', 'Face', Square], ['edge', 'Edge', Minus]]

/** Column-major 4x4 of a component pose (position mm, rotation vector in degrees). */
function poseMatrix(position: number[], rotation: number[]) {
  const r = new THREE.Vector3(...rotation)
  const deg = r.length()
  const m = deg > 1e-12 ? new THREE.Matrix4().makeRotationAxis(r.normalize(), (deg * Math.PI) / 180) : new THREE.Matrix4()
  return m.setPosition(position[0], position[1], position[2])
}

export default function AssemblyWorkspace({ modeSwitch }: { modeSwitch: React.ReactNode }) {
  const [asm, setAsm] = useState<AsmModel | null>(null)
  const [status, setStatus] = useState<{ ok: boolean; text: string } | null>(null)
  const [selection, setSelection] = useState<SelRef[]>([]) // picked faces/edges (mates, directions)
  const [comps, setComps] = useState<string[]>([]) // selected components
  const [selMate, setSelMate] = useState<string | null>(null)
  const [mode, setMode] = useState<PickMode>('feature')
  const [display, setDisplay] = useState<DisplayStyle>('shaded-edges')
  const [ribbon, setRibbon] = useState<RTab>('assembly')
  const [rtab, setRtab] = useState<'assistant' | 'pm'>('pm')
  const [command, setCommand] = useState<Command | null>(null)
  const [isolate, setIsolate] = useState<string[] | null>(null)
  const [exploded, setExploded] = useState(false)
  const [explodeT, setExplodeT] = useState(0)
  const explodeRef = useRef(0)
  const [open, setOpen] = useState({ joints: true, comps: true, mates: true })
  const [partPath, setPartPath] = useState('')
  const [clashes, setClashes] = useState<Clash[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [mass, setMass] = useState<MassProps | null>(null)
  const [markers, setMarkers] = useState<number[][]>([])
  const [measureSel, setMeasureSel] = useState<SelRef[]>([])
  const [measureGfx, setMeasureGfx] = useState<MeasureGraphic | null>(null)
  const measureRef = useRef(measureSel)
  measureRef.current = measureSel
  const viewRef = useRef<ViewportHandle>(null)
  const [report, setReport] = useState<UrdfReport | null>(null)
  const [joints, setJoints] = useState<Record<string, number>>({})
  const [poses, setPoses] = useState<Record<string, number[]>>({})
  const sending = useRef(false)
  const queued = useRef<Record<string, number> | null>(null)
  const asmRef = useRef(asm)
  asmRef.current = asm

  const [settings, setSettings] = useState<Settings | null>(null)
  const [showSettings, setShowSettings] = useState(false)
  const [gh, setGh] = useState<{ url: string; repository: string; robot_files: string[]; step_files: string[]; licence: string } | null>(null)
  useEffect(() => { api.info().then((i) => setSettings(i.ai)).catch(() => {}) }, [])

  const seen = useRef<string | null>(null) // last assembly version seen (live sync)
  const refresh = useCallback((m: AsmModel) => {
    api.version().then((v) => { seen.current = v.asm }).catch(() => {}) // our own change: no reload by the live sync
    setAsm(m); setPoses({})
    setJoints(Object.fromEntries((m.spec.robot?.joints ?? []).map((j) => [j.name, j.value])))
    const ids = new Set(m.components.map((c) => c.id))
    setComps((c) => c.filter((x) => ids.has(x)))
    setSelMate((s) => (s && m.spec.mates.some((x) => x.id === s) ? s : null))
    setSelection((s) => s.filter((x) => x.component && ids.has(x.component)).map((x) => {
      // re-find picks by their component-local signature (components may have moved)
      const kind = x.kind === 'face' ? m.entities?.faces : x.kind === 'edge' ? m.entities?.edges : m.entities?.vertices
      const e = kind?.find((y) => y.component === x.component && y.local_point?.every((v, i) => Math.abs(v - x.local_point![i]) < 1e-6))
      return e ? { ...x, index: e.index, point: e.point } : null
    }).filter((x): x is SelRef => !!x))
  }, [])

  // Joint dragging: send the latest values, one request at a time; move components by their pose change.
  const sendJoints = useCallback(async (values: Record<string, number>) => {
    if (sending.current) { queued.current = { ...(queued.current ?? {}), ...values }; return }
    sending.current = true
    try {
      const r = await call<{ result: Result; joints: Joint[]; poses: Record<string, { position: number[]; rotation: number[] }> }>('/api/asm/joints', { values })
      if (!r.result.ok) setStatus({ ok: false, text: r.result.message })
      setJoints(Object.fromEntries(r.joints.map((j) => [j.name, j.value])))
      const snap = asmRef.current
      if (snap) {
        const out: Record<string, number[]> = {}
        for (const c of snap.components) {
          const p = r.poses[c.id]
          if (!p) continue
          out[c.id] = poseMatrix(p.position, p.rotation).multiply(poseMatrix(c.position, c.rotation).invert()).toArray()
        }
        setPoses(out)
      }
    } catch (e) { setStatus({ ok: false, text: (e as Error).message }) }
    finally {
      sending.current = false
      const next = queued.current
      queued.current = null
      if (next) sendJoints(next)
    }
  }, [])
  const moveJoint = (name: string, value: number) => { setJoints((j) => ({ ...j, [name]: value })); sendJoints({ [name]: value }) }
  const home = () => {
    const robot = asm?.spec.robot
    if (!robot) return
    const vals = Object.fromEntries(robot.joints.filter((j) => MOVABLE.includes(j.type) && !j.mimic)
      .map((j) => [j.name, Math.min(Math.max(0, j.lower ?? -Infinity), j.upper ?? Infinity)]))
    setJoints((j) => ({ ...j, ...vals }))
    sendJoints(vals)
  }

  useEffect(() => { call<AsmModel>('/api/asm').then(refresh).catch((e) => setStatus({ ok: false, text: e.message })) }, [refresh])
  // Live sync: the assistant (in-app or Claude Code) may import robots, add mates or move joints.
  useEffect(() => {
    const t = setInterval(async () => {
      if (sending.current) return
      try {
        const v = await api.version()
        if (seen.current !== null && v.asm !== seen.current) refresh(await call<AsmModel>('/api/asm'))
        seen.current = v.asm
      } catch { /* backend restarting */ }
    }, 1000)
    return () => clearInterval(t)
  }, [refresh])

  const run = useCallback(async (command: string, args: Record<string, unknown>) => {
    try {
      const r = await call<{ result: Result; model: AsmModel }>('/api/asm/command', { command, args })
      refresh(r.model)
      setStatus({ ok: r.result.ok, text: r.result.message })
      return r.result.ok
    } catch (e) { setStatus({ ok: false, text: (e as Error).message }); return false }
  }, [refresh])
  const simple = useCallback(async (url: string, body?: unknown) => {
    try {
      const r = await call<{ result?: Result; model: AsmModel; path?: string }>(url, body ?? {})
      refresh(r.model)
      if (r.result) setStatus({ ok: r.result.ok, text: r.result.message })
      if (r.path) setStatus({ ok: true, text: `saved ${r.path}` })
      return true
    } catch (e) { setStatus({ ok: false, text: (e as Error).message }); return false }
  }, [refresh])

  const startCommand = (c: Command | null) => {
    setCommand(c)
    setRtab('pm')
    if (c === 'mate') { setMode('face'); setSelection([]) }
    if (c !== 'mate' && mode === 'measure') setMode('feature')
  }

  const fromGithub = async (url: string, path?: string) => {
    setStatus({ ok: true, text: path ? `downloading ${path.split('/').pop()}…` : `looking in ${url}…` })
    try {
      const r = await call<Record<string, Json>>('/api/github', { url, path })
      if (r.file) { // downloaded: import its top-level robot file
        const entry = ((r.entry_points as string[]) ?? [])[0] ?? (r.file as string)
        setGh(null)
        await importUrdf(entry)
        setStatus({ ok: true, text: `imported from GitHub · ${r.licence}` })
      } else {
        setGh({ url, ...(r as unknown as { repository: string; robot_files: string[]; step_files: string[]; licence: string }) })
        startCommand('github')
        setStatus({ ok: true, text: String(r.message) })
      }
    } catch (e) { setStatus({ ok: false, text: (e as Error).message }) }
  }
  const importUrdf = async (given?: string) => {
    const path = given ?? (desktop ? await desktop.openFileDialog('Import robot (URDF or xacro)', URDF) : window.prompt('Path to the .urdf or .xacro file on this computer'))
    if (!path) return
    setStatus({ ok: true, text: `converting ${path.split('/').pop()}…` })
    try {
      const r = await call<{ model: AsmModel; report: UrdfReport; path: string }>('/api/asm/import_urdf', { path })
      refresh(r.model)
      setReport(r.report)
      startCommand('urdf')
      setStatus({ ok: true, text: `imported ${r.report.robot}: ${r.report.links} links, ${r.report.joints} joints → ${r.path}` })
      setTimeout(() => viewRef.current?.view('iso'), 50)
    } catch (e) { setStatus({ ok: false, text: (e as Error).message }) }
  }
  const insert = async () => {
    if (!desktop) { startCommand('insert'); return }
    const path = await desktop.openFileDialog('Insert part', PARTS)
    if (path) run('add_component', { file: path })
  }
  const openAsm = async () => {
    const path = desktop ? await desktop.openFileDialog('Open assembly', ASM) : window.prompt('Assembly file path (.aicadasm)')
    if (path && await simple('/api/asm/open', { path })) { setExploded(false); setIsolate(null); setCommand(null) }
  }
  const save = async (saveAs = false) => {
    let path = saveAs ? null : asm?.path ?? null
    if (!path) path = desktop ? await desktop.saveFileDialog(`${asm?.spec.name || 'assembly'}.aicadasm`, ASM) : window.prompt('Save assembly as (path)')
    if (path) simple('/api/asm/save', { path })
  }
  const exportStep = async () => {
    if (desktop) {
      const path = await desktop.saveExportDialog(`${asm?.spec.name || 'assembly'}.step`, 'step')
      if (path) { const r = await call<Result>('/api/asm/export', { path }); setStatus({ ok: r.ok, text: r.message }) }
    } else Object.assign(document.createElement('a'), { href: '/api/asm/export.step', download: 'assembly.step' }).click()
  }
  const partsPackage = async () => {
    if (!asm?.path) { setStatus({ ok: false, text: 'Save the assembly first: the parts package is built from its saved part files' }); return }
    setStatus({ ok: true, text: 'Building the parts package: separating parts, rebuilding sheet metal, drawings… (can take a few minutes)' })
    try {
      const r = await call<{ folder: string; summary: Record<string, Json> }>('/api/parts/package', {})
      const sm = Object.entries(r.summary).filter(([, v]) => typeof v === 'number').map(([k, v]) => `${v} ${k.replace(/_/g, ' ')}`).join(', ')
      setStatus({ ok: true, text: `Parts package written to ${r.folder}${sm ? ` (${sm})` : ''}` })
    } catch (e) { setStatus({ ok: false, text: (e as Error).message }) }
  }
  const exportBom = async () => {
    if (desktop) {
      const path = await desktop.saveFileDialog(`${asm?.spec.name || 'assembly'}-bom.csv`, CSV)
      if (path) { const r = await call<Result>('/api/asm/export_bom', { path }); setStatus({ ok: r.ok, text: r.message }) }
    } else Object.assign(document.createElement('a'), { href: '/api/asm/bom.csv', download: 'bom.csv' }).click()
  }
  const interference = async () => {
    startCommand('interference')
    setBusy(true)
    try {
      const r = await call<{ items: Clash[] }>('/api/asm/interference', {})
      setClashes(r.items)
      setMarkers(r.items.map((x) => x.center))
      setStatus({ ok: true, text: r.items.length ? `${r.items.length} interference(s) found` : 'no interference' })
    } catch (e) { setStatus({ ok: false, text: (e as Error).message }) }
    finally { setBusy(false) }
  }
  // Results that depend on the assembly are recalculated or cleared only when its content changes (not on live-sync refreshes).
  const specKey = useMemo(() => JSON.stringify(asm?.spec ?? null), [asm])
  useEffect(() => {
    if (command !== 'mass' || specKey === 'null') return
    let live = true
    call<MassProps>('/api/asm/mass').then((m) => { if (live) { setMass(m); setMarkers([m.center_of_mass_mm]) } })
      .catch((e) => setStatus({ ok: false, text: (e as Error).message }))
    return () => { live = false }
  }, [command, specKey])
  useEffect(() => { if (command !== 'mass' && command !== 'interference') setMarkers([]) }, [command])
  useEffect(() => { setClashes(null) }, [specKey])

  // Exploded view: animate between collapsed (0) and exploded (1).
  useEffect(() => {
    let frame = 0
    const target = exploded ? 1 : 0
    const t0 = performance.now()
    const from = explodeRef.current
    const tick = (now: number) => {
      const k = Math.min(1, (now - t0) / 350)
      const v = from + (target - from) * (1 - (1 - k) ** 3)
      explodeRef.current = v
      setExplodeT(v)
      if (k < 1) frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [exploded])

  const steps = useMemo(() => asm?.spec.explode ?? [], [asm])
  const displayPoses = useMemo(() => {
    const out: Record<string, number[]> = { ...poses }
    if (explodeT <= 0) return out
    const off: Record<string, THREE.Vector3> = {}
    for (const s of steps) for (const id of s.components) {
      off[id] = (off[id] ?? new THREE.Vector3()).addScaledVector(new THREE.Vector3(...s.direction), s.distance * explodeT)
    }
    for (const [id, v] of Object.entries(off)) {
      const base = poses[id] ? new THREE.Matrix4().fromArray(poses[id]) : new THREE.Matrix4()
      out[id] = new THREE.Matrix4().makeTranslation(v.x, v.y, v.z).multiply(base).toArray()
    }
    return out
  }, [poses, steps, explodeT])
  const appearance = useMemo(() => Object.fromEntries((asm?.components ?? []).map((c) => [c.id, {
    hidden: c.hidden || (isolate ? !isolate.includes(c.id) : false), opacity: c.transparent ? 0.3 : 1,
  }])), [asm, isolate])
  const pseudo = useMemo(() => (asm?.entities ? ({ entities: asm.entities } as unknown as Model) : null), [asm])
  const measureAsm = useCallback((refs: SelRef[]) => call<Record<string, Json> & { ok: boolean; message?: string }>('/api/asm/measure', { refs }), [])

  const measuring = mode === 'measure'
  const setPick = (m: PickMode) => {
    setMode(m)
    setMeasureSel([]); setMeasureGfx(null)
    if (m === 'measure') setExploded(false) // measurements are in assembled positions
  }
  const onPick = (r: SelRef | null, add: boolean) => {
    if (mode === 'measure') {
      setMeasureSel((s) => {
        if (!r) return []
        if (s.some((x) => x.kind === r.kind && x.index === r.index)) return s.filter((x) => !(x.kind === r.kind && x.index === r.index))
        return s.length >= 2 ? [r] : [...s, r]
      })
      return
    }
    if (!r) { if (!add) { setSelection([]); setComps([]); setSelMate(null) } return }
    setSelMate(null)
    const id = r.component ?? r.feature
    if (mode === 'feature') {
      if (id) setComps((c) => (add ? (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]) : [id]))
      setSelection([])
      return
    }
    const multi = add || command === 'mate'
    const exists = selection.some((x) => x.kind === r.kind && x.index === r.index)
    const next = multi ? (exists ? selection.filter((x) => !(x.kind === r.kind && x.index === r.index)) : [...selection, r]).slice(-2) : [r]
    setSelection(next)
    setComps([...new Set(next.map((x) => x.component!).filter(Boolean))])
  }
  const selectComp = (id: string, add: boolean) => {
    setSelMate(null); setSelection([])
    setComps((c) => (add ? (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]) : [id]))
    if (!command) setRtab('pm')
  }

  // Keyboard shortcuts (same as the Part workspace).
  const keyRef = useRef({ save, undo: () => simple('/api/asm/undo'), redo: () => simple('/api/asm/redo'), openAsm, measuring, command })
  keyRef.current = { save, undo: () => simple('/api/asm/undo'), redo: () => simple('/api/asm/redo'), openAsm, measuring, command }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest('input, textarea, select')) return
      const k = keyRef.current
      const mod = e.ctrlKey || e.metaKey
      if (mod && e.key === 'z') { e.preventDefault(); if (e.shiftKey) k.redo(); else k.undo() }
      else if (mod && e.key === 'y') { e.preventDefault(); k.redo() }
      else if (mod && e.key === 's') { e.preventDefault(); k.save(e.shiftKey) }
      else if (mod && e.key === 'o') { e.preventDefault(); k.openAsm() }
      else if (e.key === 'Escape') {
        if (k.measuring) { if (measureRef.current.length) setMeasureSel([]); else { setMode('feature'); setMeasureGfx(null) } }
        else if (k.command) { setCommand(null); setMode('feature') }
        else { setSelection([]); setComps([]); setSelMate(null) }
      }
      else if (e.key === 'f') viewRef.current?.fit()
      else if (e.key === 'm' && !mod) { setMode((m) => (m === 'measure' ? 'feature' : 'measure')); setMeasureSel([]); setMeasureGfx(null); setExploded(false) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const components = useMemo(() => asm?.components ?? [], [asm])
  const comp = components.find((c) => c.id === comps[0])
  const mate = asm?.spec.mates.find((m) => m.id === selMate)
  const selected = components.filter((c) => comps.includes(c.id))
  const has = components.length > 0
  const faceDir = faceDirection(selection, asm)
  const faceEnt = selection[0]?.kind === 'face' ? asm?.entities?.faces[selection[0].index] : undefined
  const free = command === 'move' || command === 'rotate' ? components.filter((c) => !c.fixed && !c.error).map((c) => c.id) : null
  const st = asm?.status
  const defined = !st ? '' : !st.converged ? `Conflict: ${st.conflicts.join(', ')}` : st.dof === 0 ? 'Fully defined' : `Under defined · ${st.dof} degree(s) of freedom left`
  const totalMass = asm?.bom.reduce((s, r) => s + (r.mass_total_kg ?? 0), 0) ?? 0
  const toggleHidden = () => { if (selected.length) run('set_visibility', { ids: comps, hidden: !selected.every((c) => c.hidden) }) }
  const toggleFixed = () => {
    for (const c of selected.filter((c) => !c.link)) run('set_fixed', { id: c.id, fixed: !c.fixed })
  }
  const [cmdLabel, CmdIcon] = command ? COMMANDS[command] : ['', SlidersHorizontal]
  const wide = command === 'bom' || command === 'mass' || command === 'urdf'

  const pmBody = () => {
    if (!asm) return null
    switch (command) {
      case 'insert':
        return (
          <section>
            <h2>Part file</h2>
            <div className="row">
              <input placeholder="/path/to/part.aicad" aria-label="Part file path" value={partPath} onChange={(e) => setPartPath(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && partPath.trim() && run('add_component', { file: partPath.trim() })} />
              <button className="primary" disabled={!partPath.trim()} onClick={() => run('add_component', { file: partPath.trim() })}>Insert</button>
            </div>
            <p className="muted small">Saved parts (.aicad) or STEP files. The first component is fixed at the origin; later ones float until mated.</p>
          </section>
        )
      case 'mate':
        return <MatePanel selection={selection} comps={comps} run={run} onDone={() => setSelection([])} />
      case 'move': case 'rotate':
        return <MovePanel comp={comp} rotate={command === 'rotate'} run={run} />
      case 'pattern_linear': case 'pattern_circular':
        return <PatternPanel comp={comp} circular={command === 'pattern_circular'} faceDir={faceDir} facePoint={faceEnt?.axis_point ?? null} run={run} />
      case 'explode':
        return <ExplodePanel asm={asm} comps={comps} exploded={exploded} setExploded={setExploded} faceDir={faceDir} run={run} />
      case 'interference':
        return <InterferencePanel clashes={clashes} busy={busy} onRun={interference} onFocus={(c) => setMarkers([c.center])} />
      case 'mass':
        return <MassPanel mass={mass} />
      case 'bom':
        return <BomPanel bom={asm.bom} onExport={exportBom} />
      case 'urdf':
        return report ? <UrdfReportPanel report={report} onHide={() => { setReport(null); setCommand(null) }} /> : null
      case 'github':
        return gh && (
          <section>
            <h2>GitHub · {gh.repository}</h2>
            <p className="small warn">{gh.licence}</p>
            {gh.robot_files.length === 0 && gh.step_files.length === 0 && <p className="muted small">No URDF, xacro or STEP files in this repository.</p>}
            <ul className="findings">
              {gh.robot_files.map((f) => <li key={f}><span title={f}>{f.split('/').slice(-3).join('/')}</span>
                <button className="link" onClick={() => fromGithub(gh.url, f)}>import</button></li>)}
            </ul>
            {gh.step_files.length > 0 && <p className="muted small">{gh.step_files.length} STEP file(s) too; download one and insert it as a part.</p>}
          </section>
        )
    }
    if (mate) return <MateEditor mate={mate} run={run} conflict={!!st?.conflicts.includes(mate.id)} onDeleted={() => setSelMate(null)} />
    if (comp) return <ComponentPanel comp={comp} run={run} />
    return (
      <section>
        <h2>{asm.spec.name}</h2>
        <dl className="stats">
          <dt>Components</dt><dd>{components.length}</dd>
          <dt>Mates</dt><dd>{asm.spec.mates.length}</dd>
          <dt>Status</dt><dd className={st?.converged ? '' : 'err'}>{defined}</dd>
          <dt>Mass</dt><dd>{fmt(totalMass * 1000, 1)} g</dd>
        </dl>
        <p className="muted small">Select a component or mate to edit it, or start a command from the ribbon:
          Insert Components, then Mate faces of two components; Move Component drags free ones.</p>
      </section>
    )
  }

  return (
    <div className={`app part asm ${wide ? 'wide-right' : ''}`}>
      <TitleBar modeSwitch={modeSwitch} kind="Assembly" title={asm?.spec.name || 'Assembly'} unsaved={!asm?.path}
        onNew={() => { simple('/api/asm/new'); setComps([]); setSelection([]); setExploded(false); setIsolate(null); setCommand(null) }}
        onOpen={openAsm} onSave={() => save(false)} onSaveAs={() => save(true)} onUndo={() => simple('/api/asm/undo')} onRedo={() => simple('/api/asm/redo')}
        canSave={!!asm} canUndo={!!asm?.can_undo} canRedo={!!asm?.can_redo} onSettings={() => setShowSettings(true)} />

      <nav className="ribbon">
        <div className="rtabs" role="tablist">
          {RTABS.map(([k, label]) => (
            <button key={k} role="tab" aria-selected={ribbon === k} className={ribbon === k ? 'on' : ''} onClick={() => setRibbon(k)}>{label}</button>
          ))}
        </div>
        <div className="rpanel">
          {ribbon === 'assembly' && <>
            <RibbonGroup label="Components">
              <RibbonButton icon={PackagePlus} label="Insert Components" title="Insert a saved part (.aicad) or STEP file" on={command === 'insert'} onClick={insert} />
              <RibbonButton icon={Bot} label="Import URDF" title="Convert a robot description (URDF or xacro) into parts and an assembly with movable joints" onClick={() => importUrdf()} />
              <RibbonButton icon={FolderGit2} label="From GitHub…" title="Download a robot description from a public GitHub repository"
                onClick={() => { const u = window.prompt('GitHub link to a robot repository, folder or URDF/xacro file'); if (u) fromGithub(u.trim()) }} />
            </RibbonGroup>
            <RibbonGroup label="Mate">
              <RibbonButton icon={Magnet} label="Mate" title="Mate faces of two components" on={command === 'mate'} disabled={components.length < 2} onClick={() => startCommand(command === 'mate' ? null : 'mate')} />
            </RibbonGroup>
            <RibbonGroup label="Move">
              <RibbonButton icon={Move} label="Move Component" title="Drag or nudge a free component" on={command === 'move'} disabled={!has} onClick={() => startCommand(command === 'move' ? null : 'move')} />
              <RibbonButton icon={Rotate3d} label="Rotate Component" title="Rotate a free component about its centre" on={command === 'rotate'} disabled={!has} onClick={() => startCommand(command === 'rotate' ? null : 'rotate')} />
            </RibbonGroup>
            <RibbonGroup label="Pattern">
              <RibbonButton icon={Grid3x3} label="Linear Pattern" title="Linear component pattern" on={command === 'pattern_linear'} disabled={!has} onClick={() => startCommand('pattern_linear')} />
              <RibbonButton icon={Orbit} label="Circular Pattern" title="Circular component pattern" on={command === 'pattern_circular'} disabled={!has} onClick={() => startCommand('pattern_circular')} />
            </RibbonGroup>
            <RibbonGroup label="Display">
              <RibbonButton icon={selected.length && selected.every((c) => c.hidden) ? Eye : EyeOff} label="Show/Hide" title="Hide or show the selected components" disabled={!selected.length} onClick={toggleHidden} />
              <RibbonButton icon={Focus} label="Isolate" title="Show only the selected components" on={!!isolate} disabled={!isolate && !selected.length} onClick={() => setIsolate(isolate ? null : comps)} />
              <RibbonButton icon={Blend} label="Transparency" title="See-through selected components" disabled={!selected.length}
                onClick={() => run('set_visibility', { ids: comps, transparent: !selected.every((c) => c.transparent) })} />
              <RibbonButton icon={Eye} label="Show All" title="Show every component" disabled={!has} onClick={() => { setIsolate(null); run('set_visibility', { ids: '*', hidden: false }) }} />
            </RibbonGroup>
            <RibbonGroup label="Exploded View">
              <RibbonButton icon={Expand} label="Exploded View" title="Edit explode steps, or explode automatically" on={command === 'explode'} disabled={!has} onClick={() => startCommand(command === 'explode' ? null : 'explode')} />
              <RibbonButton icon={exploded ? Combine : Expand} label={exploded ? 'Collapse' : 'Explode'} title="Toggle the exploded view" on={exploded} disabled={!steps.length} onClick={() => setExploded(!exploded)} />
            </RibbonGroup>
            <RibbonGroup label="Fix">
              <RibbonButton icon={Anchor} label="Fix / Float" title="Fix the selected components in place, or let them float" disabled={!selected.some((c) => !c.link)} onClick={toggleFixed} />
            </RibbonGroup>
          </>}
          {ribbon === 'evaluate' && <>
            <RibbonGroup label="Check">
              <RibbonButton icon={Combine} label="Interference Detection" title="Find overlapping volume between components" disabled={components.length < 2} onClick={interference} />
            </RibbonGroup>
            <RibbonGroup label="Measure">
              <RibbonButton icon={Ruler} label="Measure" title="Measure distances, angles, diameters and areas between components (M)" on={measuring} disabled={!has} onClick={() => setPick(measuring ? 'feature' : 'measure')} />
            </RibbonGroup>
            <RibbonGroup label="Properties">
              <RibbonButton icon={Weight} label="Mass Properties" title="Mass, volume and centre of mass of the assembly" on={command === 'mass'} disabled={!has} onClick={() => startCommand('mass')} />
              <RibbonButton icon={Table} label="Bill of Materials" title="Parts list with quantities, materials and masses" on={command === 'bom'} disabled={!has} onClick={() => startCommand('bom')} />
            </RibbonGroup>
          </>}
          {ribbon === 'io' && <>
            <RibbonGroup label="Import">
              <RibbonButton icon={PackagePlus} label="Insert Components" title="Insert a saved part (.aicad) or STEP file" onClick={insert} />
              <RibbonButton icon={Bot} label="Import URDF" title="Convert a robot description (URDF or xacro)" onClick={() => importUrdf()} />
            </RibbonGroup>
            <RibbonGroup label="Export">
              <RibbonButton icon={Download} label="Export STEP" title="The assembly as one STEP file (posed)" disabled={!has} onClick={exportStep} />
              <RibbonButton icon={FileSpreadsheet} label="BOM CSV" title="Bill of materials as a CSV file" disabled={!has} onClick={exportBom} />
            </RibbonGroup>
            <RibbonGroup label="Manufacturing">
              <RibbonButton icon={Boxes} label="Parts Package" disabled={!has}
                title="Separate every part, group identical ones, rebuild sheet-metal meshes as editable parts, and write a folder with part files, STEP, drawings, flat patterns and a BOM"
                onClick={partsPackage} />
            </RibbonGroup>
          </>}
        </div>
      </nav>

      <aside className="left">
        {asm && <>
          <div className="fm-head">FeatureManager</div>
          <div className="part-name">
            <Boxes size={15} className="accent" />
            <input value={asm.spec.name} aria-label="Assembly name" onChange={(e) => setAsm({ ...asm, spec: { ...asm.spec, name: e.target.value } })}
              onBlur={(e) => run('set_name', { name: e.target.value })} />
            {asm.path && <span className="muted small" title={asm.path}>{asm.path.split('/').pop()}</span>}
          </div>
          {st && <div className={`fm-status small ${st.converged ? (st.dof === 0 ? 'ok' : 'muted') : 'err'}`}>{defined}</div>}

          {asm.spec.robot && (
            <Folder label={`Joints · ${asm.spec.robot.name}`} open={open.joints} onToggle={() => setOpen({ ...open, joints: !open.joints })}
              extra={<button className="link" onClick={home} title="All joints to zero (within limits)">home</button>}>
              <section className="robot">
                {asm.spec.robot.joints.filter((j) => MOVABLE.includes(j.type)).map((j) => {
                  const prism = j.type === 'prismatic'
                  const lo = j.lower ?? -180, hi = j.upper ?? 180
                  const m = j as Joint & { multiplier?: number; offset?: number }
                  const v = j.mimic ? (joints[j.mimic] ?? 0) * (m.multiplier ?? 1) + (m.offset ?? 0) : joints[j.name] ?? j.value
                  return (
                    <div key={j.name} className="joint">
                      <div className="row small"><b>{j.name}</b><span className="muted">{j.type}{j.mimic ? ` · follows ${j.mimic}` : ''}</span>
                        <span className="spacer" /><span className="jval">{fmt(v, 1)}{prism ? ' mm' : '°'}</span></div>
                      <input type="range" aria-label={`joint ${j.name}`} min={lo} max={hi} step="any" value={v}
                        disabled={!!j.mimic} onChange={(e) => moveJoint(j.name, Number(e.target.value))} />
                    </div>
                  )
                })}
                {asm.spec.robot.joints.some((j) => !MOVABLE.includes(j.type)) &&
                  <p className="muted small">{asm.spec.robot.joints.filter((j) => !MOVABLE.includes(j.type)).length} fixed joint(s) not shown.</p>}
              </section>
            </Folder>
          )}

          <Folder label={`Components (${components.length})`} open={open.comps} onToggle={() => setOpen({ ...open, comps: !open.comps })}>
            {!components.length && <p className="muted small">Insert saved parts (.aicad or STEP), or import a robot (URDF).</p>}
            <ul className="tree comps">
              {components.map((c) => {
                const hidden = c.hidden || (isolate ? !isolate.includes(c.id) : false)
                return (
                  <li key={c.id} className={`${comps.includes(c.id) ? 'selected' : ''} ${hidden ? 'hidden-comp' : ''}`} title={c.file}
                    onClick={(e) => selectComp(c.id, e.shiftKey || e.ctrlKey || e.metaKey)}>
                    <span className="icon">{c.link ? <Bot size={14} /> : <Cuboid size={14} />}</span>
                    <span className="name"><span className="fix-flag" title={c.fixed ? 'fixed' : 'floating'}>{c.fixed ? '(f)' : '(-)'}</span> {c.id}
                      {c.link && <span className="muted"> &lt;{c.link}&gt;</span>}</span>
                    <span className="type">{c.error ? <span className="err">missing</span> : c.link ? '' : c.name}</span>
                    <span className="actions" onClick={(e) => e.stopPropagation()}>
                      <button className="link" aria-label={`${c.hidden ? 'Show' : 'Hide'} ${c.id}`} title={c.hidden ? 'Show' : 'Hide'}
                        onClick={() => run('set_visibility', { ids: c.id, hidden: !c.hidden })}>{c.hidden ? <EyeOff size={13} /> : <Eye size={13} />}</button>
                      {!c.link && <button className="link" aria-label={`${c.fixed ? 'Float' : 'Fix'} ${c.id}`} title={c.fixed ? 'Float (free to move)' : 'Fix in place'}
                        onClick={() => run('set_fixed', { id: c.id, fixed: !c.fixed })}>{c.fixed ? <PinOff size={13} /> : <Pin size={13} />}</button>}
                      <button className="link danger" aria-label={`Delete ${c.id}`} title="Delete" onClick={() => run('remove_component', { id: c.id })}><Trash2 size={13} /></button>
                    </span>
                  </li>
                )
              })}
            </ul>
          </Folder>

          <Folder label={`Mates (${asm.spec.mates.length})`} open={open.mates} onToggle={() => setOpen({ ...open, mates: !open.mates })}>
            {!asm.spec.mates.length && <p className="muted small">No mates. Use Mate and click faces on two components.</p>}
            <ul className="tree mates">
              {asm.spec.mates.map((m) => {
                const Icon = MATES[m.type]?.icon ?? Magnet
                const bad = st?.conflicts.includes(m.id)
                return (
                  <li key={m.id} title={`${mateLabel(m)} · ${m.a.component} ↔ ${m.b.component}`} className={`${selMate === m.id ? 'selected' : ''} ${m.suppressed ? 'suppressed' : ''} ${bad ? 'conflict' : ''}`}
                    onClick={() => { setSelMate(m.id); setComps([]); setSelection([]); setCommand(null); setRtab('pm') }}>
                    <span className="icon"><Icon size={14} /></span>
                    <span className="name">{mateLabel(m)}</span>
                    <span className="type">{m.a.component} ↔ {m.b.component}</span>
                    <span className="actions" onClick={(e) => e.stopPropagation()}>
                      <button className="link" aria-label={`${m.suppressed ? 'Unsuppress' : 'Suppress'} ${m.id}`} title={m.suppressed ? 'Unsuppress' : 'Suppress'}
                        onClick={() => run('edit_mate', { id: m.id, suppressed: !m.suppressed })}><Ban size={13} /></button>
                      <button className="link danger" aria-label={`Delete ${m.id}`} title="Delete" onClick={() => run('remove_mate', { id: m.id })}><Trash2 size={13} /></button>
                    </span>
                  </li>
                )
              })}
            </ul>
          </Folder>
          {steps.length > 0 && (
            <button className="tree-row" onClick={() => startCommand('explode')}><Expand size={14} /> Exploded view · {steps.length} step(s){exploded ? ' (shown)' : ''}</button>
          )}
        </>}
      </aside>

      <main className="center">
        <ErrorBoundary name="3D view">
          <Viewport ref={viewRef} model={pseudo} mode={mode} selection={measuring ? measureSel : selection} selectedFeature={null} highlight={measuring ? [] : comps}
            markers={markers} poses={displayPoses} appearance={appearance} display={display} measure={measuring ? measureGfx : null} onPick={onPick}
            draggable={free} onDrag={(id, delta) => run('move_component', { id, translate: delta })}
            emptyHint={<>Empty assembly. <b>Insert Components</b> (saved parts or STEP), or <b>Import URDF</b> to bring in a robot.</>} />
        </ErrorBoundary>
        <ViewHud viewRef={viewRef} display={display} setDisplay={setDisplay}>
          <span className="hud-sep" />
          {PICKS.map(([m, label, Icon]) => (
            <HudButton key={m} icon={Icon} label={label} title={`Select ${label.toLowerCase()}s`} on={mode === m} onClick={() => setPick(m)} />
          ))}
          <span className="hud-sep" />
          <HudButton icon={Ruler} label="Measure tool" title="Measure (M)" on={measuring} disabled={!has} onClick={() => setPick(measuring ? 'feature' : 'measure')} />
          <HudButton icon={Expand} label="Explode toggle" title="Explode / collapse" on={exploded} disabled={!steps.length} onClick={() => setExploded(!exploded)} />
        </ViewHud>
        {measuring && pseudo && (
          <MeasurePanel selection={measureSel} model={pseudo} onGraphic={setMeasureGfx} measure={measureAsm}
            onClear={() => setMeasureSel([])} onClose={() => setPick('feature')} />
        )}
      </main>

      <aside className="right">
        <div className="tabs">
          <button className={rtab === 'assistant' ? 'on' : ''} onClick={() => setRtab('assistant')}><Sparkles size={14} /> Assistant</button>
          <button className={rtab === 'pm' ? 'on' : ''} onClick={() => setRtab('pm')}><SlidersHorizontal size={14} /> PropertyManager</button>
        </div>
        <div className={`tab-body ${rtab === 'assistant' ? '' : 'hidden'}`}>
          <Chat selection={[]} onModel={() => {}} aiReady={!!settings?.ready} openSettings={() => setShowSettings(true)} />
        </div>
        <div className={`tab-body scroll pm ${rtab === 'pm' ? '' : 'hidden'}`}>
          {command && (
            <div className="pm-head">
              <CmdIcon size={16} /> <b>{cmdLabel}</b>
              <span className="spacer" />
              <button className="icon-btn" aria-label="Close command" title="Close (Esc)" onClick={() => { setCommand(null); if (mode !== 'measure') setMode('feature') }}><X size={15} /></button>
            </div>
          )}
          {pmBody()}
        </div>
      </aside>

      <StatusBar status={status}
        hint={measuring ? 'Measure: click points, edges or faces of components · Esc clears'
          : command === 'mate' ? `Mate: click two faces on different components (${selection.length}/2)`
          : free ? 'Drag a free component in the 3D view, or use the arrows in the PropertyManager'
          : exploded ? 'Exploded view (display only) · Collapse to return'
          : comps.length ? `${comps.length} component(s) selected · Shift-click to add`
          : `Click selects ${mode === 'feature' ? 'components' : `${mode}s`} · drag rotates · right-drag pans · wheel zooms`}
        readout={asm && has && <>{components.length} components · {asm.spec.mates.length} mates · {defined}{totalMass > 0 ? ` · ${fmt(totalMass * 1000, 1)} g` : ''}</>} />

      {showSettings && settings && <SettingsDialog settings={settings} onSaved={setSettings} onClose={() => setShowSettings(false)} />}
    </div>
  )
}

function Folder({ label, open, onToggle, extra, children }: { label: string; open: boolean; onToggle: () => void; extra?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="folder">
      <div className="folder-head">
        <button className="tree-row" aria-expanded={open} onClick={onToggle}>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />} {label}</button>
        {extra}
      </div>
      {open && <div className="folder-body">{children}</div>}
    </div>
  )
}
