// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Feature, Json, Model, SelRef } from './api'
import { parseValue, type Run } from './panels'

/* Constraint-based 2D sketcher. Geometry lives in the sketch plane's (u, v) coordinates
   (global in-plane axes: XY -> x,y; YZ -> y,z; XZ -> x,z). The backend solver positions
   everything; this editor only proposes initial positions and constraints. */

type Pt = [number, number]
interface Ent { id: string; type: 'line' | 'circle' | 'arc' | 'spline'; p1?: string; p2?: string; center?: string; start?: string; end?: string; r?: number; points?: string[]; closed?: boolean; construction?: boolean }
interface Con { type: string; [k: string]: Json }
interface SolveOut {
  ok: boolean; message?: string; points?: Record<string, Pt>; radii?: Record<string, number>
  converged?: boolean; dof?: number; status?: string; conflicts?: number[]; profile?: { faces?: number; area_mm2?: number; error?: string }
  curves?: Record<string, Pt[]>
}
interface Sketch { points: Record<string, Pt>; entities: Ent[]; constraints: Con[] }
type Tool = 'select' | 'line' | 'rect' | 'circle' | 'arc' | 'spline'

const AXIS_NAMES = [['y', 'z'], ['x', 'z'], ['x', 'y']]

const uid = (prefix: string, taken: Set<string>) => { let i = 1; while (taken.has(`${prefix}${i}`)) i++; return `${prefix}${i}` }

export default function SketchEditor({ model, selection, editing, run, apply, onClose }: {
  model: Model | null
  selection: SelRef[]
  editing: Feature | null
  run: Run
  apply: (commands: { command: string; args: Record<string, unknown> }[], label: string) => Promise<boolean>
  onClose: () => void
}) {
  const selFace = selection.find((s) => s.kind === 'face' && s.type === 'PLANE')
  const [plane, setPlane] = useState<Json>(() => {
    if (editing) return editing.params.plane ?? 'XY'
    if (selFace) return { face_at: { point: selFace.point, normal: selFace.normal! } }
    return 'XY'
  })
  const [frame, setFrame] = useState<{ axis: number | null; normal: number[] } | null>(null)
  const axis = frame?.axis ?? null
  const [sk, setSk] = useState<Sketch>(() => editing
    ? { points: { origin: [0, 0], ...(editing.params.points as unknown as Record<string, Pt>) }, entities: editing.params.entities as unknown as Ent[], constraints: editing.params.constraints as unknown as Con[] }
    : { points: { origin: [0, 0] }, entities: [], constraints: [] })
  const [history, setHistory] = useState<Sketch[]>([])
  const [tool, setTool] = useState<Tool>('line')
  const [sel, setSel] = useState<string[]>([])
  const [pending, setPending] = useState<string[]>([]) // points placed by the active tool
  const [cursor, setCursor] = useState<Pt | null>(null)
  const [hover, setHover] = useState<string | null>(null) // point or curve under the cursor
  const [solveOut, setSolveOut] = useState<SolveOut | null>(null)
  const [dimValue, setDimValue] = useState('')
  const [outline, setOutline] = useState<Pt[][]>([])
  const [finish, setFinish] = useState({ op: 'extrude', distance: '10', id: '' })
  const [view, setView] = useState({ cx: 50, cy: 30, scale: 4 }) // px per mm
  const [version, setVersion] = useState(0) // bumps on every edit; drives re-solving
  const skRef = useRef(sk)
  skRef.current = sk
  const svgRef = useRef<SVGSVGElement>(null)
  const drag = useRef<{ pid?: string; pan?: { x: number; y: number; cx: number; cy: number }; moved: boolean } | null>(null)

  const sketchId = useMemo(() => editing?.id ?? uid('sketch_', new Set(model?.spec.features.map((f) => f.id))), [editing, model])

  // Plane frame + faint outline of the existing part projected on the sketch plane.
  useEffect(() => {
    fetch('/api/section', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plane }) })
      .then((r) => r.json()).then((d) => {
        if (d.plane) setFrame(d.plane)
        setOutline(d.polylines)
        const all = (d.polylines as Pt[][]).flat()
        if (all.length && !editing) {
          const xs = all.map((p) => p[0]), ys = all.map((p) => p[1])
          const w = Math.max(...xs) - Math.min(...xs), h = Math.max(...ys) - Math.min(...ys)
          setView({ cx: (Math.max(...xs) + Math.min(...xs)) / 2, cy: (Math.max(...ys) + Math.min(...ys)) / 2, scale: Math.min((svgRef.current?.clientWidth ?? 900) / Math.max(w, 1), (svgRef.current?.clientHeight ?? 600) / Math.max(h, 1)) * 0.6 })
        }
      }).catch(() => {})
  }, [JSON.stringify(plane), editing]) // eslint-disable-line react-hooks/exhaustive-deps

  // Solve on every change.
  const solve = useCallback(async (s: Sketch) => {
    if (!Object.keys(s.points).length) { setSolveOut(null); return }
    const r = await fetch('/api/sketch/solve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sketch: s }) })
    const out: SolveOut = await r.json()
    setSolveOut(out)
    if (out.ok && out.converged && out.points) {
      setSk((cur) => cur === s ? {
        ...cur, points: out.points!,
        entities: cur.entities.map((e) => e.type === 'circle' && out.radii?.[e.id] !== undefined ? { ...e, r: out.radii[e.id] } : e),
      } : cur)
    }
  }, [])
  useEffect(() => { solve(skRef.current) }, [version, solve])

  /** Drop points no curve uses (e.g. the first click of a cancelled or undone line) and constraints on them. */
  const prune = (s0: Sketch): Sketch => {
    const used = new Set(['origin', ...s0.entities.flatMap((e) => [e.p1, e.p2, e.center, e.start, e.end, ...(e.points ?? [])]).filter(Boolean) as string[]])
    const points = Object.fromEntries(Object.entries(s0.points).filter(([id]) => used.has(id)))
    if (Object.keys(points).length === Object.keys(s0.points).length) return s0
    const gone = new Set(Object.keys(s0.points).filter((id) => !used.has(id)))
    return { ...s0, points, constraints: s0.constraints.filter((c) => !Object.values(c).some((v) => typeof v === 'string' && gone.has(v))) }
  }

  const change = (next: Sketch) => { setHistory((h) => [...h.slice(-50), sk]); setSk(next); skRef.current = next; setVersion((v) => v + 1) }
  const undo = () => {
    const prev0 = history[history.length - 1]
    if (prev0) {
      const prev = prune(prev0)
      setHistory(history.slice(0, -1)); setSk(prev); skRef.current = prev; setPending([]); setVersion((v) => v + 1)
    }
  }

  // ---- coordinates (viewBox tracks the canvas pixel size, so 1 unit = 1 px) ----
  const [size, setSize] = useState({ W: 1000, H: 640 })
  useEffect(() => {
    const el = svgRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setSize({ W: Math.max(el.clientWidth, 1), H: Math.max(el.clientHeight, 1) }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const { W, H } = size
  const toScreen = ([u, v]: Pt): Pt => [W / 2 + (u - view.cx) * view.scale, H / 2 - (v - view.cy) * view.scale]
  const toSketch = (e: { clientX: number; clientY: number }): Pt => {
    const r = svgRef.current!.getBoundingClientRect()
    const x = e.clientX - r.left, y = e.clientY - r.top
    return [view.cx + (x - W / 2) / view.scale, view.cy - (y - H / 2) / view.scale]
  }
  const snapPoint = (p: Pt): string | null => {
    let best: string | null = null, bd = 9 / view.scale
    for (const [id, q] of Object.entries(sk.points)) {
      const d = Math.hypot(q[0] - p[0], q[1] - p[1])
      if (d < bd) { bd = d; best = id }
    }
    return best
  }
  const hitEntity = (p: Pt): string | null => {
    const tol = 6 / view.scale
    for (const e of sk.entities) {
      if (e.type === 'line') {
        const a = sk.points[e.p1!], b = sk.points[e.p2!]
        const dx = b[0] - a[0], dy = b[1] - a[1], L = Math.hypot(dx, dy) || 1
        const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (L * L)))
        if (Math.hypot(a[0] + t * dx - p[0], a[1] + t * dy - p[1]) < tol) return e.id
      } else if (e.type === 'spline') {
        const pts = solveOut?.curves?.[e.id] ?? e.points!.map((q) => sk.points[q])
        if (pts.some((q) => Math.hypot(q[0] - p[0], q[1] - p[1]) < tol * 1.5)) return e.id
      } else {
        const c = sk.points[e.center!]
        const r = e.type === 'circle' ? e.r! : Math.hypot(sk.points[e.start!][0] - c[0], sk.points[e.start!][1] - c[1])
        if (Math.abs(Math.hypot(p[0] - c[0], p[1] - c[1]) - r) < tol) return e.id
      }
    }
    return null
  }

  // ---- drawing tools ----
  const addPoint = (s: Sketch, p: Pt): [Sketch, string] => {
    const snap = snapPoint(p)
    if (snap) return [s, snap] // reuse the existing point (coincident by construction), incl. the origin
    const id = uid('p', new Set(Object.keys(s.points)))
    const onCurve = hitEntity(p)
    const curve = onCurve ? s.entities.find((e) => e.id === onCurve) : undefined
    const cons: Con[] = []
    if (curve?.type === 'line') cons.push({ type: 'point_on_line', point: id, line: curve.id })
    if (curve?.type === 'circle' || curve?.type === 'arc') cons.push({ type: 'point_on_circle', point: id, circle: curve.id })
    return [{ ...s, points: { ...s.points, [id]: p }, constraints: [...s.constraints, ...cons] }, id]
  }
  const taken = (from: Sketch = sk) => new Set([...Object.keys(from.points), ...from.entities.map((e) => e.id)])

  const onCanvasClick = (p: Pt, select = false) => {
    if (tool === 'select' || (select && pending.length === 0)) {
      const hit = snapPoint(p) ?? hitEntity(p)
      setSel((s) => hit ? (s.includes(hit) ? s.filter((x) => x !== hit) : [...s, hit]) : [])
      return
    }
    let [s, pid] = addPoint(sk, p)
    const pts = [...pending, pid]
    if (tool === 'line') {
      if (pts.length === 2) {
        if (pts[0] === pts[1]) { setPending([]); return }
        const id = uid('l', taken(s))
        const [a, b] = [s.points[pts[0]], s.points[pts[1]]]
        const cons: Con[] = []
        const ang = Math.abs(Math.atan2(b[1] - a[1], b[0] - a[0]) * 180 / Math.PI)
        if (ang < 3 || ang > 177) cons.push({ type: 'horizontal', line: id })
        else if (Math.abs(ang - 90) < 3) cons.push({ type: 'vertical', line: id })
        const closesLoop = sk.entities.some((e) => e.p1 === pid || e.p2 === pid) // ended on existing geometry
        change({ ...s, entities: [...s.entities, { id, type: 'line', p1: pts[0], p2: pts[1] }], constraints: [...s.constraints, ...cons] })
        setPending(closesLoop ? [] : [pid]) // otherwise the chain continues from the end point
      } else { setSk(s); setPending(pts) }
    } else if (tool === 'rect') {
      if (pts.length === 2) {
        const [a, c] = [s.points[pts[0]], s.points[pts[1]]]
        const ids = taken(s)
        const pb = uid('p', ids); ids.add(pb)
        const pd = uid('p', ids); ids.add(pd)
        s = { ...s, points: { ...s.points, [pb]: [c[0], a[1]], [pd]: [a[0], c[1]] } }
        const l = [uid('l', ids)]; ids.add(l[0])
        for (let i = 1; i < 4; i++) { l.push(uid('l', ids)); ids.add(l[i]) }
        const corners = [pts[0], pb, pts[1], pd]
        s = {
          ...s,
          entities: [...s.entities, ...l.map((id, i) => ({ id, type: 'line' as const, p1: corners[i], p2: corners[(i + 1) % 4] }))],
          constraints: [...s.constraints, { type: 'horizontal', line: l[0] }, { type: 'vertical', line: l[1] }, { type: 'horizontal', line: l[2] }, { type: 'vertical', line: l[3] }],
        }
        change(s); setPending([])
      } else { setSk(s); setPending(pts) }
    } else if (tool === 'spline') {
      // clicking the first point again closes the spline; Enter / right-click finishes an open one
      if (pending.length >= 3 && pid === pending[0]) { finishSpline(s, pending, true); return }
      setSk(s); setPending(pts)
    } else if (tool === 'circle') {
      if (pts.length === 1 && !pending.length) { setSk(s); setPending(pts) }
      else {
        const c = s.points[pending[0]]
        const r = Math.max(Math.hypot(p[0] - c[0], p[1] - c[1]), 0.1)
        change({ ...sk, entities: [...sk.entities, { id: uid('c', taken(sk)), type: 'circle', center: pending[0], r }] })
        setPending([])
      }
    } else if (tool === 'arc') {
      if (pts.length < 3) { setSk(s); setPending(pts) }
      else {
        change({ ...s, entities: [...s.entities, { id: uid('a', taken(s)), type: 'arc', center: pts[0], start: pts[1], end: pts[2] }] })
        setPending([])
      }
    }
  }

  function finishSpline(s: Sketch, pts: string[], closed: boolean) {
    if (pts.length >= 2) change({ ...s, entities: [...s.entities, { id: uid('s', taken(s)), type: 'spline', points: pts, closed }] })
    setPending([])
  }

  // ---- selection-driven constraints ----
  const selPts = sel.filter((id) => id in sk.points)
  const selEnts = sel.map((id) => sk.entities.find((e) => e.id === id)).filter(Boolean) as Ent[]
  const lines = selEnts.filter((e) => e.type === 'line')
  const rounds = selEnts.filter((e) => e.type !== 'line')
  const measured = (): number | null => {
    const P = sk.points
    if (selPts.length === 2 && !selEnts.length) return Math.hypot(P[selPts[1]][0] - P[selPts[0]][0], P[selPts[1]][1] - P[selPts[0]][1])
    if (lines.length === 1 && sel.length === 1) return Math.hypot(P[lines[0].p2!][0] - P[lines[0].p1!][0], P[lines[0].p2!][1] - P[lines[0].p1!][1])
    if (rounds.length === 1 && sel.length === 1) {
      const e = rounds[0], c = P[e.center!]
      return 2 * (e.type === 'circle' ? e.r! : Math.hypot(P[e.start!][0] - c[0], P[e.start!][1] - c[1]))
    }
    return null
  }
  useEffect(() => { const m = measured(); setDimValue(m === null ? '' : String(Number(m.toFixed(3)))) }, [sel]) // eslint-disable-line react-hooks/exhaustive-deps

  const addCon = (c: Con) => { change({ ...sk, constraints: [...sk.constraints, c] }); setSel([]) }
  const val = () => parseValue(dimValue || '0')
  const P2 = selPts.length === 2 && selEnts.length === 0
  const buttons: [string, boolean, () => void][] = [
    ['Coincident', P2, () => addCon({ type: 'coincident', a: selPts[0], b: selPts[1] })],
    ['Horizontal', (lines.length === 1 && sel.length === 1) || P2, () => addCon(lines.length ? { type: 'horizontal', line: lines[0].id } : { type: 'horizontal', a: selPts[0], b: selPts[1] })],
    ['Vertical', (lines.length === 1 && sel.length === 1) || P2, () => addCon(lines.length ? { type: 'vertical', line: lines[0].id } : { type: 'vertical', a: selPts[0], b: selPts[1] })],
    ['Parallel', lines.length === 2 && sel.length === 2, () => addCon({ type: 'parallel', a: lines[0].id, b: lines[1].id })],
    ['Perpendicular', lines.length === 2 && sel.length === 2, () => addCon({ type: 'perpendicular', a: lines[0].id, b: lines[1].id })],
    ['Equal', (lines.length === 2 || rounds.length === 2) && sel.length === 2, () => addCon({ type: 'equal', a: sel[0], b: sel[1] })],
    ['Tangent', lines.length === 1 && rounds.length === 1 && sel.length === 2, () => addCon({ type: 'tangent', line: lines[0].id, circle: rounds[0].id })],
    ['Concentric', rounds.length === 2 && sel.length === 2, () => addCon({ type: 'concentric', a: rounds[0].id, b: rounds[1].id })],
    ['Midpoint', selPts.length === 1 && lines.length === 1 && sel.length === 2, () => addCon({ type: 'midpoint', point: selPts[0], line: lines[0].id })],
    ['On line', selPts.length === 1 && lines.length === 1 && sel.length === 2, () => addCon({ type: 'point_on_line', point: selPts[0], line: lines[0].id })],
    ['On circle', selPts.length === 1 && rounds.length === 1 && sel.length === 2, () => addCon({ type: 'point_on_circle', point: selPts[0], circle: rounds[0].id })],
    ['Symmetric', selPts.length === 2 && lines.length === 1 && sel.length === 3, () => addCon({ type: 'symmetric', a: selPts[0], b: selPts[1], line: lines[0].id })],
    ['Fix', selPts.length === 1 && sel.length === 1, () => addCon({ type: 'fix', point: selPts[0], at: sk.points[selPts[0]].map((v) => Number(v.toFixed(4))) })],
  ]
  const dims: [string, boolean, () => void][] = [
    ['Distance', P2, () => addCon({ type: 'distance', a: selPts[0], b: selPts[1], value: val() })],
    ['Horiz. dist.', P2, () => addCon({ type: 'horizontal_distance', a: selPts[0], b: selPts[1], value: val() })],
    ['Vert. dist.', P2, () => addCon({ type: 'vertical_distance', a: selPts[0], b: selPts[1], value: val() })],
    ['Length', lines.length === 1 && sel.length === 1, () => addCon({ type: 'length', line: lines[0].id, value: val() })],
    ['Diameter', rounds.length === 1 && sel.length === 1, () => addCon({ type: 'diameter', circle: rounds[0].id, value: val() })],
    ['Angle °', lines.length === 2 && sel.length === 2, () => addCon({ type: 'angle', a: lines[0].id, b: lines[1].id, value: val() })],
  ]

  const deleteSelected = () => {
    if (!sel.filter((x) => x !== 'origin').length) return
    const gone = new Set(sel.filter((x) => x !== 'origin'))
    // deleting a point removes entities using it; deleting entities removes their constraints
    for (const e of sk.entities) if ([e.p1, e.p2, e.center, e.start, e.end, ...(e.points ?? [])].some((p) => p && gone.has(p))) gone.add(e.id)
    const entities = sk.entities.filter((e) => !gone.has(e.id))
    const used = new Set(entities.flatMap((e) => [e.p1, e.p2, e.center, e.start, e.end, ...(e.points ?? [])]).filter(Boolean) as string[])
    const points = Object.fromEntries(Object.entries(sk.points).filter(([id]) => id === 'origin' || (!gone.has(id) && (used.has(id) || !sel.includes(id)))))
    const constraints = sk.constraints.filter((c) => !Object.values(c).some((v) => typeof v === 'string' && gone.has(v)))
    change({ points, entities, constraints })
    setSel([])
  }

  // Keyboard: one listener for the editor's lifetime (capture phase, so it runs before the app's own
  // shortcuts), always calling the latest handler. Re-registering per render loses events, because the
  // app's handler re-renders between listeners of the same key press.
  const keyRef = useRef<(e: KeyboardEvent) => void>(() => {})
  keyRef.current = (e: KeyboardEvent) => {
    const t = e.target
    if (t instanceof Element && t.closest('input, textarea, select')) return
    if (e.key === 'Enter' && tool === 'spline') { finishSpline(sk, pending, false); return }
    if (e.key === 'Escape') {
      e.stopPropagation()  // Esc belongs to the sketcher while it is open
      if (pending.length) {  // 1st: cancel the line/shape in progress (and its unused start point)
        setPending([])
        const pruned = prune(sk)
        if (pruned !== sk) { setSk(pruned); skRef.current = pruned; setVersion((v) => v + 1) }
        return
      }
      if (tool !== 'select') { setTool('select'); return }  // 2nd: back to Select, to pick geometry for constraints
      setSel([])
      return
    }
    if (e.key === 'Delete' || e.key === 'Backspace') deleteSelected()
    if ((e.ctrlKey || e.metaKey) && e.key === 'z') { e.preventDefault(); e.stopPropagation(); undo() }
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => keyRef.current(e)
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [])

  // ---- finish ----
  const [pathOnly, setPathOnly] = useState<boolean>(!!editing?.params.path_only)
  const params = () => {
    const s0 = prune(sk)
    return { plane, points: s0.points, entities: s0.entities, constraints: s0.constraints, ...(pathOnly ? { path_only: true } : {}) }
  }
  const submit = async (mode: 'sketch' | 'extrude' | 'cut' | 'revolve') => {
    if (editing) {
      if (await run('update_feature', { id: editing.id, params: params() })) onClose()
      return
    }
    const cmds: { command: string; args: Record<string, unknown> }[] = [{ command: 'add_feature', args: { id: sketchId, type: 'sketch', params: params() } }]
    const fid = finish.id || uid(`${mode}_`, new Set(model?.spec.features.map((f) => f.id)))
    if (mode === 'extrude' || mode === 'cut') cmds.push({ command: 'add_feature', args: { id: fid, type: 'extrude', params: { sketch: sketchId, distance: parseValue(finish.distance), op: mode === 'cut' ? 'cut' : 'add' } } })
    if (mode === 'revolve') cmds.push({ command: 'add_feature', args: { id: fid, type: 'revolve', params: { sketch: sketchId, axis: 'v', angle: parseValue(finish.distance) } } })
    if (await apply(cmds, `sketch ${sketchId}${mode === 'sketch' ? '' : ' + ' + mode}`)) onClose()
  }

  // ---- render ----
  const conflicts = new Set(solveOut?.conflicts ?? [])
  const isSel = (id: string) => sel.includes(id)
  const [U, V] = axis === null ? ['u', 'v'] : AXIS_NAMES[axis]
  const gridStep = [1, 2, 5, 10, 20, 50, 100].find((g) => g * view.scale > 30) ?? 100
  const pathOf = (pts: Pt[]) => pts.map((p, i) => `${i ? 'L' : 'M'}${toScreen(p).join(' ')}`).join(' ')

  const describeCon = (c: Con) => {
    const args = Object.entries(c).filter(([k]) => k !== 'type' && k !== 'value' && k !== 'at').map(([, v]) => v).join(', ')
    return `${c.type.replace(/_/g, ' ')} (${args})`
  }

  return (
    <div className="modal-backdrop">
      <div className="sketcher">
        <header className="sk-top">
          <b>Sketch {sketchId}</b>
          <span className="muted small">plane: {JSON.stringify(plane)} · axes {U} → right, {V} → up</span>
          {!editing && (
            <select value={typeof plane === 'string' ? plane : (plane as Record<string, Json>).face_at ? 'face' : 'XY'}
              onChange={(e) => setPlane(e.target.value === 'face' && selFace ? { face_at: { point: selFace.point, normal: selFace.normal! } } : e.target.value)}>
              {selFace && <option value="face">selected face</option>}
              <option value="XY">XY plane</option><option value="XZ">XZ plane</option><option value="YZ">YZ plane</option>
            </select>
          )}
          <span className={`sk-status ${solveOut?.converged ? (solveOut.dof === 0 ? 'ok' : 'warn') : 'err'}`}>
            {!solveOut ? 'empty' : !solveOut.ok ? solveOut.message : solveOut.status}
            {solveOut?.profile?.error && !pathOnly && ` · ${solveOut.profile.error}`}
            {solveOut?.profile?.faces !== undefined && ` · profile ${solveOut.profile.area_mm2?.toFixed(1)} mm²`}
          </span>
          <button onClick={onClose}>Cancel</button>
        </header>

        <div className="sk-body">
          <div className="sk-tools">
            {(['select', 'line', 'rect', 'circle', 'arc', 'spline'] as Tool[]).map((t) => (
              <button key={t} className={tool === t ? 'on' : ''} onClick={() => { setTool(t); setPending([]) }}>{t}</button>
            ))}
            <hr />
            <button onClick={undo} disabled={!history.length}>Undo</button>
            <button onClick={deleteSelected} disabled={!sel.length}>Delete</button>
            <button disabled={!selEnts.length} onClick={() => change({ ...sk, entities: sk.entities.map((e) => sel.includes(e.id) ? { ...e, construction: !e.construction } : e) })}>Construction</button>
            <p className="muted small">
              {tool === 'line' && 'Click points to draw a chain; Esc ends it. Clicking the origin or a line attaches to it.'}
              {tool === 'rect' && 'Click two opposite corners.'}
              {tool === 'circle' && 'Click the centre, then a point on the circle.'}
              {tool === 'arc' && 'Click centre, start, then end (counter-clockwise).'}
              {tool === 'spline' && 'Click points the curve passes through; click the first point to close, Enter or right-click to finish open.'}
              {tool === 'select' && 'Click points/curves to select; drag points to move. Delete removes.'}
            </p>
          </div>

          <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} className="sk-canvas"
            onWheel={(e) => { const f = e.deltaY < 0 ? 1.15 : 1 / 1.15; setView((v) => ({ ...v, scale: Math.min(200, Math.max(0.05, v.scale * f)) })) }}
            onPointerDown={(e) => {
              const p = toSketch(e)
              if (e.button === 1 || e.button === 2) { drag.current = { pan: { x: e.clientX, y: e.clientY, cx: view.cx, cy: view.cy }, moved: false }; return }
              const pid = tool === 'select' ? snapPoint(p) : null
              drag.current = { pid: pid ?? undefined, moved: false }
            }}
            onPointerMove={(e) => {
              const p = toSketch(e)
              setCursor(p)
              setHover(snapPoint(p) ?? hitEntity(p))
              const d = drag.current
              if (!d) return
              if (d.pan) {
                const k = 1 / view.scale
                setView((v) => ({ ...v, cx: d.pan!.cx - (e.clientX - d.pan!.x) * k, cy: d.pan!.cy + (e.clientY - d.pan!.y) * k }))
                d.moved = true
              } else if (d.pid) {
                if (!d.moved) setHistory((h) => [...h, sk])
                d.moved = true
                setSk((s) => ({ ...s, points: { ...s.points, [d.pid!]: p } }))
              }
            }}
            onPointerUp={(e) => {
              const d = drag.current
              drag.current = null
              if (d?.pan) return
              if (d?.pid && d.moved) { setVersion((v) => v + 1); return }
              if (e.button === 0) onCanvasClick(toSketch(e), e.shiftKey)
            }}
            onPointerLeave={() => setHover(null)}
            onContextMenu={(e) => { e.preventDefault(); if (tool === 'spline') finishSpline(sk, pending, false); else setPending([]) }}>
            <defs>
              <pattern id="grid" width={gridStep * view.scale} height={gridStep * view.scale} patternUnits="userSpaceOnUse"
                x={toScreen([0, 0])[0]} y={toScreen([0, 0])[1]}>
                <path d={`M ${gridStep * view.scale} 0 L 0 0 0 ${gridStep * view.scale}`} fill="none" className="sk-grid" />
              </pattern>
            </defs>
            <rect width={W} height={H} fill="url(#grid)" />
            <line x1={0} x2={W} y1={toScreen([0, 0])[1]} y2={toScreen([0, 0])[1]} className="sk-axis" />
            <line y1={0} y2={H} x1={toScreen([0, 0])[0]} x2={toScreen([0, 0])[0]} className="sk-axis" />
            {outline.map((pl, i) => <path key={i} d={pathOf(pl)} className="sk-outline" />)}

            {sk.entities.map((e) => {
              const cls = `sk-ent ${isSel(e.id) ? 'sel' : ''} ${hover === e.id ? 'hover' : ''} ${e.construction ? 'cons' : ''}`
              if (e.type === 'line') {
                const [a, b] = [toScreen(sk.points[e.p1!]), toScreen(sk.points[e.p2!])]
                return <line key={e.id} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} className={cls} />
              }
              if (e.type === 'spline') {
                const pts = solveOut?.curves?.[e.id] ?? e.points!.map((q) => sk.points[q])
                return <path key={e.id} d={pathOf(pts)} className={cls} fill="none" />
              }
              const c = sk.points[e.center!]
              if (e.type === 'circle') { const s = toScreen(c); return <circle key={e.id} cx={s[0]} cy={s[1]} r={e.r! * view.scale} className={cls} /> }
              const st = sk.points[e.start!], en = sk.points[e.end!]
              const r = Math.hypot(st[0] - c[0], st[1] - c[1])
              let a0 = Math.atan2(st[1] - c[1], st[0] - c[0]), a1 = Math.atan2(en[1] - c[1], en[0] - c[0])
              if (a1 <= a0) a1 += 2 * Math.PI
              const pts: Pt[] = Array.from({ length: 33 }, (_, i) => { const a = a0 + (a1 - a0) * i / 32; return [c[0] + r * Math.cos(a), c[1] + r * Math.sin(a)] })
              return <path key={e.id} d={pathOf(pts)} className={cls} fill="none" />
            })}

            {sk.constraints.map((c, i) => {
              if (!('value' in c)) return null
              let at: Pt | null = null
              if (c.line) { const e = sk.entities.find((x) => x.id === c.line); if (e) at = [(sk.points[e.p1!][0] + sk.points[e.p2!][0]) / 2, (sk.points[e.p1!][1] + sk.points[e.p2!][1]) / 2] }
              else if (c.circle) { const e = sk.entities.find((x) => x.id === c.circle); if (e) at = sk.points[e.center!] }
              else if (c.a && c.b && sk.points[c.a as string]) { const a = sk.points[c.a as string], b = sk.points[c.b as string]; at = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] }
              else if (c.a) { const e = sk.entities.find((x) => x.id === c.a); if (e?.p1) at = sk.points[e.p1] }
              if (!at) return null
              const s = toScreen(at)
              const prefix = c.type === 'diameter' ? 'Ø' : c.type === 'radius' ? 'R' : c.type === 'angle' ? '∠' : ''
              return <text key={i} x={s[0] + 6} y={s[1] - 6} className={`sk-dim ${conflicts.has(i) ? 'bad' : ''}`}>{prefix}{String(c.value)}</text>
            })}

            {Object.entries(sk.points).map(([id, p]) => {
              const s = toScreen(p)
              if (id === 'origin') {
                return (
                  <g key={id} className={`sk-origin ${isSel(id) ? 'sel' : ''} ${hover === id ? 'hover' : ''}`}>
                    <line x1={s[0]} y1={s[1]} x2={s[0] + 22} y2={s[1]} className="ax-u" />
                    <line x1={s[0]} y1={s[1]} x2={s[0]} y2={s[1] - 22} className="ax-v" />
                    <circle cx={s[0]} cy={s[1]} r={5} />
                    <text x={s[0] + 7} y={s[1] + 14}>origin</text>
                  </g>
                )
              }
              const fixed = sk.constraints.some((c) => c.type === 'fix' && c.point === id)
              return <rect key={id} x={s[0] - 3.5} y={s[1] - 3.5} width={7} height={7} className={`sk-pt ${isSel(id) ? 'sel' : ''} ${hover === id ? 'hover' : ''} ${fixed ? 'fixed' : ''} ${pending.includes(id) ? 'pending' : ''}`} />
            })}

            {cursor && pending.length > 0 && (tool === 'line' || tool === 'rect' || tool === 'spline') && (() => {
              const a = toScreen(sk.points[pending[pending.length - 1]]), b = toScreen(cursor)
              return tool !== 'rect'
                ? <line x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} className="sk-rubber" />
                : <rect x={Math.min(a[0], b[0])} y={Math.min(a[1], b[1])} width={Math.abs(b[0] - a[0])} height={Math.abs(b[1] - a[1])} className="sk-rubber" fill="none" />
            })()}
          </svg>

          <div className="sk-side">
            <h2>Constraints</h2>
            <p className="small sk-selinfo">
              {sel.length
                ? <>Selected: {sel.map((id) => id === 'origin' ? 'origin' : id in sk.points ? `point ${id}` : `${sk.entities.find((e) => e.id === id)?.type ?? ''} ${id}`).join(', ')}</>
                : tool === 'select'
                  ? 'Click points or curves (they light up on hover). Pick two to relate them, e.g. point + origin → Coincident.'
                  : 'Drawing. Press Esc to finish, Esc again for Select, or Shift-click geometry to select it now.'}
            </p>
            <div className="sk-buttons">
              {buttons.map(([label, enabled, fn]) => <button key={label} disabled={!enabled} onClick={fn}>{label}</button>)}
            </div>
            <h2>Dimension</h2>
            <input value={dimValue} onChange={(e) => setDimValue(e.target.value)} placeholder="value or =formula" />
            <div className="sk-buttons">
              {dims.map(([label, enabled, fn]) => <button key={label} disabled={!enabled || !dimValue} onClick={fn}>{label}</button>)}
            </div>
            <ul className="sk-cons">
              {sk.constraints.map((c, i) => (
                <li key={i} className={conflicts.has(i) ? 'bad' : ''}>
                  <span>{i + 1}. {describeCon(c)}</span>
                  {'value' in c && (
                    <input defaultValue={String(c.value)} onBlur={(e) => {
                      const v = parseValue(e.target.value)
                      if (v !== c.value) change({ ...sk, constraints: sk.constraints.map((x, j) => j === i ? { ...x, value: v } : x) })
                    }} />
                  )}
                  <button className="link danger" onClick={() => change({ ...sk, constraints: sk.constraints.filter((_, j) => j !== i) })}>✕</button>
                </li>
              ))}
            </ul>
            <div className="sk-finish">
              {editing ? (
                <button className="primary" onClick={() => submit('sketch')} disabled={!solveOut?.converged}>Save sketch</button>
              ) : (
                <>
                  <label>{finish.op === 'revolve' ? 'Angle °' : 'Distance / depth'}<input value={finish.distance} onChange={(e) => setFinish({ ...finish, distance: e.target.value })} /></label>
                  <div className="sk-buttons">
                    <button className="primary" disabled={!solveOut?.converged || !!solveOut?.profile?.error || pathOnly} onClick={() => submit('extrude')}>Extrude</button>
                    <button className="primary" disabled={!solveOut?.converged || !!solveOut?.profile?.error || !model?.entities} onClick={() => submit('cut')}>Cut</button>
                    <button className="primary" disabled={!solveOut?.converged || !!solveOut?.profile?.error} onClick={() => { setFinish({ ...finish, op: 'revolve' }); submit('revolve') }}>Revolve (about {V})</button>
                    <button disabled={!solveOut?.converged} onClick={() => submit('sketch')}>Sketch only</button>
                  </div>
                  <label className="row small"><input type="checkbox" checked={pathOnly} onChange={(e) => setPathOnly(e.target.checked)} /> use as a sweep path or rib (open curves allowed)</label>
                  <div className="sk-buttons">
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
