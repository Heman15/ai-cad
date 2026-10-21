// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import {
  ArrowLeftRight, CircleDot, Equal, FlipVertical2, Lock, Magnet, Slash, Trash2, TriangleRight, type LucideIcon,
} from 'lucide-react'
import { fmt, type Model, type SelRef } from './api'

/* PropertyManager panels of the Assembly workspace (one per command) and the assembly data types. */

export interface Comp {
  id: string; file: string; name?: string; fixed: boolean; position: number[]; rotation: number[]; material?: string | null
  mass_kg?: number | null; error?: string; link?: string | null; color?: string | null; hidden?: boolean; transparent?: boolean
}
export interface Joint { name: string; type: string; parent: string; child: string; lower: number | null; upper: number | null; value: number; mimic: string | null }
export interface Robot { name: string; root: string; file: string; geometry: string; joints: Joint[]; links: Record<string, { name: string; mass_kg: number | null }> }
export interface UrdfReport {
  robot: string; links: number; joints: number; parametric_parts: number; mesh_parts: number; skipped: string[]; warnings: string[]; folder: string
  masses: { link: string; mass_kg: number; cad_volume_mm3: number | null; implied_density: number | null }[]
  meshes: Record<string, { source: string; triangles_in: number; faces: number; closed: boolean; decimated_to?: number }>
}
export interface Mate { id: string; type: string; a: { component: string }; b: { component: string }; value: number; aligned: boolean; suppressed?: boolean }
export interface ExplodeStep { id: string; components: string[]; direction: number[]; distance: number }
export interface BomRow { item: number; part: string; file: string; qty: number; material: string | null; mass_each_kg: number | null; mass_total_kg: number | null }
export interface AsmModel {
  spec: { name: string; components: Comp[]; mates: Mate[]; robot: Robot | null; explode: ExplodeStep[] }
  path: string | null
  status: { converged: boolean; dof: number; conflicts: string[] }
  can_undo: boolean; can_redo: boolean
  components: Comp[]
  entities: Model['entities']
  bom: BomRow[]
}
export interface Clash { a: string; b: string; volume_mm3: number; center: number[] }
export interface MassProps {
  mass_kg: number; volume_mm3: number; center_of_mass_mm: number[]; center_from: string; without_mass: string[]
  components: { component: string; part: string; material: string | null; volume_mm3: number; mass_kg: number | null; mass_source: string | null; center_mm: number[] }[]
}
export type Run = (command: string, args: Record<string, unknown>) => Promise<boolean>

export const MATES: Record<string, { label: string; icon: LucideIcon; unit?: string; flip?: boolean }> = {
  coincident: { label: 'Coincident', icon: Magnet, flip: true },
  parallel: { label: 'Parallel', icon: Equal },
  perpendicular: { label: 'Perpendicular', icon: TriangleRight },
  distance: { label: 'Distance', icon: ArrowLeftRight, unit: 'mm', flip: true },
  angle: { label: 'Angle', icon: Slash, unit: '°', flip: true },
  concentric: { label: 'Concentric', icon: CircleDot },
  lock: { label: 'Lock', icon: Lock },
}

export const mateLabel = (m: Mate) => `${MATES[m.type]?.label ?? m.type}${MATES[m.type]?.unit ? ` ${fmt(m.value, 3)}${MATES[m.type].unit === '°' ? '°' : ' mm'}` : ''}${m.aligned ? ' (aligned)' : ''}`

const DIRS: Record<string, number[]> = { '+X': [1, 0, 0], '-X': [-1, 0, 0], '+Y': [0, 1, 0], '-Y': [0, -1, 0], '+Z': [0, 0, 1], '-Z': [0, 0, -1] }
const dirName = (d: number[]) => Object.entries(DIRS).find(([, v]) => v.every((x, i) => Math.abs(x - d[i]) < 1e-6))?.[0] ?? d.map((v) => fmt(v, 3)).join(', ')

/** Direction picker: an axis, or the normal/axis of the selected face. */
function DirectionField({ label, value, onChange, face }: { label: string; value: string; onChange: (v: string) => void; face: number[] | null }) {
  return (
    <label>{label}
      <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
        {Object.keys(DIRS).map((k) => <option key={k} value={k}>{k}</option>)}
        <option value="face" disabled={!face}>Selected face{face ? ` (${face.map((v) => fmt(v, 2)).join(', ')})` : ''}</option>
        <option value="-face" disabled={!face}>Selected face, reversed</option>
      </select>
    </label>
  )
}
const dirOf = (k: string, face: number[] | null) => k === 'face' ? face! : k === '-face' ? face!.map((v) => -v) : DIRS[k]

/** World direction of the first selected face: plane normal or cylinder axis. */
export function faceDirection(selection: SelRef[], asm: AsmModel | null): number[] | null {
  const f = selection.find((s) => s.kind === 'face')
  if (!f) return null
  const e = asm?.entities?.faces[f.index]
  return e?.normal ?? e?.axis_dir ?? null
}

export function MatePanel({ selection, comps, run, onDone }: { selection: SelRef[]; comps: string[]; run: Run; onDone: () => void }) {
  const [dist, setDist] = useState('10')
  const [angle, setAngle] = useState('90')
  const [aligned, setAligned] = useState(false)
  const faces = selection.filter((s) => s.kind === 'face' && s.component)
  const pair = faces.length === 2 && faces[0].component !== faces[1].component
  const planar = pair && faces.every((f) => f.type === 'PLANE')
  const cyl = pair && faces.every((f) => f.type === 'CYLINDER')
  const lockPair = pair ? [faces[0].component!, faces[1].component!] : comps.length === 2 ? comps : null
  const side = (s: SelRef) => ({ component: s.component, face: { point: s.local_point, ...(s.local_normal ? { normal: s.local_normal } : {}) } })
  const enabled: Record<string, boolean> = {
    coincident: planar, parallel: planar || cyl, perpendicular: planar || cyl, distance: planar, angle: planar, concentric: cyl, lock: !!lockPair,
  }
  const add = async (type: string) => {
    const args = type === 'lock' ? { type, a: { component: lockPair![0] }, b: { component: lockPair![1] } }
      : { type, a: side(faces[0]), b: side(faces[1]), value: type === 'angle' ? Number(angle) : Number(dist), aligned }
    if (await run('add_mate', args)) onDone()
  }
  return (
    <>
      <section>
        <h2>Mate selections</h2>
        {faces.length === 0 && <p className="muted small">Click two faces on different components (planar or cylindrical). Lock also works on two selected components.</p>}
        <ol className="mp-picks">
          {faces.map((f, i) => <li key={i}><span className={`pick-dot k${i}`} />{f.type.toLowerCase()} of {f.component}</li>)}
        </ol>
      </section>
      <section>
        <h2>Standard mates</h2>
        <div className="mate-types">
          {Object.entries(MATES).map(([type, { label, icon: Icon }]) => (
            <button key={type} disabled={!enabled[type]} onClick={() => add(type)} title={`Add a ${label.toLowerCase()} mate`}><Icon size={16} /> {label}</button>
          ))}
        </div>
        <div className="grid3">
          <label>distance (mm)<input value={dist} onChange={(e) => setDist(e.target.value)} /></label>
          <label>angle (°)<input value={angle} onChange={(e) => setAngle(e.target.value)} /></label>
          <label className="row small check"><input type="checkbox" checked={aligned} onChange={(e) => setAligned(e.target.checked)} /> Flip alignment</label>
        </div>
        <p className="muted small">Coincident and distance mates put the normals opposite (faces touching); Flip alignment points them the same way.
          Angle is between the face normals.</p>
      </section>
    </>
  )
}

export function MateEditor({ mate, run, onDeleted, conflict }: { mate: Mate; run: Run; onDeleted: () => void; conflict: boolean }) {
  const info = MATES[mate.type]
  const [value, setValue] = useState(String(mate.value))
  useEffect(() => setValue(String(mate.value)), [mate])
  const Icon = info?.icon ?? Magnet
  return (
    <section>
      <h2><span className="row"><Icon size={14} /> {info?.label ?? mate.type} · {mate.id}</span></h2>
      <p className="small">{mate.a.component} ↔ {mate.b.component}{mate.suppressed ? <span className="muted"> · suppressed</span> : ''}</p>
      {conflict && <p className="err small">This mate conflicts with others.</p>}
      {info?.unit && (
        <div className="row">
          <label className="grow">{mate.type === 'angle' ? 'angle (°)' : 'distance (mm)'}
            <input aria-label="Mate value" value={value} onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && run('edit_mate', { id: mate.id, value: Number(value) })} /></label>
          <button onClick={() => run('edit_mate', { id: mate.id, value: Number(value) })}>Apply</button>
        </div>
      )}
      <div className="sk-buttons pm-actions">
        {info?.flip && <button onClick={() => run('flip_mate', { id: mate.id })}><FlipVertical2 size={14} /> Flip</button>}
        <button onClick={() => run('edit_mate', { id: mate.id, suppressed: !mate.suppressed })}>{mate.suppressed ? 'Unsuppress' : 'Suppress'}</button>
        <button className="danger-btn" onClick={async () => { if (await run('remove_mate', { id: mate.id })) onDeleted() }}><Trash2 size={14} /> Delete</button>
      </div>
    </section>
  )
}

export function TransformEditor({ comp, onApply }: { comp: Comp; onApply: (p: number[], r: number[]) => void }) {
  const fmt4 = (v: number[]) => v.map((x) => String(Number(x.toFixed(4))))
  const [p, setP] = useState(fmt4(comp.position))
  const [r, setR] = useState(fmt4(comp.rotation))
  useEffect(() => { setP(fmt4(comp.position)); setR(fmt4(comp.rotation)) }, [comp])
  const cell = (arr: string[], set: (a: string[]) => void, i: number, label: string) => (
    <label key={label}>{label}<input value={arr[i]} onChange={(e) => set(arr.map((x, j) => (j === i ? e.target.value : x)))} /></label>
  )
  return (
    <>
      <div className="grid3">{['x', 'y', 'z'].map((l, i) => cell(p, setP, i, `position ${l}`))}</div>
      <div className="grid3">{['x', 'y', 'z'].map((l, i) => cell(r, setR, i, `rotation ${l}°`))}</div>
      <p className="muted small">Rotation is a rotation vector (axis × angle in degrees). Mates override free components.</p>
      <button onClick={() => onApply(p.map(Number), r.map(Number))}>Apply</button>
    </>
  )
}

export function MovePanel({ comp, rotate, run }: { comp: Comp | undefined; rotate: boolean; run: Run }) {
  const [step, setStep] = useState('10')
  const [ang, setAng] = useState('15')
  if (!comp) return <p className="muted small">Select a component in the tree or the 3D view (Component selection filter).</p>
  if (comp.fixed) {
    return (
      <section>
        <p className="warn small">{comp.id} is fixed{comp.link ? ` (robot link ${comp.link}: its joints place it)` : ''}.</p>
        {!comp.link && <button onClick={() => run('set_fixed', { id: comp.id, fixed: false })}>Float {comp.id}</button>}
      </section>
    )
  }
  const axes: [string, number[]][] = [['X', [1, 0, 0]], ['Y', [0, 1, 0]], ['Z', [0, 0, 1]]]
  const move = (v: number[], sign: number) => run('move_component', { id: comp.id, translate: v.map((x) => x * sign * Number(step)) })
  const turn = (v: number[], sign: number) => run('move_component', { id: comp.id, axis: v, angle: sign * Number(ang) })
  return (
    <>
      <section>
        <h2>{comp.id} <span className="muted">{comp.name}</span></h2>
        <p className="small">Drag the component in the 3D view, or nudge it. Mates re-solve after each move, so only its free directions change.</p>
      </section>
      <section className={rotate ? 'dim' : ''}>
        <h2>Translate</h2>
        <label className="row small">step (mm)<input aria-label="Move step" value={step} onChange={(e) => setStep(e.target.value)} /></label>
        <div className="nudge">
          {axes.map(([n, v]) => (
            <div key={n} className="row"><b>{n}</b>
              <button aria-label={`Move -${n}`} onClick={() => move(v, -1)}>−{n}</button>
              <button aria-label={`Move +${n}`} onClick={() => move(v, 1)}>+{n}</button></div>
          ))}
        </div>
      </section>
      <section className={rotate ? '' : 'dim'}>
        <h2>Rotate (about its centre)</h2>
        <label className="row small">step (°)<input aria-label="Rotate step" value={ang} onChange={(e) => setAng(e.target.value)} /></label>
        <div className="nudge">
          {axes.map(([n, v]) => (
            <div key={n} className="row"><b>{n}</b>
              <button aria-label={`Rotate -${n}`} onClick={() => turn(v, -1)}>↻ −{n}</button>
              <button aria-label={`Rotate +${n}`} onClick={() => turn(v, 1)}>↺ +{n}</button></div>
          ))}
        </div>
      </section>
      <section>
        <h2>Position</h2>
        <TransformEditor comp={comp} onApply={(position, rotation) => run('set_transform', { id: comp.id, position, rotation })} />
      </section>
    </>
  )
}

export function PatternPanel({ comp, circular, faceDir, facePoint, run }: {
  comp: Comp | undefined; circular: boolean; faceDir: number[] | null; facePoint: number[] | null; run: Run
}) {
  const [dir, setDir] = useState(circular ? '+Z' : '+X')
  const [count, setCount] = useState('3')
  const [spacing, setSpacing] = useState('50')
  const [angle, setAngle] = useState('360')
  const [point, setPoint] = useState('0, 0, 0')
  useEffect(() => { if (circular && facePoint) setPoint(facePoint.map((v) => fmt(v, 3)).join(', ')) }, [circular, facePoint])
  if (!comp) return <p className="muted small">Select the component to pattern.</p>
  const ok = () => run('pattern_component', circular
    ? { id: comp.id, type: 'circular', count: Number(count), axis: dirOf(dir, faceDir), point: point.split(',').map(Number), angle: Number(angle) }
    : { id: comp.id, type: 'linear', count: Number(count), direction: dirOf(dir, faceDir), spacing: Number(spacing) })
  return (
    <section>
      <h2>{circular ? 'Circular' : 'Linear'} pattern of {comp.id}</h2>
      <div className="form">
        <DirectionField label={circular ? 'Axis' : 'Direction'} value={dir} onChange={setDir} face={faceDir} />
        {circular && <label>Axis point (x, y, z)<input value={point} onChange={(e) => setPoint(e.target.value)} /></label>}
        <label>Instances (incl. the original)<input aria-label="Instances" value={count} onChange={(e) => setCount(e.target.value)} /></label>
        {circular ? <label>Total angle (°)<input value={angle} onChange={(e) => setAngle(e.target.value)} /></label>
          : <label>Spacing (mm)<input aria-label="Spacing" value={spacing} onChange={(e) => setSpacing(e.target.value)} /></label>}
      </div>
      <p className="muted small">Copies are independent free components (not linked to the pattern). {circular ? 'Pick a cylindrical face to use its axis.' : ''}</p>
      <button className="primary" onClick={ok}>Create pattern</button>
    </section>
  )
}

export function ExplodePanel({ asm, comps, exploded, setExploded, faceDir, run }: {
  asm: AsmModel; comps: string[]; exploded: boolean; setExploded: (v: boolean) => void; faceDir: number[] | null; run: Run
}) {
  const [dir, setDir] = useState('+Z')
  const [dist, setDist] = useState('50')
  const [scale, setScale] = useState('1')
  const steps = asm.spec.explode ?? []
  return (
    <>
      <section>
        <div className="row">
          <button className="primary" onClick={() => setExploded(!exploded)} disabled={!steps.length}>{exploded ? 'Collapse' : 'Explode'}</button>
          <span className="spacer" />
          <label className="row small">scale<input aria-label="Auto explode scale" className="short" value={scale} onChange={(e) => setScale(e.target.value)} /></label>
          <button onClick={async () => { if (await run('auto_explode', { scale: Number(scale) })) setExploded(true) }} disabled={!asm.components.length}>Auto Explode</button>
        </div>
        <p className="muted small">Exploded views only move the display; mates and positions are unchanged. Steps are saved with the assembly.</p>
      </section>
      <section>
        <h2>Explode steps {steps.length > 0 && <button className="link danger" onClick={() => run('clear_explode', {})}>delete all</button>}</h2>
        {!steps.length && <p className="muted small">No steps yet.</p>}
        <ul className="findings steps-list">
          {steps.map((s) => (
            <li key={s.id}>
              <span title={s.components.join(', ')}><b>{s.id}</b> {s.components.length > 2 ? `${s.components.length} components` : s.components.join(', ')} → {dirName(s.direction)}</span>
              <span className="row">
                <input aria-label={`Distance ${s.id}`} className="short" defaultValue={fmt(s.distance, 2)} key={s.distance}
                  onBlur={(e) => Number(e.target.value) !== s.distance && run('edit_explode_step', { id: s.id, distance: Number(e.target.value) })} /> mm
                <button className="link danger" aria-label={`Delete ${s.id}`} onClick={() => run('remove_explode_step', { id: s.id })}><Trash2 size={13} /></button>
              </span>
            </li>
          ))}
        </ul>
      </section>
      <section>
        <h2>New step</h2>
        <p className="small">{comps.length ? <>Components: <b>{comps.join(', ')}</b></> : <span className="muted">Select components (Component filter; Shift adds).</span>}</p>
        <div className="grid3">
          <DirectionField label="Direction" value={dir} onChange={setDir} face={faceDir} />
          <label>Distance (mm)<input aria-label="Explode distance" value={dist} onChange={(e) => setDist(e.target.value)} /></label>
          <label>&nbsp;<button disabled={!comps.length} onClick={async () => {
            if (await run('add_explode_step', { components: comps, direction: dirOf(dir, faceDir), distance: Number(dist) })) setExploded(true)
          }}>Add Step</button></label>
        </div>
      </section>
    </>
  )
}

export function InterferencePanel({ clashes, onRun, onFocus, busy }: { clashes: Clash[] | null; onRun: () => void; onFocus: (c: Clash) => void; busy: boolean }) {
  return (
    <section>
      <h2>Interference <button className="link" onClick={onRun} disabled={busy}>{busy ? 'calculating…' : 'calculate'}</button></h2>
      {!clashes && <p className="muted small">{busy ? 'Checking every pair of components…' : 'Not calculated yet.'}</p>}
      {clashes && (clashes.length === 0 ? <p className="ok small">No overlapping volume between components (touching is allowed).</p> : (
        <ul className="findings">{clashes.map((c, i) => <li key={i} className="err" onClick={() => onFocus(c)}>{c.a} ∩ {c.b}: {fmt(c.volume_mm3, 1)} mm³</li>)}</ul>
      ))}
      <p className="muted small">Visual meshes of imported robots often overlap at the joints.</p>
    </section>
  )
}

export function MassPanel({ mass }: { mass: MassProps | null }) {
  if (!mass) return <p className="muted small">Calculating…</p>
  return (
    <section>
      <h2>Mass properties</h2>
      <dl className="stats">
        <dt>Mass</dt><dd>{fmt(mass.mass_kg * 1000, 2)} g</dd>
        <dt>Volume</dt><dd>{fmt(mass.volume_mm3, 1)} mm³</dd>
        <dt>Centre of mass</dt><dd>{mass.center_of_mass_mm.map((v) => fmt(v, 2)).join(', ')} mm</dd>
      </dl>
      {mass.center_from === 'volume' && <p className="warn small">No component has a mass; the centre shown is the centre of volume.</p>}
      {mass.without_mass.length > 0 && mass.center_from === 'mass' && <p className="warn small">Without material (not in the mass): {mass.without_mass.join(', ')}</p>}
      <table className="dims">
        <thead><tr><th>component</th><th>material</th><th>mass</th><th>volume</th></tr></thead>
        <tbody>{mass.components.map((c) => (
          <tr key={c.component}><td>{c.component}</td><td>{c.mass_source === 'urdf' ? 'URDF mass' : c.mass_source === 'link' ? 'in link mass' : c.material ?? '—'}</td>
            <td>{c.mass_kg != null ? `${fmt(c.mass_kg * 1000, 1)} g` : '—'}</td><td>{fmt(c.volume_mm3 / 1000, 2)} cm³</td></tr>
        ))}</tbody>
      </table>
      <p className="muted small">The red marker in the 3D view is the centre of mass.</p>
    </section>
  )
}

export function BomPanel({ bom, onExport }: { bom: BomRow[]; onExport: () => void }) {
  return (
    <section>
      <h2>Bill of materials <button className="link" onClick={onExport} disabled={!bom.length}>export CSV</button></h2>
      <table className="dims bom">
        <thead><tr><th>#</th><th>part</th><th>qty</th><th>material</th><th>mass</th></tr></thead>
        <tbody>
          {bom.map((r) => <tr key={r.item}><td>{r.item}</td><td title={r.file}>{r.part}</td><td>{r.qty}</td><td>{r.material ?? '—'}</td>
            <td>{r.mass_total_kg != null ? `${fmt(r.mass_total_kg * 1000, 1)} g` : '—'}</td></tr>)}
        </tbody>
      </table>
      {bom.length > 0 && <p className="small">Total: {fmt(bom.reduce((s, r) => s + (r.mass_total_kg ?? 0), 0) * 1000, 1)} g
        {bom.some((r) => r.mass_total_kg == null) ? ' (parts without material excluded)' : ''}</p>}
    </section>
  )
}

export function UrdfReportPanel({ report, onHide }: { report: UrdfReport; onHide: () => void }) {
  return (
    <section className="urdf-report">
      <h2>URDF import · {report.robot} <button className="link" onClick={onHide}>hide</button></h2>
      <p className="small">{report.links} links, {report.joints} joints → <b>{report.parametric_parts}</b> editable parts (box, cylinder, sphere)
        and <b>{report.mesh_parts}</b> solids from meshes. Files written to <code>{report.folder}</code>.</p>
      {Object.entries(report.meshes).length > 0 && (
        <ul className="findings">{Object.entries(report.meshes).map(([f, m]) => (
          <li key={f}><span>{f}: {m.triangles_in} triangles → {m.faces} faces{m.decimated_to ? ` (reduced to ${m.decimated_to})` : ''}</span>
            <span className={m.closed ? 'ok' : 'warn'}>{m.closed ? 'solid' : 'surfaces'}</span></li>))}</ul>
      )}
      {report.skipped.map((t, i) => <p key={i} className="err small">Skipped: {t}</p>)}
      {report.warnings.map((t, i) => <p key={i} className="warn small">{t}</p>)}
      {report.masses.length > 0 && (
        <table className="dims">
          <thead><tr><th>link</th><th>URDF mass</th><th>CAD volume</th><th>implied density</th></tr></thead>
          <tbody>{report.masses.map((m) => <tr key={m.link}><td>{m.link}</td><td>{fmt(m.mass_kg, 3)} kg</td>
            <td>{m.cad_volume_mm3 != null ? `${fmt(m.cad_volume_mm3 / 1000, 1)} cm³` : '—'}</td>
            <td className={m.implied_density != null && (m.implied_density < 200 || m.implied_density > 20000) ? 'warn' : ''}>{m.implied_density != null ? `${m.implied_density} kg/m³` : '—'}</td></tr>)}</tbody>
        </table>
      )}
      <p className="muted small">Mesh parts keep their shape but have no feature history, so only box, cylinder and sphere links are parametric.
        Visual meshes often overlap at the joints, which the interference check will show.</p>
    </section>
  )
}

export function ComponentPanel({ comp, run }: { comp: Comp; run: Run }) {
  if (comp.error) return <section><h2>{comp.id}</h2><p className="err small">{comp.error}</p></section>
  return (
    <>
      <section>
        <h2>{comp.id} <span className="muted">{comp.name}</span></h2>
        <dl className="stats">
          <dt>File</dt><dd title={comp.file}>{comp.file.split('/').pop()}</dd>
          <dt>Material</dt><dd>{comp.material ?? '—'}</dd>
          <dt>Mass</dt><dd>{comp.mass_kg != null ? `${fmt(comp.mass_kg * 1000, 1)} g` : '—'}</dd>
          {comp.link && <><dt>Robot link</dt><dd>{comp.link}</dd></>}
          <dt>State</dt><dd>{comp.fixed ? 'fixed' : 'floating'}{comp.hidden ? ', hidden' : ''}{comp.transparent ? ', transparent' : ''}</dd>
        </dl>
        <div className="sk-buttons pm-actions">
          {!comp.link && <button onClick={() => run('set_fixed', { id: comp.id, fixed: !comp.fixed })}>{comp.fixed ? 'Float' : 'Fix'}</button>}
          <button onClick={() => run('set_visibility', { ids: comp.id, hidden: !comp.hidden })}>{comp.hidden ? 'Show' : 'Hide'}</button>
          <button onClick={() => run('set_visibility', { ids: comp.id, transparent: !comp.transparent })}>{comp.transparent ? 'Opaque' : 'Transparent'}</button>
        </div>
      </section>
      {!comp.link && (
        <section>
          <h2>Position</h2>
          <TransformEditor comp={comp} onApply={(position, rotation) => run('set_transform', { id: comp.id, position, rotation })} />
        </section>
      )}
    </>
  )
}
