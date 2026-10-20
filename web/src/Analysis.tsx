// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from 'react'
import { fmt, type Model, type Overlay, type SelRef } from './api'
import type { Run } from './panels'
import { turbo } from './Viewport'

/* Manufacturability screening and linear-static stress analysis of the current part. */

interface Finding { rule: string; severity: string; message: string; location: number[]; value: number; threshold: number | null }
interface DfmResult { label: string; findings: Finding[]; summary: { setup_note?: string }; disclaimer: string; rules: Record<string, number> }
interface FeaResult {
  max_von_mises_mpa: number; max_von_mises_at: number[]; max_displacement_mm: number; max_displacement_at: number[]
  safety_factor?: number; yield_mpa?: number; equilibrium_error: number; nodes: number; elements: number; mesh_size_mm: number; solve_s: number
  convergence?: { max_displacement_change_pct: number; max_von_mises_change_pct: number; fine_elements: number }
  assumptions: string[]
  display: { vertices: number[]; triangles: number[]; von_mises: number[]; displacement: number[] }
}

async function post<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const d = await r.json()
  if (!r.ok) throw new Error(d.detail ?? `failed (${r.status})`)
  return d
}

export default function AnalysisPanel({ model, selection, run, setOverlay, setMarkers }: {
  model: Model; selection: SelRef[]; run: Run; setOverlay: (o: Overlay | null) => void; setMarkers: (m: number[][]) => void
}) {
  const [process, setProcess] = useState('cnc_milling')
  const [dfm, setDfm] = useState<DfmResult | null>(null)
  const [fea, setFea] = useState<FeaResult | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [load, setLoad] = useState({ kind: 'force', fx: '0', fy: '0', fz: '-100', p: '0.1' })
  const [meshSize, setMeshSize] = useState('')
  const [conv, setConv] = useState(false)
  const [view, setView] = useState<'stress' | 'deformed' | 'off'>('stress')
  const m = model.spec.material
  const [mat, setMat] = useState({ E: String(m?.E ?? ''), nu: String(m?.nu ?? ''), ys: String(m?.yield_strength ?? '') })
  const setup = model.spec.analysis ?? { supports: [], loads: [] }
  const faces = selection.filter((s) => s.kind === 'face').map((s) => ({ point: s.point, ...(s.normal ? { normal: s.normal } : {}) }))

  const saveSetup = (supports: typeof setup.supports, loads: typeof setup.loads) => run('set_analysis_setup', { supports, loads })

  const runDfm = async () => {
    setBusy('dfm'); setError(null)
    try {
      const r = await post<DfmResult>('/api/analysis/dfm', { process })
      setDfm(r)
      setMarkers(r.findings.map((f) => f.location))
    } catch (e) { setError((e as Error).message) } finally { setBusy(null) }
  }

  const show = (r: FeaResult, mode: typeof view) => {
    if (mode === 'off') { setOverlay(null); return }
    const vm = r.display.von_mises
    const size = Math.max(...model.bbox_mm!)
    setOverlay({
      vertices: r.display.vertices, triangles: r.display.triangles, values: vm, displacement: r.display.displacement,
      deformScale: mode === 'deformed' && r.max_displacement_mm > 0 ? (0.08 * size) / r.max_displacement_mm : 0,
      min: Math.min(...vm), max: Math.max(...vm),
    })
  }

  const runFea = async () => {
    setBusy('fea'); setError(null)
    try {
      const r = await post<FeaResult>('/api/analysis/fea', { mesh_size: meshSize ? Number(meshSize) : null, check_convergence: conv })
      setFea(r)
      show(r, view)
      setMarkers([r.max_von_mises_at])
    } catch (e) { setError((e as Error).message) } finally { setBusy(null) }
  }

  const sev = (s: string) => (s === 'warning' ? 'warn' : s === 'error' ? 'err' : 'muted')
  const vmMax = fea ? fea.max_von_mises_mpa : 0

  return (
    <>
      <section>
        <h2>Manufacturability</h2>
        <div className="row">
          <select value={process} onChange={(e) => setProcess(e.target.value)}>
            <option value="cnc_milling">CNC milling (3-axis)</option><option value="fdm_printing">FDM 3D printing</option>
            {model.spec.sheet && <option value="sheet_metal">Sheet metal</option>}
          </select>
          <button className="primary" disabled={!!busy} onClick={runDfm}>{busy === 'dfm' ? 'Checking…' : 'Check'}</button>
        </div>
        {dfm && (
          <>
            {dfm.summary.setup_note && <p className="small">{dfm.summary.setup_note}</p>}
            {dfm.findings.length === 0 && <p className="ok small">No issues found by these rules.</p>}
            <ul className="findings">
              {dfm.findings.map((f, i) => (
                <li key={i} className={sev(f.severity)} onClick={() => setMarkers([f.location])}>{f.message}</li>
              ))}
            </ul>
            <p className="muted small">Rules: {Object.entries(dfm.rules).map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`).join(' · ')}. {dfm.disclaimer}</p>
          </>
        )}
      </section>

      <section>
        <h2>Strength (linear static FEA)</h2>
        <h3>Material</h3>
        {!m ? <p className="warn small">Assign a material in Properties first.</p> : (
          <div className="grid3">
            <label>E (MPa)<input value={mat.E} onChange={(e) => setMat({ ...mat, E: e.target.value })} /></label>
            <label>ν<input value={mat.nu} onChange={(e) => setMat({ ...mat, nu: e.target.value })} /></label>
            <label>yield (MPa)<input value={mat.ys} onChange={(e) => setMat({ ...mat, ys: e.target.value })} /></label>
            <button className="wide" onClick={() => run('assign_material', { name: m.name, density: m.density, E: Number(mat.E), nu: Number(mat.nu), yield_strength: mat.ys ? Number(mat.ys) : undefined })}>
              Apply ({m.name}; typical values — check your datasheet)</button>
          </div>
        )}
        <h3>Fixed supports ({setup.supports.length})</h3>
        <ul className="findings">
          {setup.supports.map((s, i) => (
            <li key={i}>face at ({s.face.point.map((c) => fmt(c, 1)).join(', ')})
              <button className="link danger" onClick={() => saveSetup(setup.supports.filter((_, j) => j !== i), setup.loads)}>✕</button></li>
          ))}
        </ul>
        <button disabled={!faces.length} onClick={() => saveSetup([...setup.supports, ...faces.map((f) => ({ face: f }))], setup.loads)}>
          Add {faces.length || ''} selected face(s) as fixed</button>
        <h3>Loads ({setup.loads.length})</h3>
        <ul className="findings">
          {setup.loads.map((l, i) => (
            <li key={i}>{l.force ? `F = (${l.force.join(', ')}) N` : `p = ${l.pressure} MPa`} on face at ({l.face.point.map((c) => fmt(c, 1)).join(', ')})
              <button className="link danger" onClick={() => saveSetup(setup.supports, setup.loads.filter((_, j) => j !== i))}>✕</button></li>
          ))}
        </ul>
        <div className="grid3">
          <label>type<select value={load.kind} onChange={(e) => setLoad({ ...load, kind: e.target.value })}><option value="force">total force</option><option value="pressure">pressure</option></select></label>
          {load.kind === 'force' ? <>
            <label>Fx (N)<input value={load.fx} onChange={(e) => setLoad({ ...load, fx: e.target.value })} /></label>
            <label>Fy (N)<input value={load.fy} onChange={(e) => setLoad({ ...load, fy: e.target.value })} /></label>
            <label>Fz (N)<input value={load.fz} onChange={(e) => setLoad({ ...load, fz: e.target.value })} /></label>
          </> : <label>p (MPa, into face)<input value={load.p} onChange={(e) => setLoad({ ...load, p: e.target.value })} /></label>}
        </div>
        <button disabled={!faces.length} onClick={() => saveSetup(setup.supports, [...setup.loads, ...faces.map((f) => (
          load.kind === 'force' ? { face: f, force: [Number(load.fx), Number(load.fy), Number(load.fz)] } : { face: f, pressure: Number(load.p) }))])}>
          Add load on {faces.length || ''} selected face(s)</button>
        {!faces.length && <p className="muted small">Select faces in the 3D view (Face mode) to add supports or loads.</p>}
        <div className="grid3">
          <label>mesh size (mm)<input value={meshSize} placeholder="auto" onChange={(e) => setMeshSize(e.target.value)} /></label>
          <label className="row small"><input type="checkbox" checked={conv} onChange={(e) => setConv(e.target.checked)} /> convergence check</label>
        </div>
        <button className="primary wide" disabled={!!busy || !setup.supports.length || !setup.loads.length || !m?.E} onClick={runFea}>
          {busy === 'fea' ? 'Solving…' : 'Run analysis'}</button>
        {error && <p className="err small">{error}</p>}
        {fea && (
          <>
            <dl className="stats">
              <dt>Max von Mises</dt><dd>{fmt(fea.max_von_mises_mpa, 2)} MPa</dd>
              <dt>Max deflection</dt><dd>{fmt(fea.max_displacement_mm, 4)} mm</dd>
              {fea.safety_factor !== undefined && <><dt>Safety factor vs yield</dt><dd className={fea.safety_factor < 1.5 ? 'err' : ''}>{fmt(fea.safety_factor, 2)}</dd></>}
              <dt>Force balance error</dt><dd>{fea.equilibrium_error.toExponential(1)}</dd>
              <dt>Mesh</dt><dd>{fea.elements} tet10, h={fmt(fea.mesh_size_mm, 2)} mm, {fea.solve_s}s</dd>
              {fea.convergence && <><dt>Refined mesh change</dt><dd>δ {fmt(fea.convergence.max_displacement_change_pct, 2)}%, σ {fmt(fea.convergence.max_von_mises_change_pct, 2)}%</dd></>}
            </dl>
            <div className="seg row">
              {(['stress', 'deformed', 'off'] as const).map((v) => (
                <button key={v} className={view === v ? 'on' : ''} onClick={() => { setView(v); show(fea, v) }}>{v === 'off' ? 'hide' : v}</button>
              ))}
            </div>
            <div className="legend">
              <span>{fmt(Math.min(...fea.display.von_mises), 1)}</span>
              <div className="bar" style={{ background: `linear-gradient(90deg, ${[0, 0.25, 0.5, 0.75, 1].map((t) => `rgb(${turbo(t).map((c) => Math.round(c * 255)).join(',')})`).join(',')})` }} />
              <span>{fmt(vmMax, 1)} MPa</span>
            </div>
            <ul className="muted small">{fea.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
            {fea.convergence && fea.convergence.max_von_mises_change_pct > 10 && <p className="warn small">Peak stress changed by more than 10% on refinement: likely a stress singularity (sharp corner or support edge). Judge stresses away from it, or add a fillet.</p>}
          </>
        )}
      </section>
    </>
  )
}
