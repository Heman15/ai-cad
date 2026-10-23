// SPDX-License-Identifier: AGPL-3.0-or-later
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowDownToLine, Box, Copy, Download, Eye, FlipHorizontal2, FolderInput, Grid2x2, LayoutGrid, Layers, Move3d, Plus, Printer, RotateCcw,
  RotateCw, Scale3d, Scissors, Send, Trash2, X,
} from 'lucide-react'
import { del, desktop, fileBase64, get, MODEL_FILTERS, post } from './api'
import PrintView, { placement, pointInPoly, TYPE_COLORS, type Bed, type MeshData, type PlateItem, type PrintViewHandle, type Toolpaths } from './PrintView'
import { RibbonButton, RibbonGroup, StatusBar, TitleBar } from './ui'

/* ---------------- types from the print API ---------------- */

interface EngineStatus { found: boolean; version?: string; source?: string; message?: string; can_install?: boolean }
interface PrinterInfo {
  vendor: string; vendor_name: string; name: string; model: string; model_name: string; family: string; variant: string
  nozzle: number; thumbnail: string; bed: Bed; custom?: boolean
}
interface Presets {
  prints: { name: string; layer_height: number }[]
  filaments: { name: string; type: string; maker: string; default: boolean; generic: boolean }[]
  defaults: { print: string | null; filament: string | null }
  bed: Bed; nozzle: number; bed_model: { bed_model?: string; bed_texture?: string; thumbnail?: string } | null
}
interface Setting { key: string; value: string; type: string; text: string }
interface PlateObject { id: string; name: string; source: string; triangles: number; size_mm: number[]; watertight: boolean; volume_mm3?: number }
interface Job {
  id: string; status: 'running' | 'done' | 'error'; progress: number; message: string; name?: string
  stats?: { time: string | null; time_s: number | null; filament_mm: number; filament_g: number; filament_cm3: number; cost: number; layers: number }
  warnings?: string[]
}
interface Host { name: string; type: string; url: string; api_key: string }
type Overrides = Record<string, string | number>

/* ---------------- 3x3 matrices (row-major) ---------------- */

const I3 = [1, 0, 0, 0, 1, 0, 0, 0, 1]
const mul = (a: number[], b: number[]) => {
  const o = new Array(9).fill(0)
  for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) for (let k = 0; k < 3; k++) o[i * 3 + j] += a[i * 3 + k] * b[k * 3 + j]
  return o
}
const rot = (axis: 0 | 1 | 2, deg: number) => {
  const c = Math.cos((deg * Math.PI) / 180), s = Math.sin((deg * Math.PI) / 180)
  const r = Math.abs(c) < 1e-12 ? 0 : c, q = Math.abs(s) < 1e-12 ? 0 : s
  return axis === 0 ? [1, 0, 0, 0, r, -q, 0, q, r] : axis === 1 ? [r, 0, q, 0, 1, 0, -q, 0, r] : [r, -q, 0, q, r, 0, 0, 0, 1]
}
const diag = (x: number, y: number, z: number) => [x, 0, 0, 0, y, 0, 0, 0, z]
/** Smallest rotation taking unit vector a onto unit vector b. */
function rotationTo(a: number[], b: number[]) {
  const n = (v: number[]) => { const l = Math.hypot(...v); return v.map((x) => x / l) }
  a = n(a); b = n(b)
  const v = [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
  const c = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
  if (c < -0.999999) {
    let ax = [0, -a[2], a[1]]
    if (Math.hypot(...ax) < 1e-6) ax = [a[2], 0, -a[0]]
    ax = n(ax)
    return [0, 1, 2].flatMap((i) => [0, 1, 2].map((j) => 2 * ax[i] * ax[j] - (i === j ? 1 : 0)))
  }
  const vx = [0, -v[2], v[1], v[2], 0, -v[0], -v[1], v[0], 0]
  const vx2 = mul(vx, vx)
  return I3.map((x, i) => x + vx[i] + vx2[i] / (1 + c))
}

/* ---------------- plate kept across workspace switches ---------------- */

const kept: { items: PlateItem[]; selected: string[] } = { items: [], selected: [] }
let keyCounter = 0
const newKey = () => `i${Date.now().toString(36)}${(keyCounter++).toString(36)}`

const SUPPORT: [string, string][] = [['off', 'None'], ['buildplate', 'Touching build plate'], ['everywhere', 'Everywhere']]
const ADHESION: [string, string][] = [['none', 'None'], ['skirt', 'Skirt'], ['brim', 'Brim'], ['raft', 'Raft']]
const PATTERNS = ['rectilinear', 'alignedrectilinear', 'grid', 'triangles', 'stars', 'cubic', 'line', 'concentric', 'honeycomb',
  '3dhoneycomb', 'gyroid', 'hilbertcurve', 'archimedeanchords', 'octagramspiral', 'adaptivecubic', 'supportcubic', 'lightning']

/** Common settings shown in the Custom section, in Cura's order. */
const GROUPS: [string, [string, string][]][] = [
  ['Quality', [['layer_height', 'Layer height (mm)'], ['first_layer_height', 'First layer height (mm)']]],
  ['Walls', [['perimeters', 'Wall line count'], ['top_solid_layers', 'Top layers'], ['bottom_solid_layers', 'Bottom layers'], ['seam_position', 'Seam position']]],
  ['Infill', [['fill_density', 'Infill density'], ['fill_pattern', 'Infill pattern'], ['infill_every_layers', 'Combine infill every n layers']]],
  ['Material', [['temperature', 'Nozzle temperature (°C)'], ['first_layer_temperature', 'First layer nozzle (°C)'], ['bed_temperature', 'Bed temperature (°C)'],
    ['first_layer_bed_temperature', 'First layer bed (°C)'], ['extrusion_multiplier', 'Flow (extrusion multiplier)']]],
  ['Speed', [['perimeter_speed', 'Wall speed (mm/s)'], ['external_perimeter_speed', 'Outer wall speed'], ['infill_speed', 'Infill speed (mm/s)'],
    ['travel_speed', 'Travel speed (mm/s)'], ['first_layer_speed', 'First layer speed']]],
  ['Travel', [['retract_length', 'Retraction distance (mm)'], ['retract_speed', 'Retraction speed (mm/s)'], ['retract_lift', 'Z hop (mm)']]],
  ['Cooling', [['cooling', 'Enable cooling'], ['min_fan_speed', 'Min fan speed (%)'], ['max_fan_speed', 'Max fan speed (%)'], ['disable_fan_first_layers', 'No fan for first layers']]],
  ['Support', [['support_material_threshold', 'Overhang angle (°, 0 = auto)'], ['support_material_style', 'Support style'],
    ['support_material_contact_distance', 'Z distance (mm)'], ['support_material_spacing', 'Pattern spacing (mm)']]],
  ['Adhesion', [['skirts', 'Skirt loops'], ['skirt_distance', 'Skirt distance (mm)'], ['brim_width', 'Brim width (mm)'], ['raft_layers', 'Raft layers']]],
  ['Special', [['spiral_vase', 'Spiral vase'], ['ironing', 'Ironing'], ['thick_bridges', 'Thick bridges'], ['avoid_crossing_perimeters', 'Avoid crossing walls']]],
]
const ENUMS: Record<string, string[]> = {
  fill_pattern: PATTERNS, top_fill_pattern: ['rectilinear', 'monotonic', 'monotoniclines', 'alignedrectilinear', 'concentric', 'hilbertcurve', 'archimedeanchords', 'octagramspiral'],
  bottom_fill_pattern: ['rectilinear', 'monotonic', 'monotoniclines', 'alignedrectilinear', 'concentric', 'hilbertcurve', 'archimedeanchords', 'octagramspiral'],
  seam_position: ['aligned', 'nearest', 'random', 'rear'], support_material_style: ['grid', 'snug', 'organic'],
  support_material_pattern: ['rectilinear', 'rectilinear-grid', 'honeycomb'], gcode_flavor: ['marlin2', 'marlin', 'klipper', 'reprap', 'repetier', 'smoothie'],
}

/* ---------------- workspace ---------------- */

export default function PrintWorkspace({ modeSwitch, onSettings }: { modeSwitch: React.ReactNode; onSettings: () => void }) {
  const [engine, setEngine] = useState<EngineStatus | null>(null)
  const [printers, setPrinters] = useState<PrinterInfo[]>([])
  const [printer, setPrinter] = useState<string | null>(null)
  const [presets, setPresets] = useState<Presets | null>(null)
  const [quality, setQuality] = useState<string | null>(null)
  const [material, setMaterial] = useState<string | null>(null)
  const [overrides, setOverrides] = useState<Overrides>({})
  const [base, setBase] = useState<Record<string, Setting>>({})
  const [objects, setObjects] = useState<PlateObject[]>([])
  const [meshes, setMeshes] = useState<Map<string, MeshData>>(new Map())
  const [items, setItemsState] = useState<PlateItem[]>(kept.items)
  const [selected, setSelectedState] = useState<string[]>(kept.selected)
  const [status, setStatus] = useState<{ ok: boolean; text: string } | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const [stale, setStale] = useState(false)
  const [mode, setMode] = useState<'prepare' | 'preview'>('prepare')
  const [toolpaths, setToolpaths] = useState<Toolpaths | null>(null)
  const [layers, setLayers] = useState<[number, number]>([0, 0])
  const [hiddenTypes, setHiddenTypes] = useState<Set<number>>(new Set([14]))
  const [facePick, setFacePick] = useState(false)
  const [dialog, setDialog] = useState<'printer' | 'hosts' | null>(null)
  const [hosts, setHosts] = useState<Host[]>([])
  const [installing, setInstalling] = useState<string | null>(null)
  const viewRef = useRef<PrintViewHandle>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const setItems = useCallback((f: (prev: PlateItem[]) => PlateItem[]) => {
    setItemsState((prev) => { const next = f(prev); kept.items = next; return next })
    setStale(true)
  }, [])
  const setSelected = useCallback((s: string[]) => { kept.selected = s; setSelectedState(s) }, [])
  const fail = (e: unknown) => setStatus({ ok: false, text: (e as Error).message })

  // ----- engine, printers, saved choices -----
  const loadEngine = useCallback(async () => {
    const e = await get<EngineStatus>('/print/engine')
    setEngine(e)
    if (!e.found) return
    const r = await get<{ printers: PrinterInfo[]; state: { printer?: string; print?: string; filament?: string; overrides?: Overrides; hosts?: Host[] } }>('/print/printers')
    setPrinters(r.printers)
    setHosts(r.state.hosts ?? [])
    const st = r.state
    if (st.printer && r.printers.some((p) => p.name === st.printer)) {
      setPrinter(st.printer)
      setQuality(st.print ?? null)
      setMaterial(st.filament ?? null)
      setOverrides(st.overrides ?? {})
    } else setDialog('printer')
  }, [])
  useEffect(() => { loadEngine().catch(fail) }, [loadEngine])

  // Presets for the chosen printer; keep the chosen quality/material if this printer has them.
  useEffect(() => {
    if (!printer) return
    get<Presets>(`/print/presets?printer=${encodeURIComponent(printer)}`).then((p) => {
      setPresets(p)
      setQuality((q) => (q && p.prints.some((x) => x.name === q) ? q : p.defaults.print))
      setMaterial((m) => (m && p.filaments.some((x) => x.name === m) ? m : p.defaults.filament))
    }).catch(fail)
  }, [printer])

  // Resolved settings (what the quality and material profiles say) for the settings panel.
  useEffect(() => {
    if (!printer || !quality || !material) return
    post<{ settings: Setting[] }>('/print/config', { printer, print: quality, filament: material })
      .then((r) => setBase(Object.fromEntries(r.settings.map((s) => [s.key, s])))).catch(fail)
    post('/print/state', { printer, print: quality, filament: material }).catch(() => {})
  }, [printer, quality, material])
  useEffect(() => { setStale(true) }, [printer, quality, material, overrides])
  useEffect(() => {
    const t = setTimeout(() => { post('/print/state', { overrides }).catch(() => {}) }, 600)
    return () => clearTimeout(t)
  }, [overrides])

  // ----- plate objects -----
  const fetchMesh = useCallback(async (id: string) => {
    const res = await fetch(`/api/print/objects/${id}/mesh`)
    if (!res.ok) throw new Error('could not load the model')
    const buf = await res.arrayBuffer()
    const [nv, nf] = new Uint32Array(buf, 0, 2)
    return { V: new Float32Array(buf, 8, nv * 3), F: new Uint32Array(buf, 8 + nv * 12, nf * 3) }
  }, [])

  const bed = presets?.bed ?? null
  const arrange = useCallback((list: PlateItem[], ms: Map<string, MeshData>): PlateItem[] => {
    if (!bed) return list
    const gap = 6
    const sizes = list.map((it) => {
      const m = ms.get(it.object)
      if (!m) return { w: 10, d: 10 }
      const p = placement(m, { ...it, x: 0, y: 0 })
      return { w: p.max[0] - p.min[0], d: p.max[1] - p.min[1] }
    })
    const width = bed.max[0] - bed.min[0] - 2 * gap
    const order = list.map((_, i) => i).sort((a, b) => sizes[b].d - sizes[a].d)
    const pos: { x: number; y: number }[] = new Array(list.length)
    const rows: { y: number; d: number; x: number; idx: number[] }[] = []
    let row = { y: 0, d: 0, x: 0, idx: [] as number[] }
    for (const i of order) {
      const s = sizes[i]
      if (row.idx.length && row.x + s.w > width) { rows.push(row); row = { y: row.y + row.d + gap, d: 0, x: 0, idx: [] } }
      pos[i] = { x: row.x + s.w / 2, y: row.y + s.d / 2 }
      row.x += s.w + gap
      row.d = Math.max(row.d, s.d)
      row.idx.push(i)
    }
    rows.push(row)
    const totalD = rows[rows.length - 1].y + rows[rows.length - 1].d
    const out = list.map((it) => ({ ...it }))
    for (const r of rows) {
      const rowW = r.x - gap
      for (const i of r.idx) {
        out[i].x = Math.round((bed.center[0] - rowW / 2 + pos[i].x) * 100) / 100
        out[i].y = Math.round((bed.center[1] - totalD / 2 + pos[i].y) * 100) / 100
      }
    }
    return out
  }, [bed])

  const refreshObjects = useCallback(async (added?: string[]) => {
    const r = await get<{ objects: PlateObject[] }>('/print/objects')
    setObjects(r.objects)
    const ms = new Map(meshes)
    for (const o of r.objects) if (!ms.has(o.id)) ms.set(o.id, await fetchMesh(o.id))
    for (const k of [...ms.keys()]) if (!r.objects.some((o) => o.id === k)) ms.delete(k)
    setMeshes(ms)
    setItems((prev) => {
      let list = prev.filter((i) => r.objects.some((o) => o.id === i.object))
      const missing = r.objects.filter((o) => !list.some((i) => i.object === o.id))
      if (missing.length) {
        list = [...list, ...missing.map((o) => ({ key: newKey(), object: o.id, matrix: [...I3], x: bed?.center[0] ?? 0, y: bed?.center[1] ?? 0 }))]
        list = list.length > 1 ? arrange(list, ms) : list
      }
      return list
    })
    if (added?.length) {
      const o = r.objects.find((x) => x.id === added[0])
      if (o) setStatus({ ok: true, text: `added ${o.name}: ${o.size_mm.map((v) => v.toFixed(1)).join(' × ')} mm, ${o.triangles.toLocaleString()} triangles` +
        (o.watertight ? '' : ' — the mesh has holes; the slicer will try to repair it') })
    }
  }, [meshes, fetchMesh, setItems, arrange, bed])
  useEffect(() => { if (engine?.found && bed) refreshObjects().catch(fail) }, [engine?.found, bed]) // eslint-disable-line react-hooks/exhaustive-deps

  const addFiles = useCallback(async () => {
    try {
      if (desktop) {
        const paths = desktop.openFilesDialog ? await desktop.openFilesDialog('Add models to the plate', MODEL_FILTERS)
          : await desktop.openFileDialog('Add a model to the plate', MODEL_FILTERS).then((p) => (p ? [p] : null))
        if (!paths?.length) return
        const ids: string[] = []
        for (const p of paths) {
          setStatus({ ok: true, text: `reading ${p.split('/').pop()}…` })
          ids.push((await post<PlateObject>('/print/objects', { path: p })).id)
        }
        await refreshObjects(ids)
      } else fileRef.current?.click()
    } catch (e) { fail(e) }
  }, [refreshObjects])

  const uploadFiles = async (files: FileList) => {
    try {
      const ids: string[] = []
      for (const f of Array.from(files)) {
        setStatus({ ok: true, text: `uploading ${f.name}…` })
        ids.push((await post<PlateObject>('/print/objects/upload', { filename: f.name, data: await fileBase64(f) })).id)
      }
      await refreshObjects(ids)
    } catch (e) { fail(e) }
  }

  const addOpenPart = async () => {
    try {
      const o = await post<PlateObject>('/print/objects/from_part')
      await refreshObjects([o.id])
    } catch (e) { fail(e) }
  }

  const removeItems = async (keys: string[]) => {
    const left = items.filter((i) => !keys.includes(i.key))
    const gone = [...new Set(items.filter((i) => keys.includes(i.key)).map((i) => i.object))].filter((o) => !left.some((i) => i.object === o))
    for (const o of gone) await del(`/print/objects/${o}`).catch(() => {})
    setItems(() => left)
    setSelected([])
    await refreshObjects()
  }

  // ----- transforms of the selection -----
  const sel = items.filter((i) => selected.includes(i.key))
  const one = sel.length === 1 ? sel[0] : null
  const transform = (f: (m: number[]) => number[]) => setItems((prev) => prev.map((i) => (selected.includes(i.key) ? { ...i, matrix: f(i.matrix).map((v) => Math.round(v * 1e9) / 1e9) } : i)))
  const layFlat = async () => {
    try {
      const next = await Promise.all(sel.map(async (i) => [i.key, (await post<{ matrix: number[] }>(`/print/objects/${i.object}/layflat`, { matrix: i.matrix })).matrix] as const))
      const map = new Map(next)
      setItems((prev) => prev.map((i) => (map.has(i.key) ? { ...i, matrix: map.get(i.key)! } : i)))
    } catch (e) { fail(e) }
  }
  const duplicate = () => {
    const copies = sel.map((i) => ({ ...i, key: newKey() }))
    setItems((prev) => arrange([...prev, ...copies], meshes))
    setSelected(copies.map((c) => c.key))
  }

  const placements = useMemo(() => new Map(items.filter((i) => meshes.has(i.object)).map((i) => [i.key, placement(meshes.get(i.object)!, i)])), [items, meshes])
  const outside = useMemo(() => {
    if (!bed) return []
    return items.filter((i) => {
      const p = placements.get(i.key)
      if (!p) return false
      const corners = [[p.min[0], p.min[1]], [p.max[0], p.min[1]], [p.max[0], p.max[1]], [p.min[0], p.max[1]]]
      return p.max[2] > bed.height + 0.01 || corners.some(([x, y]) => !pointInPoly(x, y, bed.shape))
    }).map((i) => i.key)
  }, [items, placements, bed])

  // ----- settings -----
  const value = (k: string): string => String(overrides[k] ?? base[k]?.value ?? '')
  const setOv = (k: string, v: string | number | null) => setOverrides((o) => {
    const n = { ...o }
    if (v === null || String(v) === base[k]?.value) delete n[k]
    else n[k] = v
    return n
  })
  const num = (k: string) => parseFloat(value(k).split(',')[0]) || 0
  const support = value('support_material') === '1' ? (value('support_material_buildplate_only') === '1' ? 'buildplate' : 'everywhere') : 'off'
  const adhesion = num('raft_layers') > 0 ? 'raft' : num('brim_width') > 0 ? 'brim' : num('skirts') > 0 ? 'skirt' : 'none'
  const setSupport = (s: string) => {
    setOv('support_material', s === 'off' ? 0 : 1)
    if (s !== 'off') { setOv('support_material_auto', 1); setOv('support_material_buildplate_only', s === 'buildplate' ? 1 : 0) }
  }
  const setAdhesion = (a: string) => {
    setOv('raft_layers', a === 'raft' ? Math.max(3, Number(base.raft_layers?.value) || 0) : 0)
    setOv('brim_width', a === 'brim' ? Math.max(5, Number(base.brim_width?.value) || 0) : 0)
    setOv('skirts', a === 'skirt' ? Math.max(1, Number(base.skirts?.value) || 0) : 0)
  }

  // ----- slicing -----
  const slice = async () => {
    if (!printer || !quality || !material || !items.length) return
    if (outside.length) { setStatus({ ok: false, text: 'move the red objects onto the bed (or scale them down) before slicing' }); return }
    try {
      setStale(false)
      const r = await post<{ job: string }>('/print/slice', {
        printer, print: quality, filament: material, overrides,
        instances: items.map((i) => ({ object: i.object, matrix: i.matrix, x: i.x, y: i.y })),
        name: objects.find((o) => o.id === items[0].object)?.name,
      })
      let j: Job = { id: r.job, status: 'running', progress: 0, message: 'starting' }
      setJob(j)
      while (j.status === 'running') {
        await new Promise((res) => setTimeout(res, 300))
        j = await get<Job>(`/print/jobs/${r.job}`)
        setJob(j)
      }
      if (j.status === 'error') { setStatus({ ok: false, text: j.message }); return }
      await loadToolpaths(r.job)
      setMode('preview')
      setStatus({ ok: true, text: `sliced: ${j.stats?.time ?? '?'} · ${j.stats?.filament_g ?? 0} g · ${j.stats?.layers} layers` })
    } catch (e) { fail(e) }
  }
  const loadToolpaths = async (id: string) => {
    const res = await fetch(`/api/print/jobs/${id}/toolpaths`)
    if (!res.ok) throw new Error('could not load the preview')
    const buf = await res.arrayBuffer()
    const [n, nl] = new Uint32Array(buf, 0, 2)
    let o = 8
    const positions = new Float32Array(buf, o, n * 6); o += n * 24
    const types = new Uint8Array(buf, o, n); o += n + ((4 - (n % 4)) % 4)
    const layerZ = new Float32Array(buf, o, nl); o += nl * 4
    const layerStart = new Uint32Array(buf, o, nl)
    setToolpaths({ positions, types, layerZ, layerStart, typeNames: JSON.parse(res.headers.get('X-Types') || '[]') })
    setLayers([0, Math.max(0, nl - 1)])
  }
  const saveGcode = async () => {
    if (!job || job.status !== 'done') return
    try {
      if (desktop) {
        const path = await desktop.saveExportDialog(job.name ?? 'print.gcode', 'gcode')
        if (!path) return
        const r = await post<{ message: string }>(`/print/jobs/${job.id}/save`, { path })
        setStatus({ ok: true, text: r.message })
      } else Object.assign(document.createElement('a'), { href: `/api/print/jobs/${job.id}/gcode`, download: job.name ?? 'print.gcode' }).click()
    } catch (e) { fail(e) }
  }

  // Keyboard: Delete removes, arrows step through layers in the preview.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest('input, textarea, select')) return
      if ((e.key === 'Delete' || e.key === 'Backspace') && selected.length && mode === 'prepare') removeItems(selected)
      else if (e.key === 'Escape') { setFacePick(false); setSelected([]) }
      else if (mode === 'preview' && toolpaths && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
        e.preventDefault()
        const nl = toolpaths.layerZ.length
        setLayers(([a, b]) => [a, Math.max(a, Math.min(nl - 1, b + (e.key === 'ArrowUp' ? 1 : -1)))])
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const current = printers.find((p) => p.name === printer) ?? null
  const presentTypes = useMemo(() => (toolpaths ? [...new Set(toolpaths.types)].sort((a, b) => a - b) : []), [toolpaths])
  const nl = toolpaths?.layerZ.length ?? 0
  const sliceable = !!(printer && quality && material && items.length && engine?.found)

  return (
    <div className="app part print">
      <TitleBar modeSwitch={modeSwitch} kind="Print" title={current?.name ?? 'Choose a printer'} unsaved={false}
        onNew={() => removeItems(items.map((i) => i.key))} onOpen={addFiles} onSave={saveGcode}
        onUndo={() => {}} onRedo={() => {}} canSave={job?.status === 'done' && !stale} canUndo={false} canRedo={false} onSettings={onSettings}>
        <input ref={fileRef} type="file" multiple hidden accept={MODEL_FILTERS[0].extensions.map((e) => `.${e}`).join(',')}
          onChange={(e) => { const f = e.target.files; if (f?.length) uploadFiles(f); e.target.value = '' }} />
      </TitleBar>

      <nav className="ribbon">
        <div className="rtabs" role="tablist">
          <button role="tab" aria-selected={mode === 'prepare'} className={mode === 'prepare' ? 'on' : ''} onClick={() => setMode('prepare')}>Prepare</button>
          <button role="tab" aria-selected={mode === 'preview'} className={mode === 'preview' ? 'on' : ''} disabled={!toolpaths} onClick={() => setMode('preview')}>Preview</button>
        </div>
        <div className="rpanel">
          <RibbonGroup label="Plate">
            <RibbonButton icon={FolderInput} label="Add Model" title="STL, OBJ, 3MF, PLY, STEP, IGES, AI CAD parts and assemblies" onClick={addFiles} disabled={!engine?.found} />
            <RibbonButton icon={Box} label="Add Open Part" title="Put the part from the Part workspace on the plate" onClick={addOpenPart} disabled={!engine?.found} />
            <RibbonButton icon={LayoutGrid} label="Arrange" title="Arrange all objects on the bed" disabled={!items.length} onClick={() => setItems((p) => arrange(p, meshes))} />
            <RibbonButton icon={Trash2} label="Clear Plate" disabled={!items.length} onClick={() => removeItems(items.map((i) => i.key))} />
          </RibbonGroup>
          <RibbonGroup label="Object">
            <RibbonButton icon={ArrowDownToLine} label="Lay Flat" title="Put the selection on its largest flat face" disabled={!sel.length} onClick={layFlat} />
            <RibbonButton icon={Layers} label="Face to Bed" on={facePick} title="Click a face of a model to put it on the bed" disabled={!items.length}
              onClick={() => setFacePick(!facePick)} />
            <RibbonButton icon={RotateCw} label="Rotate 90°" title="Rotate the selection 90° about Z" disabled={!sel.length} onClick={() => transform((m) => mul(rot(2, 90), m))} />
            <RibbonButton icon={FlipHorizontal2} label="Mirror X" title="Mirror the selection across X" disabled={!sel.length} onClick={() => transform((m) => mul(diag(-1, 1, 1), m))} />
            <RibbonButton icon={Copy} label="Duplicate" disabled={!sel.length} onClick={duplicate} />
            <RibbonButton icon={RotateCcw} label="Reset" title="Undo rotations and scaling of the selection" disabled={!sel.length} onClick={() => transform(() => [...I3])} />
            <RibbonButton icon={X} label="Delete" disabled={!sel.length} onClick={() => removeItems(selected)} />
          </RibbonGroup>
          <RibbonGroup label="View">
            <RibbonButton icon={Grid2x2} label="Top" onClick={() => viewRef.current?.view('top')} />
            <RibbonButton icon={Move3d} label="Iso" onClick={() => viewRef.current?.view('iso')} />
            <RibbonButton icon={Eye} label="Preview" on={mode === 'preview'} disabled={!toolpaths} onClick={() => setMode(mode === 'preview' ? 'prepare' : 'preview')} />
          </RibbonGroup>
          <RibbonGroup label="Output">
            <RibbonButton icon={Scissors} label="Slice" disabled={!sliceable || job?.status === 'running'} onClick={slice} />
            <RibbonButton icon={Download} label="Save G-code" disabled={job?.status !== 'done' || stale} onClick={saveGcode} />
            <RibbonButton icon={Send} label="Send" title="Send to a network printer (OctoPrint, Klipper/Moonraker, PrusaLink)" disabled={job?.status !== 'done' || stale} onClick={() => setDialog('hosts')} />
          </RibbonGroup>
        </div>
      </nav>

      <aside className="left">
        <div className="fm-head">Objects on the plate</div>
        {!items.length && <p className="muted small">Add a model (STL, 3MF, OBJ, STEP…) or the part you are designing. Drag objects to move them on the bed.</p>}
        <ul className="plate-list">
          {items.map((i) => {
            const o = objects.find((x) => x.id === i.object)
            const p = placements.get(i.key)
            return (
              <li key={i.key} className={`${selected.includes(i.key) ? 'on' : ''} ${outside.includes(i.key) ? 'bad' : ''}`}
                onClick={(e) => setSelected(e.shiftKey ? (selected.includes(i.key) ? selected.filter((k) => k !== i.key) : [...selected, i.key]) : [i.key])}>
                <Box size={14} />
                <span className="grow">{o?.name ?? '…'}</span>
                {p && <span className="muted small">{(p.max[0] - p.min[0]).toFixed(0)}×{(p.max[1] - p.min[1]).toFixed(0)}×{(p.max[2] - p.min[2]).toFixed(0)}</span>}
              </li>
            )
          })}
        </ul>
        {one && <ObjectPanel item={one} object={objects.find((o) => o.id === one.object)} place={placements.get(one.key)} bed={bed}
          onMatrix={(m) => setItems((prev) => prev.map((i) => (i.key === one.key ? { ...i, matrix: m } : i)))}
          onMove={(x, y) => setItems((prev) => prev.map((i) => (i.key === one.key ? { ...i, x, y } : i)))} />}
        {sel.length > 1 && <p className="muted small">{sel.length} objects selected. Rotate, mirror, lay flat, duplicate and delete apply to all of them.</p>}
        {outside.length > 0 && <p className="err small">{outside.length} object{outside.length > 1 ? 's are' : ' is'} outside the printable volume (red).</p>}
      </aside>

      <main className="center">
        <PrintView ref={viewRef} bed={bed} texture={presets?.bed_model?.bed_texture ?? null} items={items} meshes={meshes} selected={selected}
          outside={outside} mode={mode} toolpaths={toolpaths} layers={layers} hiddenTypes={hiddenTypes} facePick={facePick}
          onSelect={(k, add) => setSelected(k === null ? [] : add ? (selected.includes(k) ? selected.filter((x) => x !== k) : [...selected, k]) : [k])}
          onMove={(k, x, y) => setItems((prev) => prev.map((i) => (i.key === k ? { ...i, x, y } : i)))}
          onFacePick={(k, n) => {
            const R = rotationTo(n, [0, 0, -1])
            setItems((prev) => prev.map((i) => (i.key === k ? { ...i, matrix: mul(R, i.matrix).map((v) => Math.round(v * 1e9) / 1e9) } : i)))
            setFacePick(false)
            setSelected([k])
          }} />
        {facePick && <div className="pv-banner">Click the face that should lie on the bed · Esc cancels</div>}
        {mode === 'preview' && toolpaths && (
          <>
            <div className="pv-legend">
              {presentTypes.map((t) => {
                const name = toolpaths.typeNames[t]
                return (
                  <label key={t} className={hiddenTypes.has(t) ? 'off' : ''}>
                    <input type="checkbox" checked={!hiddenTypes.has(t)} onChange={() => setHiddenTypes((h) => { const n = new Set(h); if (n.has(t)) n.delete(t); else n.add(t); return n })} />
                    <span className="swatch" style={{ background: TYPE_COLORS[name] }} />{name}
                  </label>
                )
              })}
            </div>
            <div className="pv-layers">
              <span className="small">Layer <b>{layers[1] + 1}</b> / {nl} · Z {toolpaths.layerZ[layers[1]]?.toFixed(2)} mm</span>
              <input type="range" min={0} max={Math.max(0, nl - 1)} value={layers[1]} aria-label="Top layer"
                onChange={(e) => { const v = Number(e.target.value); setLayers(([a]) => [Math.min(a, v), v]) }} />
              <input type="range" min={0} max={Math.max(0, nl - 1)} value={layers[0]} aria-label="Bottom layer" className="lo"
                onChange={(e) => { const v = Number(e.target.value); setLayers(([, b]) => [Math.min(v, b), b]) }} />
              <span className="muted small">↑/↓ steps the top layer · bottom slider hides lower layers</span>
            </div>
          </>
        )}
      </main>

      <aside className="right">
        <div className="tab-body scroll print-settings">
          {engine && !engine.found && (
            <section className="card">
              <h3>Slicer engine needed</h3>
              <p className="small">{engine.message}</p>
              {engine.can_install && (
                <button className="primary wide" disabled={!!installing} onClick={async () => {
                  try {
                    const r = await post<{ job: string }>('/print/engine/install')
                    let j: Job = { id: r.job, status: 'running', progress: 0, message: 'starting' }
                    while (j.status === 'running') {
                      setInstalling(j.message)
                      await new Promise((res) => setTimeout(res, 700))
                      j = await get<Job>(`/print/jobs/${r.job}`)
                    }
                    setInstalling(null)
                    if (j.status === 'error') setStatus({ ok: false, text: j.message })
                    else { setStatus({ ok: true, text: 'slicer engine installed' }); await loadEngine() }
                  } catch (e) { setInstalling(null); fail(e) }
                }}>{installing ? `Installing: ${installing}` : 'Install engine (no admin rights needed)'}</button>
              )}
              <button className="wide" onClick={() => loadEngine().catch(fail)}>Check again</button>
            </section>
          )}
          {engine?.found && (
            <>
              <section>
                <h2>Printer</h2>
                <button className="printer-card" onClick={() => setDialog('printer')} title="Choose another printer">
                  {current?.thumbnail ? <img src={`/api/print/thumbnail/${current.vendor}/${current.thumbnail}`} alt="" /> : <Printer size={36} />}
                  <span><b>{current?.name ?? 'Choose a printer…'}</b>
                    {bed && <span className="muted small">{(bed.max[0] - bed.min[0]).toFixed(0)} × {(bed.max[1] - bed.min[1]).toFixed(0)} × {bed.height} mm · nozzle {presets?.nozzle} mm</span>}</span>
                </button>
              </section>
              {presets && (
                <section>
                  <h2>Material</h2>
                  <select value={material ?? ''} onChange={(e) => setMaterial(e.target.value)} aria-label="Material">
                    {groupBy(presets.filaments, (f) => (f.default ? 'Recommended for this printer' : f.type || 'Other')).map(([g, fs]) => (
                      <optgroup key={g} label={g}>{fs.map((f) => <option key={f.name} value={f.name}>{f.name}</option>)}</optgroup>
                    ))}
                  </select>
                  <h2>Quality</h2>
                  <select value={quality ?? ''} onChange={(e) => setQuality(e.target.value)} aria-label="Quality profile">
                    {presets.prints.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
                  </select>
                </section>
              )}
              {Object.keys(base).length > 0 && (
                <section>
                  <h2>Print settings {Object.keys(overrides).length > 0 && <button className="link" onClick={() => setOverrides({})} title="Back to the profile's values">reset {Object.keys(overrides).length} changes</button>}</h2>
                  <div className="param-row"><span>Infill <b>{num('fill_density')}%</b></span>
                    <input type="range" min={0} max={100} step={5} value={num('fill_density')} aria-label="Infill density" onChange={(e) => setOv('fill_density', `${e.target.value}%`)} /></div>
                  <div className="param-row"><span>Infill pattern</span>
                    <select value={value('fill_pattern')} onChange={(e) => setOv('fill_pattern', e.target.value)}>{PATTERNS.map((p) => <option key={p}>{p}</option>)}</select></div>
                  <div className="param-row"><span>Support</span>
                    <select value={support} onChange={(e) => setSupport(e.target.value)} aria-label="Support">{SUPPORT.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></div>
                  <div className="param-row"><span>Adhesion</span>
                    <select value={adhesion} onChange={(e) => setAdhesion(e.target.value)} aria-label="Adhesion">{ADHESION.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></div>
                  <details className="custom-settings">
                    <summary>Custom settings</summary>
                    {GROUPS.map(([g, rows]) => (
                      <Fragment key={g}>
                        <h3>{g}</h3>
                        {rows.filter(([k]) => base[k]).map(([k, label]) => <SettingRow key={k} s={base[k]} label={label} value={value(k)} changed={k in overrides} onChange={(v) => setOv(k, v)} />)}
                      </Fragment>
                    ))}
                  </details>
                  <AllSettings base={base} overrides={overrides} value={value} setOv={setOv} />
                </section>
              )}
              <section className="slice-box">
                {job?.status === 'running' && <div className="progress"><div style={{ width: `${job.progress}%` }} /><span>{job.message}</span></div>}
                <button className="primary wide big" disabled={!sliceable || job?.status === 'running'} onClick={slice}>
                  <Scissors size={16} /> {job?.status === 'running' ? 'Slicing…' : 'Slice'}
                </button>
                {job?.status === 'done' && job.stats && (
                  <div className={stale ? 'stale' : ''}>
                    {stale && <p className="warn small">Settings or the plate changed since this slice. Slice again before saving.</p>}
                    <dl className="stats">
                      <dt>Print time</dt><dd><b>{job.stats.time ?? '—'}</b></dd>
                      <dt>Filament</dt><dd>{job.stats.filament_g ? `${job.stats.filament_g} g · ` : ''}{(job.stats.filament_mm / 1000).toFixed(2)} m</dd>
                      {job.stats.cost > 0 && <><dt>Cost</dt><dd>{job.stats.cost.toFixed(2)}</dd></>}
                      <dt>Layers</dt><dd>{job.stats.layers}</dd>
                    </dl>
                    {!!job.warnings?.length && <ul className="findings">{job.warnings.map((w, i) => <li key={i} className="warn">{w}</li>)}</ul>}
                    <div className="row">
                      <button className="primary" disabled={stale} onClick={saveGcode}><Download size={14} /> Save G-code</button>
                      <button disabled={stale} onClick={() => setDialog('hosts')}><Send size={14} /> Send to printer</button>
                    </div>
                  </div>
                )}
                {job?.status === 'error' && <p className="err small">{job.message}</p>}
              </section>
              <p className="muted small">Slicing by the PrusaSlicer engine {engine.version} ({engine.source}). Profiles come from its printer library.</p>
            </>
          )}
        </div>
      </aside>

      <StatusBar status={status}
        hint={mode === 'preview' ? 'Preview: drag to rotate · ↑/↓ change the top layer' : facePick ? 'Click a face to put it on the bed'
          : 'Click selects · drag an object to move it · Shift-click adds · Delete removes'}
        readout={items.length ? `${items.length} object${items.length > 1 ? 's' : ''} on the plate` : undefined} />

      {dialog === 'printer' && engine?.found && (
        <PrinterDialog printers={printers} current={printer} onClose={() => setDialog(null)}
          onPick={(p) => { setPrinter(p); setDialog(null) }}
          onCustomSaved={async (name) => { await loadEngine(); setPrinter(name); setDialog(null) }} />
      )}
      {dialog === 'hosts' && job && (
        <HostsDialog hosts={hosts} job={job} onHosts={setHosts} onClose={() => setDialog(null)} setStatus={setStatus} />
      )}
    </div>
  )
}

function groupBy<T>(xs: T[], key: (x: T) => string): [string, T[]][] {
  const m = new Map<string, T[]>()
  for (const x of xs) { const k = key(x); m.set(k, [...(m.get(k) ?? []), x]) }
  return [...m.entries()]
}

/* ---------------- object panel ---------------- */

function ObjectPanel({ item, object, place, bed, onMatrix, onMove }: {
  item: PlateItem; object?: PlateObject; place?: { min: number[]; max: number[] }; bed: Bed | null
  onMatrix: (m: number[]) => void; onMove: (x: number, y: number) => void
}) {
  const [deg, setDeg] = useState('45')
  if (!place || !object) return null
  const size = [0, 1, 2].map((k) => place.max[k] - place.min[k])
  const scale = Math.cbrt(Math.abs(item.matrix[0] * (item.matrix[4] * item.matrix[8] - item.matrix[5] * item.matrix[7])
    - item.matrix[1] * (item.matrix[3] * item.matrix[8] - item.matrix[5] * item.matrix[6]) + item.matrix[2] * (item.matrix[3] * item.matrix[7] - item.matrix[4] * item.matrix[6])))
  const apply = (m: number[]) => onMatrix(mul(m, item.matrix).map((v) => Math.round(v * 1e9) / 1e9))
  const tiny = Math.max(...object.size_mm) < 2
  const fitScale = bed ? Math.min((bed.max[0] - bed.min[0]) / size[0], (bed.max[1] - bed.min[1]) / size[1], bed.height / size[2]) * 0.95 : 1
  return (
    <section className="obj-panel">
      <h2>{object.name}</h2>
      <p className="muted small">{object.triangles.toLocaleString()} triangles{object.volume_mm3 ? ` · ${(object.volume_mm3 / 1000).toFixed(2)} cm³` : ''}
        {!object.watertight && <span className="warn"> · has holes</span>}</p>
      {tiny && <p className="warn small">Very small. If the file is in metres or inches:{' '}
        <button className="link" onClick={() => apply(diag(1000, 1000, 1000))}>× 1000</button>
        <button className="link" onClick={() => apply(diag(25.4, 25.4, 25.4))}>× 25.4</button></p>}
      <div className="grid3">
        {['X', 'Y', 'Z'].map((ax, k) => (
          <label key={ax}>{ax} size (mm)
            <input type="number" value={size[k].toFixed(2)} step="any" onChange={(e) => {
              const v = parseFloat(e.target.value)
              if (v > 0) { const s = v / size[k]; apply(diag(s, s, s)) }
            }} /></label>
        ))}
      </div>
      <div className="param-row"><span>Scale (%)</span>
        <input type="number" value={(scale * 100).toFixed(1)} step="any" onChange={(e) => {
          const v = parseFloat(e.target.value) / 100
          if (v > 0 && scale > 0) { const s = v / scale; apply(diag(s, s, s)) }
        }} /></div>
      {bed && fitScale < 1 && <button className="wide" onClick={() => apply(diag(fitScale, fitScale, fitScale))}><Scale3d size={14} /> Scale to fit the printer</button>}
      <div className="grid3">
        <label>Position X<input type="number" value={item.x} step="any" onChange={(e) => onMove(Number(e.target.value), item.y)} /></label>
        <label>Position Y<input type="number" value={item.y} step="any" onChange={(e) => onMove(item.x, Number(e.target.value))} /></label>
        <label>Rotate by (°)<input type="number" value={deg} onChange={(e) => setDeg(e.target.value)} /></label>
      </div>
      <div className="rot-row">
        {(['X', 'Y', 'Z'] as const).map((ax, k) => (
          <span key={ax}>
            <button title={`Rotate ${deg}° about ${ax}`} onClick={() => apply(rot(k as 0 | 1 | 2, Number(deg) || 0))}>{ax} <RotateCw size={12} /></button>
            <button title={`Rotate -${deg}° about ${ax}`} onClick={() => apply(rot(k as 0 | 1 | 2, -(Number(deg) || 0)))}><RotateCcw size={12} /></button>
          </span>
        ))}
      </div>
      <div className="rot-row">
        {(['X', 'Y', 'Z'] as const).map((ax, k) => (
          <button key={ax} title={`Mirror across ${ax}`} onClick={() => apply(diag(k === 0 ? -1 : 1, k === 1 ? -1 : 1, k === 2 ? -1 : 1))}><FlipHorizontal2 size={12} /> {ax}</button>
        ))}
      </div>
    </section>
  )
}

/* ---------------- settings ---------------- */

function SettingRow({ s, label, value, changed, onChange }: { s: Setting; label?: string; value: string; changed: boolean; onChange: (v: string | number | null) => void }) {
  const enumVals = ENUMS[s.key]
  return (
    <div className={`param-row setting ${changed ? 'changed' : ''}`} title={`${s.key}: ${s.text}`}>
      <span>{label ?? s.key.replace(/_/g, ' ')}</span>
      <span className="row">
        {s.type === 'bool' ? <input type="checkbox" checked={value === '1'} onChange={(e) => onChange(e.target.checked ? 1 : 0)} />
          : enumVals ? <select value={value} onChange={(e) => onChange(e.target.value)}>{[...new Set([value, ...enumVals])].map((v) => <option key={v}>{v}</option>)}</select>
          : <input value={value} onChange={(e) => onChange(e.target.value)} />}
        {changed && <button className="link" title={`Back to the profile value (${s.value})`} onClick={() => onChange(null)}>↺</button>}
      </span>
    </div>
  )
}

function AllSettings({ base, overrides, value, setOv }: { base: Record<string, Setting>; overrides: Overrides; value: (k: string) => string; setOv: (k: string, v: string | number | null) => void }) {
  const [q, setQ] = useState('')
  const words = q.toLowerCase().split(/\s+/).filter(Boolean)
  const list = Object.values(base).filter((s) => !s.key.endsWith('_gcode') && words.every((w) => `${s.key} ${s.text}`.toLowerCase().includes(w)))
  return (
    <details className="custom-settings">
      <summary>All settings ({Object.keys(base).length})</summary>
      <input placeholder="Search settings, e.g. retract, bridge, fan" value={q} onChange={(e) => setQ(e.target.value)} />
      {list.slice(0, 80).map((s) => <SettingRow key={s.key} s={s} value={value(s.key)} changed={s.key in overrides} onChange={(v) => setOv(s.key, v)} />)}
      {list.length > 80 && <p className="muted small">{list.length - 80} more; narrow the search.</p>}
    </details>
  )
}

/* ---------------- dialogs ---------------- */

function PrinterDialog({ printers, current, onClose, onPick, onCustomSaved }: {
  printers: PrinterInfo[]; current: string | null; onClose: () => void; onPick: (name: string) => void; onCustomSaved: (name: string) => void
}) {
  const cur = printers.find((p) => p.name === current)
  const [vendor, setVendor] = useState<string>(cur?.vendor ?? 'Creality')
  const [q, setQ] = useState('')
  const [custom, setCustom] = useState(false)
  const vendors = useMemo(() => groupBy(printers, (p) => p.vendor), [printers])
  const words = q.toLowerCase().split(/\s+/).filter(Boolean)
  const shown = words.length ? printers.filter((p) => words.every((w) => `${p.vendor_name} ${p.name}`.toLowerCase().includes(w))) : printers.filter((p) => p.vendor === vendor)
  const models = groupBy(shown, (p) => `${p.vendor}|${p.model || p.name}`)
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal printer-dialog" role="dialog" aria-label="Choose a printer">
        <h2>Choose a printer <button className="link" onClick={onClose} aria-label="Close"><X size={14} /></button></h2>
        {custom ? <CustomPrinterForm onCancel={() => setCustom(false)} onSaved={onCustomSaved} /> : (
          <>
            <div className="row"><input autoFocus placeholder={`Search ${printers.length} printers, e.g. ender 3 v2`} value={q} onChange={(e) => setQ(e.target.value)} />
              <button onClick={() => setCustom(true)}><Plus size={14} /> Custom printer</button></div>
            <div className="pd-body">
              <ul className="pd-vendors">
                {vendors.map(([v, ps]) => (
                  <li key={v}><button className={!words.length && v === vendor ? 'on' : ''} onClick={() => { setVendor(v); setQ('') }}>{ps[0].vendor_name} <span className="muted">{new Set(ps.map((p) => p.model)).size}</span></button></li>
                ))}
              </ul>
              <div className="pd-models">
                {models.map(([k, ps]) => (
                  <div key={k} className={`pd-model ${ps.some((p) => p.name === current) ? 'on' : ''}`}>
                    {ps[0].thumbnail ? <img src={`/api/print/thumbnail/${ps[0].vendor}/${ps[0].thumbnail}`} alt="" loading="lazy" /> : <Printer size={40} className="muted" />}
                    <div>
                      <b>{ps[0].model_name}</b>
                      <div className="muted small">{(ps[0].bed.max[0] - ps[0].bed.min[0]).toFixed(0)} × {(ps[0].bed.max[1] - ps[0].bed.min[1]).toFixed(0)} × {ps[0].bed.height} mm</div>
                      <div className="variants">
                        {ps.map((p) => <button key={p.name} className={p.name === current ? 'primary' : ''} title={p.name} onClick={() => onPick(p.name)}>
                          {ps.length > 1 ? (p.variant ? `${p.variant} mm` : p.name.replace(ps[0].model_name, '').trim() || p.name) : 'Select'}</button>)}
                      </div>
                    </div>
                    {ps[0].custom && <button className="link danger" title="Delete this custom printer" onClick={async () => { await del(`/print/custom-printer/${encodeURIComponent(ps[0].name)}`); onCustomSaved(current ?? '') }}><Trash2 size={13} /></button>}
                  </div>
                ))}
                {!models.length && <p className="muted">No printer matches. Add a custom printer with your bed size instead.</p>}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function CustomPrinterForm({ onCancel, onSaved }: { onCancel: () => void; onSaved: (name: string) => void }) {
  const [f, setF] = useState({ name: 'My printer', width: 220, depth: 220, height: 250, nozzle: 0.4, filament_diameter: 1.75, flavor: 'marlin2',
    origin_center: false, heated_bed: true, retract_length: 5, start_gcode: '', end_gcode: '' })
  const [err, setErr] = useState<string | null>(null)
  const set = (k: string, v: unknown) => setF({ ...f, [k]: v })
  return (
    <div className="form">
      <p className="muted small">For a printer that is not in the library. Bed size and nozzle are what matter most; the start G-code below is a safe Marlin default.</p>
      <label>Name<input value={f.name} onChange={(e) => set('name', e.target.value)} /></label>
      <div className="grid3">
        <label>Bed width X (mm)<input type="number" value={f.width} onChange={(e) => set('width', Number(e.target.value))} /></label>
        <label>Bed depth Y (mm)<input type="number" value={f.depth} onChange={(e) => set('depth', Number(e.target.value))} /></label>
        <label>Max height Z (mm)<input type="number" value={f.height} onChange={(e) => set('height', Number(e.target.value))} /></label>
        <label>Nozzle (mm)<input type="number" step="0.05" value={f.nozzle} onChange={(e) => set('nozzle', Number(e.target.value))} /></label>
        <label>Filament (mm)<select value={f.filament_diameter} onChange={(e) => set('filament_diameter', Number(e.target.value))}><option value={1.75}>1.75</option><option value={2.85}>2.85</option></select></label>
        <label>Retraction (mm)<input type="number" step="0.5" value={f.retract_length} onChange={(e) => set('retract_length', Number(e.target.value))} /></label>
        <label>G-code flavor<select value={f.flavor} onChange={(e) => set('flavor', e.target.value)}>{ENUMS.gcode_flavor.map((v) => <option key={v}>{v}</option>)}</select></label>
      </div>
      <label className="check"><input type="checkbox" checked={f.origin_center} onChange={(e) => set('origin_center', e.target.checked)} /> Origin at the centre of the bed (delta printers)</label>
      <label>Start G-code (blank = default)<textarea rows={4} value={f.start_gcode} onChange={(e) => set('start_gcode', e.target.value)} /></label>
      <label>End G-code (blank = default)<textarea rows={3} value={f.end_gcode} onChange={(e) => set('end_gcode', e.target.value)} /></label>
      {err && <p className="err small">{err}</p>}
      <div className="row end">
        <button onClick={onCancel}>Back</button>
        <button className="primary" onClick={async () => { try { await post('/print/custom-printer', f); onSaved(f.name) } catch (e) { setErr((e as Error).message) } }}>Save printer</button>
      </div>
    </div>
  )
}

function HostsDialog({ hosts, job, onHosts, onClose, setStatus }: {
  hosts: Host[]; job: Job; onHosts: (h: Host[]) => void; onClose: () => void; setStatus: (s: { ok: boolean; text: string }) => void
}) {
  const [f, setF] = useState<Host>({ name: '', type: 'octoprint', url: 'http://', api_key: '' })
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const send = async (h: Host, start: boolean) => {
    setBusy(h.name); setErr(null)
    try {
      const r = await post<{ message: string }>(`/print/jobs/${job.id}/send`, { host: h.name, start })
      setStatus({ ok: true, text: r.message })
      onClose()
    } catch (e) { setErr((e as Error).message) } finally { setBusy(null) }
  }
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label="Send to printer">
        <h2>Send to printer <button className="link" onClick={onClose} aria-label="Close"><X size={14} /></button></h2>
        {hosts.length === 0 && <p className="muted small">No network printers yet. Add one below (OctoPrint, Klipper with Moonraker/Mainsail/Fluidd, or PrusaLink).</p>}
        <ul className="findings">
          {hosts.map((h) => (
            <li key={h.name}>
              <span><b>{h.name}</b> <span className="muted small">{h.type} · {h.url}</span></span>
              <span className="row">
                <button disabled={!!busy} onClick={() => send(h, false)}>{busy === h.name ? 'Sending…' : 'Upload'}</button>
                <button className="primary" disabled={!!busy} onClick={() => send(h, true)}>Upload & print</button>
                <button className="link danger" title="Remove" onClick={async () => onHosts((await del<{ hosts: Host[] }>(`/print/hosts/${encodeURIComponent(h.name)}`)).hosts)}><Trash2 size={13} /></button>
              </span>
            </li>
          ))}
        </ul>
        {err && <p className="err small">{err}</p>}
        <h3>Add a network printer</h3>
        <div className="grid3">
          <label>Name<input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
          <label>Type<select value={f.type} onChange={(e) => setF({ ...f, type: e.target.value })}>
            <option value="octoprint">OctoPrint</option><option value="moonraker">Klipper (Moonraker)</option><option value="prusalink">PrusaLink</option></select></label>
          <label>Address<input value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} placeholder="http://192.168.1.50" /></label>
          <label className="wide">API key {f.type === 'moonraker' ? '(only if required)' : ''}<input type="password" value={f.api_key} onChange={(e) => setF({ ...f, api_key: e.target.value })} /></label>
        </div>
        <div className="row end">
          <button className="primary" disabled={!f.name || !/^https?:\/\/.+/.test(f.url)} onClick={async () => {
            try { onHosts((await post<{ hosts: Host[] }>('/print/hosts', f)).hosts); setF({ name: '', type: 'octoprint', url: 'http://', api_key: '' }) }
            catch (e) { setErr((e as Error).message) }
          }}><Plus size={14} /> Add printer</button>
        </div>
      </div>
    </div>
  )
}
