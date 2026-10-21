// SPDX-License-Identifier: AGPL-3.0-or-later
import { useMemo, useState } from 'react'
import type { Json, Model, SelRef } from './api'
import { parseValue, type Run } from './panels'

/* Manual controls for every supported operation. Each produces exactly the same
   add_feature / template commands the AI uses. */

export type DialogKind =
  | 'template' | 'extrude' | 'cut' | 'revolve' | 'hole' | 'fillet' | 'chamfer' | 'shell'
  | 'pattern_linear' | 'pattern_circular' | 'mirror' | 'sweep' | 'loft' | 'thread'
  | 'sheet_setup' | 'sheet_base' | 'sheet_flange' | 'sheet_hem'
  | 'draft' | 'rib' | 'variable_fillet' | 'hole_wizard'

export const DIALOGS: { kind: DialogKind; label: string; needsSolid: boolean }[] = [
  { kind: 'template', label: 'Create from template', needsSolid: false },
  { kind: 'extrude', label: 'Extrude (add)', needsSolid: false },
  { kind: 'cut', label: 'Extruded cut', needsSolid: true },
  { kind: 'revolve', label: 'Revolve', needsSolid: false },
  { kind: 'sweep', label: 'Sweep (profile along path)', needsSolid: false },
  { kind: 'loft', label: 'Loft (through sections)', needsSolid: false },
  { kind: 'hole', label: 'Hole', needsSolid: true },
  { kind: 'hole_wizard', label: 'Hole Wizard (ISO standard holes)', needsSolid: true },
  { kind: 'rib', label: 'Rib (from an open line)', needsSolid: true },
  { kind: 'draft', label: 'Draft (taper faces)', needsSolid: true },
  { kind: 'variable_fillet', label: 'Variable-radius fillet', needsSolid: true },
  { kind: 'fillet', label: 'Fillet', needsSolid: true },
  { kind: 'chamfer', label: 'Chamfer', needsSolid: true },
  { kind: 'shell', label: 'Shell', needsSolid: true },
  { kind: 'thread', label: 'Thread (on cylindrical face)', needsSolid: true },
  { kind: 'pattern_linear', label: 'Linear pattern', needsSolid: true },
  { kind: 'pattern_circular', label: 'Circular pattern', needsSolid: true },
  { kind: 'mirror', label: 'Mirror', needsSolid: true },
  { kind: 'sheet_setup', label: 'Sheet metal: parameters', needsSolid: false },
  { kind: 'sheet_base', label: 'Sheet metal: base flange', needsSolid: false },
  { kind: 'sheet_flange', label: 'Sheet metal: edge flange', needsSolid: true },
  { kind: 'sheet_hem', label: 'Sheet metal: hem', needsSolid: true },
]

const NAMED_FACES: Record<string, string> = { top: '>Z', bottom: '<Z', front: '<Y', back: '>Y', left: '<X', right: '>X' }

// Hole Wizard sizes per type (the backend tables in cadcore/holes.py are authoritative and reject anything else)
const WIZARD_SIZES: Record<string, string[]> = {
  clearance: ['M1.6', 'M2', 'M2.5', 'M3', 'M4', 'M5', 'M6', 'M8', 'M10', 'M12', 'M14', 'M16', 'M18', 'M20', 'M22', 'M24', 'M27', 'M30', 'M33', 'M36'],
  counterbore: ['M3', 'M4', 'M5', 'M6', 'M8', 'M10', 'M12', 'M14', 'M16', 'M20', 'M24', 'M30', 'M36'],
  countersink: ['M3', 'M4', 'M5', 'M6', 'M8', 'M10', 'M12', 'M14', 'M16', 'M20'],
  tapped: ['M1.6', 'M2', 'M2.5', 'M3', 'M4', 'M5', 'M6', 'M8', 'M10', 'M12', 'M14', 'M16', 'M18', 'M20', 'M22', 'M24', 'M27', 'M30', 'M36',
    'M3x0.35', 'M4x0.5', 'M5x0.5', 'M6x0.75', 'M8x1', 'M10x1.25', 'M10x1', 'M12x1.5', 'M12x1.25', 'M14x1.5', 'M16x1.5', 'M18x1.5',
    'M20x1.5', 'M20x2', 'M22x1.5', 'M24x2', 'M27x2', 'M30x2', 'M36x3'],
}
const WIZARD_NOTE: Record<string, string> = {
  clearance: 'Clearance hole per ISO 273 (close / normal / loose = fine / medium / coarse series).',
  counterbore: 'Counterbore for an ISO 4762 socket head cap screw: Ø per DIN 974-1, depth = head height (head flush).',
  countersink: '90° countersink for an ISO 10642 countersunk screw: Ø = theoretical head Ø (head flush).',
  tapped: 'Tap drill per ISO 2306 (coarse) or d − P (fine), with a cosmetic thread.',
}

type Shape = Record<string, string>
const SHAPE_FIELDS: Record<string, string[]> = {
  rect: ['w', 'h', 'x', 'y', 'r', 'angle'],
  circle: ['d', 'x', 'y'],
  slot: ['length', 'w', 'x', 'y', 'angle'],
  regular_polygon: ['d', 'n', 'x', 'y', 'angle'],
  polygon: ['points', 'r'],
}

function nextId(model: Model | null, base: string) {
  let n = 1
  while (model?.spec.features.some((f) => f.id === `${base}_${n}`)) n++
  return `${base}_${n}`
}

function num(s: string | undefined): Json | undefined {
  if (s === undefined || s.trim() === '') return undefined
  return parseValue(s)
}

/** "10,10; 90,10" -> [[10,10],[90,10]] (entries may be formulas). */
function parsePairs(text: string): Json[][] {
  return text.split(';').map((p) => p.trim()).filter(Boolean).map((p) => p.split(',').map((x) => parseValue(x)))
}

export default function FeatureDialog({ kind, model, selection, run, onClose }: {
  kind: DialogKind; model: Model | null; selection: SelRef[]; run: Run; onClose: () => void
}) {
  const selFace = selection.find((s) => s.kind === 'face' && s.type === 'PLANE')
  const selEdges = selection.filter((s) => s.kind === 'edge')
  const selFaces = selection.filter((s) => s.kind === 'face')
  const selVertex = selection.find((s) => s.kind === 'vertex')
  const sketches = (model?.spec.features ?? []).filter((f) => f.type === 'sketch' && !f.params.path_only)
  const pathSketches = (model?.spec.features ?? []).filter((f) => f.type === 'sketch')
  const selCyl = selection.find((s) => s.kind === 'face' && s.type === 'CYLINDER')
  const toolFeatures = (model?.spec.features ?? []).filter((f) => ['extrude', 'revolve', 'hole', 'pattern_linear', 'pattern_circular', 'mirror'].includes(f.type))

  const [id, setId] = useState(() => nextId(model, kind === 'cut' ? 'cut' : kind === 'template' ? 'part' : kind === 'hole_wizard' ? 'hole' : kind))
  const [v, setV] = useState<Record<string, string>>(() => ({
    template: 'plate', length: '100', width: '60', thickness: '5', height: '60', segments: '20x30, 12x50',
    od: '100', id_: '40', pcd: '75', n: '6', bolt_d: '9',
    plane: selFace ? 'selected' : kind === 'extrude' || kind === 'revolve' ? 'XY' : 'top', offset: '0',
    distance: kind === 'cut' ? '2' : '10', both: '', through: '', axis: 'v', angle: '360',
    diameter: '6', depth: 'through', positions: '10,10', cbore_d: '', cbore_depth: '', csk_d: '', csk_angle: '90',
    edges: selEdges.length ? 'selected' : '|Z', radius: '2', faces: selFaces.length ? 'selected' : '>Z', wall: '2',
    feature: toolFeatures[toolFeatures.length - 1]?.id ?? '', count: '3', step: '20,0,0', count2: '1', step2: '0,20,0',
    axis_point: '0,0,0', axis_dir: '0,0,1', mplane: 'YZ', moffset: '0',
    thread: '', thread_depth: '', designation: 'M8', tlength: '', tfrom: 'start', modeled: '',
    profile_src: sketches.length ? sketches[0].id : 'shapes', path_src: pathSketches.length ? 'sketch' : 'points',
    path_sketch: pathSketches[0]?.id ?? '', path_points: '0,0,0; 0,0,50', helix_pitch: '5', helix_height: '20', helix_radius: '10',
    transition: 'right', op: 'add', sections: sketches.map((f) => f.id).join(', '), ruled: '',
    sm_t: String(model?.spec.sheet?.thickness ?? 2), sm_r: String(model?.spec.sheet?.bend_radius ?? 2), sm_k: String(model?.spec.sheet?.k_factor ?? 0.44),
    fl_len: '20', fl_angle: '90', fl_dir: 'up', fl_ts: '0', fl_te: '0', fl_r: '',
    hem_type: 'closed', hem_gap: '1', hem_len: '10',
    dr_faces: selFaces.length ? 'selected' : '#Z', dr_angle: '3', nplane: 'XY', noffset: '0', dr_flip: '',
    rib_src: pathSketches.find((f) => f.params.path_only)?.id ?? 'points', rib_points: '5,40; 60,5', rib_t: '4', rib_dir: 'parallel', rib_side: 'both', rib_flip: '',
    r1: '2', r2: '5', vf_points: '', vf_rev: '',
    hw_type: 'counterbore', hw_size: 'M6', hw_fit: 'normal',
  }))
  const [shapes, setShapes] = useState<Shape[]>([
    kind === 'revolve' ? { type: 'rect', w: '10', h: '40', x: '15', y: '20', mode: 'add' } : { type: 'rect', w: '40', h: '20', x: '0', y: '0', mode: 'add' },
  ])
  const [error, setError] = useState<string | null>(null)
  const set = (k: string) => (e: { target: { value: string } }) => setV({ ...v, [k]: e.target.value })

  const planeSpec = (): Json => {
    if (v.plane === 'selected' && selFace) return { face_at: { point: selFace.point, normal: selFace.normal! } }
    if (v.plane in NAMED_FACES) return { face: NAMED_FACES[v.plane] }
    return { base: v.plane, offset: num(v.offset) ?? 0 }
  }
  const shapeSpec = (): Json[] => shapes.map((s) => {
    const out: Record<string, Json> = { type: s.type }
    for (const k of SHAPE_FIELDS[s.type]) {
      if (k === 'points') out.points = parsePairs(s.points ?? '')
      else if (s[k]?.trim()) out[k] = parseValue(s[k])
    }
    if (s.mode === 'subtract') out.mode = 'subtract'
    return out
  })
  const triple = (s: string) => s.split(',').map((x) => parseValue(x))
  const selector = (which: 'edges' | 'faces', text: string): Json => {
    const sel = which === 'edges' ? selEdges : selFaces
    return text === 'selected' ? { near: sel.map((s) => s.point) } : text
  }

  const submit = async () => {
    setError(null)
    let ok = false
    if (kind === 'sheet_setup') {
      ok = await run('set_sheet_metal', { thickness: parseValue(v.sm_t), bend_radius: parseValue(v.sm_r), k_factor: Number(v.sm_k) })
      if (ok) onClose()
      return
    }
    if (kind === 'template') {
      const t = v.template
      if (t === 'plate') ok = await run('create_plate', { length: +v.length, width: +v.width, thickness: +v.thickness })
      if (t === 'l_bracket') ok = await run('create_l_bracket', { length: +v.length, width: +v.width, thickness: +v.thickness, height: +v.height })
      if (t === 'shaft') ok = await run('create_shaft', { segments: v.segments.split(',').map((s) => { const [d, l] = s.toLowerCase().split('x').map(Number); return { diameter: d, length: l } }) })
      if (t === 'flange') ok = await run('create_flange', { outer_diameter: +v.od, inner_diameter: +v.id_, thickness: +v.thickness, bolt_circle_diameter: +v.pcd, bolt_count: +v.n, bolt_diameter: +v.bolt_d })
    } else {
      let type: string = kind
      let params: Record<string, Json> = {}
      if (kind === 'extrude' || kind === 'cut') {
        type = 'extrude'
        params = { plane: planeSpec(), shapes: shapeSpec(), op: kind === 'cut' ? 'cut' : 'add' }
        if (v.through === 'yes' && kind === 'cut') params.through = true
        else params.distance = num(v.distance)!
        if (v.both === 'yes') params.both = true
      } else if (kind === 'revolve') {
        params = { plane: planeSpec(), shapes: shapeSpec(), axis: v.axis, angle: num(v.angle)! }
      } else if (kind === 'hole') {
        params = { plane: planeSpec(), diameter: num(v.diameter)!, depth: num(v.depth) ?? 'through', positions: parsePairs(v.positions) }
        if (v.cbore_d.trim()) Object.assign(params, { cbore_diameter: num(v.cbore_d)!, cbore_depth: num(v.cbore_depth)! })
        if (v.csk_d.trim()) Object.assign(params, { csk_diameter: num(v.csk_d)!, csk_angle: num(v.csk_angle)! })
        if (v.thread.trim()) {
          params.thread = v.thread.trim()
          if (!v.diameter.trim()) delete params.diameter
          if (v.thread_depth.trim()) params.thread_depth = num(v.thread_depth)!
        }
      } else if (kind === 'sweep') {
        const profile: Record<string, Json> = v.profile_src === 'shapes' ? { plane: planeSpec(), shapes: shapeSpec() } : { sketch: v.profile_src }
        const path: Json = v.path_src === 'sketch' ? { sketch: v.path_sketch }
          : v.path_src === 'helix' ? { helix: { pitch: num(v.helix_pitch)!, height: num(v.helix_height)!, radius: num(v.helix_radius)! } }
          : { points: v.path_points.split(';').map((p) => p.trim()).filter(Boolean).map((p) => p.split(',').map((x) => parseValue(x))), type: v.path_src === 'spline' ? 'spline' : 'polyline' }
        params = { profile, path, transition: v.transition, op: v.op }
      } else if (kind === 'loft') {
        params = { sections: v.sections.split(',').map((x) => x.trim()).filter(Boolean).map((id) => ({ sketch: id })), ruled: v.ruled === 'yes', op: v.op }
      } else if (kind === 'thread') {
        if (!selCyl) { setError('Select a cylindrical face first (Face pick mode).'); return }
        params = { designation: v.designation, face: { near: selCyl.point }, from: v.tfrom, modeled: v.modeled === 'yes' }
        if (v.tlength.trim()) params.length = num(v.tlength)!
      } else if (kind === 'sheet_base') {
        params = v.profile_src !== 'shapes' && sketches.length ? { sketch: v.profile_src } : { plane: planeSpec(), shapes: shapeSpec() }
      } else if (kind === 'sheet_flange') {
        const edge = selEdges[0]
        if (!edge) { setError('Select a straight edge of the base flange first (Edge pick mode).'); return }
        params = { edge: { near: edge.point }, length: num(v.fl_len)!, angle: num(v.fl_angle)!, direction: v.fl_dir,
          trim_start: num(v.fl_ts) ?? 0, trim_end: num(v.fl_te) ?? 0, ...(v.fl_r.trim() ? { bend_radius: num(v.fl_r)! } : {}) }
      } else if (kind === 'sheet_hem') {
        const edge = selEdges[0]
        if (!edge) { setError('Select a straight edge of the base flange first (Edge pick mode).'); return }
        params = { edge: { near: edge.point }, type: v.hem_type, length: num(v.hem_len)!, direction: v.fl_dir,
          trim_start: num(v.fl_ts) ?? 0, trim_end: num(v.fl_te) ?? 0, ...(v.hem_type === 'open' ? { gap: num(v.hem_gap)! } : {}) }
      } else if (kind === 'hole_wizard') {
        type = 'hole'
        params = { plane: planeSpec(), positions: parsePairs(v.positions), depth: num(v.depth) ?? 'through',
          standard: 'ISO', type: v.hw_type, size: v.hw_size, ...(v.hw_type === 'tapped' ? {} : { fit: v.hw_fit }) }
        if (v.hw_type === 'tapped' && v.thread_depth.trim()) params.thread_depth = num(v.thread_depth)!
      } else if (kind === 'draft') {
        const faces: Json = v.dr_faces === 'selected' ? selFaces.map((s) => ({ point: s.point, ...(s.normal ? { normal: s.normal } : {}) })) : v.dr_faces
        const neutral: Json = v.nplane in NAMED_FACES ? { face: NAMED_FACES[v.nplane] } : { base: v.nplane, offset: num(v.noffset) ?? 0 }
        params = { faces, angle: num(v.dr_angle)!, neutral_plane: neutral, ...(v.dr_flip === 'yes' ? { flip: true } : {}) }
      } else if (kind === 'rib') {
        params = v.rib_src === 'points' ? { plane: planeSpec(), points: parsePairs(v.rib_points) } : { sketch: v.rib_src }
        Object.assign(params, { thickness: num(v.rib_t)!, direction: v.rib_dir, side: v.rib_side, ...(v.rib_flip === 'yes' ? { flip: true } : {}) })
      } else if (kind === 'variable_fillet') {
        const edge = selEdges[0]
        if (!edge || selEdges.length !== 1) { setError('Select exactly one edge first (Edge pick mode).'); return }
        params = { edge: { near: edge.point }, r1: num(v.r1)!, r2: num(v.r2)! }
        if (selVertex) params.start = selVertex.point
        if (v.vf_points.trim()) params.points = parsePairs(v.vf_points)
        if (v.vf_rev === 'yes') params.reverse = true
      } else if (kind === 'fillet') {
        params = { edges: selector('edges', v.edges), radius: num(v.radius)! }
      } else if (kind === 'chamfer') {
        params = { edges: selector('edges', v.edges), distance: num(v.radius)! }
      } else if (kind === 'shell') {
        params = { faces: selector('faces', v.faces), thickness: num(v.wall)! }
      } else if (kind === 'pattern_linear') {
        params = { feature: v.feature, count: num(v.count)!, step: triple(v.step), count2: num(v.count2) ?? 1, step2: triple(v.step2) }
      } else if (kind === 'pattern_circular') {
        params = { feature: v.feature, count: num(v.count)!, axis_point: triple(v.axis_point), axis_dir: triple(v.axis_dir), angle: num(v.angle)! }
      } else if (kind === 'mirror') {
        params = { feature: v.feature, plane: v.mplane, offset: num(v.moffset) ?? 0 }
      }
      ok = await run('add_feature', { id, type, params })
    }
    if (ok) onClose()
    else setError('Rejected; see the message in the status bar. The model was not changed.')
  }

  const title = DIALOGS.find((d) => d.kind === kind)?.label
  const field = (k: string, label: string, hint?: string) => (
    <label key={k}>{label}<input value={v[k]} onChange={set(k)} placeholder={hint} /></label>
  )
  const planeField = (allowBase: boolean) => (
    <>
      <label>Plane / face
        <select value={v.plane} onChange={set('plane')}>
          {selFace && <option value="selected">Selected face</option>}
          {Object.keys(NAMED_FACES).map((f) => <option key={f} value={f}>{f} face of part</option>)}
          {allowBase && ['XY', 'YZ', 'XZ'].map((p) => <option key={p} value={p}>{p} plane</option>)}
        </select>
      </label>
      {['XY', 'YZ', 'XZ'].includes(v.plane) && field('offset', 'Offset along normal')}
      <p className="muted small">
        Coordinates use the plane's two global axes: XY → (x, y), YZ → (y, z), XZ → (x, z).
        {v.plane in NAMED_FACES || v.plane === 'selected' ? ' On a face, add grows outward and cuts go into the part.' : ''}
      </p>
    </>
  )

  const shapeEditor = useMemo(() => (
    <div className="shapes">
      {shapes.map((s, i) => (
        <div className="shape" key={i}>
          <div className="row">
            <select value={s.type} onChange={(e) => setShapes(shapes.map((x, j) => j === i ? { ...x, type: e.target.value } : x))}>
              {Object.keys(SHAPE_FIELDS).map((t) => <option key={t} value={t}>{t.replace('_', ' ')}</option>)}
            </select>
            <select value={s.mode} onChange={(e) => setShapes(shapes.map((x, j) => j === i ? { ...x, mode: e.target.value } : x))}>
              <option value="add">add</option><option value="subtract">subtract</option>
            </select>
            <button className="link danger" onClick={() => setShapes(shapes.filter((_, j) => j !== i))} disabled={shapes.length === 1}>✕</button>
          </div>
          <div className="grid3">
            {SHAPE_FIELDS[s.type].map((k) => (
              <label key={k} className={k === 'points' ? 'wide' : ''}>{k === 'points' ? 'points "u,v; u,v; …"' : k}
                <input value={s[k] ?? ''} onChange={(e) => setShapes(shapes.map((x, j) => j === i ? { ...x, [k]: e.target.value } : x))} />
              </label>
            ))}
          </div>
        </div>
      ))}
      <button onClick={() => setShapes([...shapes, { type: 'circle', d: '10', x: '0', y: '0', mode: 'subtract' }])}>+ Add profile shape</button>
      <p className="muted small">Shapes are fixed-geometry profiles with parametric values (formulas allowed). Constraint-based sketches are listed as a separate item on the roadmap.</p>
    </div>
  ), [shapes])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>{title}</h2>
        <div className="form">
          {kind !== 'template' && (
            <label>Feature id<input value={id} onChange={(e) => setId(e.target.value)} /></label>
          )}

          {kind === 'template' && (
            <>
              <select value={v.template} onChange={set('template')}>
                <option value="plate">Plate</option><option value="l_bracket">L-bracket</option>
                <option value="shaft">Stepped shaft</option><option value="flange">Flange with bolt circle</option>
              </select>
              <p className="warn small">Replaces the current part.</p>
              <div className="grid3">
                {(v.template === 'plate' || v.template === 'l_bracket') && [field('length', 'length'), field('width', 'width'), field('thickness', 'thickness')]}
                {v.template === 'l_bracket' && field('height', 'height')}
                {v.template === 'flange' && [field('od', 'outer Ø'), field('id_', 'inner Ø'), field('thickness', 'thickness'), field('pcd', 'bolt circle Ø'), field('n', 'bolt count'), field('bolt_d', 'bolt hole Ø')]}
              </div>
              {v.template === 'shaft' && field('segments', 'Segments (Ø×length, bottom up)')}
            </>
          )}

          {(kind === 'extrude' || kind === 'cut') && (
            <>
              {planeField(true)}
              {shapeEditor}
              <div className="grid3">
                {field('distance', kind === 'cut' ? 'depth' : 'distance')}
                <label>both sides<select value={v.both} onChange={set('both')}><option value="">no</option><option value="yes">yes</option></select></label>
                {kind === 'cut' && <label>through all<select value={v.through} onChange={set('through')}><option value="">no</option><option value="yes">yes</option></select></label>}
              </div>
            </>
          )}

          {kind === 'revolve' && (
            <>
              {planeField(true)}
              {shapeEditor}
              <div className="grid3">
                <label>axis<select value={v.axis} onChange={set('axis')}><option value="v">plane v axis</option><option value="u">plane u axis</option></select></label>
                {field('angle', 'angle °')}
              </div>
            </>
          )}

          {kind === 'hole' && (
            <>
              {planeField(true)}
              <div className="grid3">
                {field('diameter', 'diameter')}{field('depth', 'depth or "through"')}
              </div>
              {field('positions', 'Positions "u,v; u,v; …"', '10,10; 90,10; =length-10,=width-10')}
              <div className="grid3">
                {field('cbore_d', 'counterbore Ø')}{field('cbore_depth', 'cbore depth')}
                {field('csk_d', 'countersink Ø')}{field('csk_angle', 'csk angle °')}
              </div>
            </>
          )}

          {kind === 'sheet_setup' && (
            <>
              <div className="grid3">{field('sm_t', 'thickness t (mm)')}{field('sm_r', 'inside bend radius (mm)')}{field('sm_k', 'K-factor (0–0.5)')}</div>
              <p className="muted small">K-factor locates the neutral axis (0.44 is a common air-bending default; ask your fabricator).
                The flat pattern uses bend allowance BA = θ·(R + K·t).</p>
            </>
          )}
          {kind === 'sheet_base' && (
            <>
              {!model?.spec.sheet && <p className="warn small">Set the sheet-metal parameters first.</p>}
              <label>Profile
                <select value={v.profile_src} onChange={set('profile_src')}>
                  {sketches.map((f) => <option key={f.id} value={f.id}>sketch {f.id}</option>)}
                  <option value="shapes">shapes on a plane…</option>
                </select>
              </label>
              {(v.profile_src === 'shapes' || !sketches.length) && <>{planeField(true)}{shapeEditor}</>}
              <p className="muted small">Extruded to the sheet thickness ({model?.spec.sheet ? String(model.spec.sheet.thickness) : '—'} mm).</p>
            </>
          )}
          {kind === 'sheet_flange' && (
            <>
              <p className={selEdges.length ? 'small' : 'warn small'}>{selEdges.length ? `Edge of ${selEdges[0].feature ?? 'base'} selected (${(selEdges[0].length ?? 0).toFixed(1)} mm).` : 'Select a straight edge on the top or bottom of the base flange (Edge mode).'}</p>
              <div className="grid3">
                {field('fl_len', 'flange length (after bend)')}{field('fl_angle', 'bend angle °')}
                <label>direction<select value={v.fl_dir} onChange={set('fl_dir')}><option value="up">up</option><option value="down">down</option></select></label>
                {field('fl_ts', 'trim at edge start')}{field('fl_te', 'trim at edge end')}{field('fl_r', 'bend radius', 'sheet default')}
              </div>
            </>
          )}

          {kind === 'hole' && (
            <div className="grid3">
              {field('thread', 'thread (e.g. M8)', 'optional')}{field('thread_depth', 'thread depth')}
            </div>
          )}

          {kind === 'sweep' && (
            <>
              <label>Profile
                <select value={v.profile_src} onChange={set('profile_src')}>
                  {sketches.map((f) => <option key={f.id} value={f.id}>sketch {f.id}</option>)}
                  <option value="shapes">shapes on a plane…</option>
                </select>
              </label>
              {v.profile_src === 'shapes' && <>{planeField(true)}{shapeEditor}</>}
              <label>Path
                <select value={v.path_src} onChange={set('path_src')}>
                  {pathSketches.length > 0 && <option value="sketch">sketch</option>}
                  <option value="points">3D polyline</option><option value="spline">3D spline</option><option value="helix">helix (along Z)</option>
                </select>
              </label>
              {v.path_src === 'sketch' && <select value={v.path_sketch} onChange={set('path_sketch')}>{pathSketches.map((f) => <option key={f.id}>{f.id}</option>)}</select>}
              {(v.path_src === 'points' || v.path_src === 'spline') && field('path_points', 'Points "x,y,z; x,y,z; …"')}
              {v.path_src === 'helix' && <div className="grid3">{field('helix_pitch', 'pitch')}{field('helix_height', 'height')}{field('helix_radius', 'radius')}</div>}
              <div className="grid3">
                <label>corners<select value={v.transition} onChange={set('transition')}><option value="right">sharp</option><option value="round">round</option><option value="transformed">transformed</option></select></label>
                <label>operation<select value={v.op} onChange={set('op')}><option value="add">add</option><option value="cut">cut</option></select></label>
              </div>
              <p className="muted small">The path must start on the profile's plane.</p>
            </>
          )}

          {kind === 'loft' && (
            <>
              {field('sections', 'Section sketches in order (comma separated)')}
              <p className="muted small">Available: {sketches.map((f) => f.id).join(', ') || 'none; create one sketch per section first'}. Each must be one closed outline.</p>
              <div className="grid3">
                <label>ruled<select value={v.ruled} onChange={set('ruled')}><option value="">smooth</option><option value="yes">ruled</option></select></label>
                <label>operation<select value={v.op} onChange={set('op')}><option value="add">add</option><option value="cut">cut</option></select></label>
              </div>
            </>
          )}

          {kind === 'thread' && (
            <>
              <p className={selCyl ? 'small' : 'warn small'}>{selCyl ? `Cylindrical face of ${selCyl.feature ?? 'part'} selected (Ø${((selCyl.radius ?? 0) * 2).toFixed(2)}).` : 'Select a cylindrical face (shaft or hole) in Face mode first.'}</p>
              <div className="grid3">
                {field('designation', 'designation', 'M8 or M8x1')}{field('tlength', 'length', 'full face')}
                <label>from<select value={v.tfrom} onChange={set('tfrom')}><option value="start">start of face</option><option value="end">end of face</option></select></label>
                <label>geometry<select value={v.modeled} onChange={set('modeled')}><option value="">cosmetic (annotation)</option><option value="yes">modelled helix</option></select></label>
              </div>
              <p className="muted small">External threads need the face at the major diameter; internal threads need the tap-drill diameter.</p>
            </>
          )}

          {kind === 'hole_wizard' && (
            <>
              {planeField(true)}
              <div className="grid3">
                <label>hole type<select value={v.hw_type} onChange={(e) => {
                  const t = e.target.value
                  setV({ ...v, hw_type: t, hw_size: WIZARD_SIZES[t].includes(v.hw_size) ? v.hw_size : WIZARD_SIZES[t][0] })
                }}>
                  <option value="counterbore">counterbore (ISO 4762)</option><option value="countersink">countersink (ISO 10642)</option>
                  <option value="clearance">clearance (ISO 273)</option><option value="tapped">tapped</option>
                </select></label>
                <label>size<select value={v.hw_size} onChange={set('hw_size')}>{WIZARD_SIZES[v.hw_type].map((x) => <option key={x}>{x}</option>)}</select></label>
                {v.hw_type !== 'tapped' && <label>fit<select value={v.hw_fit} onChange={set('hw_fit')}><option value="close">close</option><option value="normal">normal</option><option value="loose">loose</option></select></label>}
                {field('depth', 'depth or "through"')}
                {v.hw_type === 'tapped' && field('thread_depth', 'thread depth', 'full')}
              </div>
              {field('positions', 'Positions "u,v; u,v; …"', '10,10; 90,10; =length-10,=width-10')}
              <p className="muted small">{WIZARD_NOTE[v.hw_type]}</p>
            </>
          )}

          {kind === 'draft' && (
            <>
              <label>Faces to draft
                <select value={v.dr_faces === 'selected' ? 'selected' : 'selector'} onChange={(e) => setV({ ...v, dr_faces: e.target.value === 'selected' ? 'selected' : '#Z' })}>
                  {selFaces.length > 0 && <option value="selected">{selFaces.length} selected face(s)</option>}
                  <option value="selector">selector</option>
                </select>
              </label>
              {v.dr_faces !== 'selected' && field('dr_faces', 'Selector', '#Z = all faces parallel to Z')}
              <div className="grid3">
                {field('dr_angle', 'draft angle °')}
                <label>neutral plane<select value={v.nplane} onChange={set('nplane')}>
                  {['XY', 'YZ', 'XZ'].map((p) => <option key={p} value={p}>{p} plane</option>)}
                  {Object.keys(NAMED_FACES).map((f) => <option key={f} value={f}>{f} face of part</option>)}
                </select></label>
                {['XY', 'YZ', 'XZ'].includes(v.nplane) && field('noffset', 'plane offset')}
                <label>pull direction<select value={v.dr_flip} onChange={set('dr_flip')}><option value="">plane normal / into the part</option><option value="yes">reversed</option></select></label>
              </div>
              <p className="muted small">Faces keep their size on the neutral plane; a positive angle narrows the part along the pull direction. Faces that cannot be tapered are listed by position.</p>
            </>
          )}

          {kind === 'rib' && (
            <>
              <label>Rib line
                <select value={v.rib_src} onChange={set('rib_src')}>
                  {pathSketches.filter((f) => f.params.path_only).map((f) => <option key={f.id} value={f.id}>sketch {f.id}</option>)}
                  <option value="points">points on a plane…</option>
                </select>
              </label>
              {v.rib_src === 'points' && <>{planeField(true)}{field('rib_points', 'Open line "u,v; u,v; …"')}</>}
              <div className="grid3">
                {field('rib_t', 'thickness')}
                <label>direction<select value={v.rib_dir} onChange={set('rib_dir')}><option value="parallel">parallel to sketch</option><option value="normal">normal to sketch</option></select></label>
                <label>thickness side<select value={v.rib_side} onChange={set('rib_side')}><option value="both">both sides</option><option value="first">first side</option><option value="second">second side</option></select></label>
                <label>region<select value={v.rib_flip} onChange={set('rib_flip')}><option value="">smaller enclosed side</option><option value="yes">other side</option></select></label>
              </div>
              <p className="muted small">The line is extended to the part; the rib fills the region it closes off with the part, and fails if it does not reach material.
                Sketches made with "use as a sweep path or rib" are listed.</p>
            </>
          )}

          {kind === 'variable_fillet' && (
            <>
              <p className={selEdges.length === 1 ? 'small' : 'warn small'}>{selEdges.length === 1 ? `Edge of ${selEdges[0].feature ?? 'part'} selected (${(selEdges[0].length ?? 0).toFixed(1)} mm).` : 'Select exactly one edge (Edge mode).'}
                {selVertex ? ' r1 starts at the selected vertex.' : ' Optionally also select a vertex at the end where r1 applies.'}</p>
              <div className="grid3">
                {field('r1', 'start radius r1')}{field('r2', 'end radius r2')}
                <label>ends<select value={v.vf_rev} onChange={set('vf_rev')}><option value="">as picked</option><option value="yes">swapped</option></select></label>
              </div>
              {field('vf_points', 'Intermediate radii "t,r; t,r" (t = 0…1 along the edge)', 'optional, e.g. 0.5,8')}
            </>
          )}

          {kind === 'sheet_hem' && (
            <>
              <p className={selEdges.length ? 'small' : 'warn small'}>{selEdges.length ? `Edge of ${selEdges[0].feature ?? 'base'} selected (${(selEdges[0].length ?? 0).toFixed(1)} mm).` : 'Select a straight edge on the top or bottom of the base flange (Edge mode).'}</p>
              <div className="grid3">
                <label>hem<select value={v.hem_type} onChange={set('hem_type')}><option value="closed">closed</option><option value="open">open</option></select></label>
                {v.hem_type === 'open' && field('hem_gap', 'inside gap')}
                {field('hem_len', 'hem length (after the fold)')}
                <label>direction<select value={v.fl_dir} onChange={set('fl_dir')}><option value="up">up</option><option value="down">down</option></select></label>
                {field('fl_ts', 'trim at edge start')}{field('fl_te', 'trim at edge end')}
              </div>
              <p className="muted small">A 180° fold back over the sheet. A closed hem is modelled with a 0.02 mm gap so both layers stay one valid solid; the flat pattern adds BA = π·(R + K·t).</p>
            </>
          )}

          {(kind === 'fillet' || kind === 'chamfer') && (
            <>
              <label>Edges
                <select value={v.edges === 'selected' ? 'selected' : 'selector'} onChange={(e) => setV({ ...v, edges: e.target.value === 'selected' ? 'selected' : '|Z' })}>
                  {selEdges.length > 0 && <option value="selected">{selEdges.length} selected edge(s)</option>}
                  <option value="selector">selector</option>
                </select>
              </label>
              {v.edges !== 'selected' && field('edges', 'Selector', '|Z, >Z, all, feature:<id>')}
              {field('radius', kind === 'fillet' ? 'radius' : 'distance')}
              {selEdges.length === 0 && <p className="muted small">Tip: switch the pick mode to Edge and click edges first.</p>}
            </>
          )}

          {kind === 'shell' && (
            <>
              <label>Faces to remove
                <select value={v.faces === 'selected' ? 'selected' : 'selector'} onChange={(e) => setV({ ...v, faces: e.target.value === 'selected' ? 'selected' : '>Z' })}>
                  {selFaces.length > 0 && <option value="selected">{selFaces.length} selected face(s)</option>}
                  <option value="selector">selector</option>
                </select>
              </label>
              {v.faces !== 'selected' && field('faces', 'Selector', '>Z')}
              {field('wall', 'wall thickness')}
            </>
          )}

          {(kind === 'pattern_linear' || kind === 'pattern_circular' || kind === 'mirror') && (
            <label>Feature to repeat
              <select value={v.feature} onChange={set('feature')}>
                {toolFeatures.map((f) => <option key={f.id} value={f.id}>{f.id} ({f.type})</option>)}
              </select>
            </label>
          )}
          {kind === 'pattern_linear' && (
            <div className="grid3">
              {field('count', 'count')}{field('step', 'step dx,dy,dz')}{field('count2', 'count 2')}{field('step2', 'step 2 dx,dy,dz')}
            </div>
          )}
          {kind === 'pattern_circular' && (
            <div className="grid3">
              {field('count', 'count')}{field('angle', 'total angle °')}{field('axis_point', 'axis point x,y,z')}{field('axis_dir', 'axis dir x,y,z')}
            </div>
          )}
          {kind === 'mirror' && (
            <div className="grid3">
              <label>mirror plane<select value={v.mplane} onChange={set('mplane')}><option>YZ</option><option>XZ</option><option>XY</option></select></label>
              {field('moffset', 'plane offset')}
            </div>
          )}

          {error && <p className="err small">{error}</p>}
          <div className="row end">
            <button onClick={onClose}>Cancel</button>
            <button className="primary" onClick={submit}>OK</button>
          </div>
        </div>
      </div>
    </div>
  )
}
