// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useRef, useState } from 'react'
import { desktop, fmt, type Json, type Model, type SelRef } from './api'
import ErrorBoundary from './ErrorBoundary'
import Viewport, { type StdView, type ViewportHandle } from './Viewport'

/* Assembly workspace: components referencing part files, mates, interference, BOM, STEP export. */

interface Comp { id: string; file: string; name?: string; fixed: boolean; position: number[]; rotation: number[]; material?: string | null; mass_kg?: number | null; error?: string }
interface Mate { id: string; type: string; a: { component: string }; b: { component: string }; value: number; aligned: boolean }
interface AsmModel {
  spec: { name: string; components: Comp[]; mates: Mate[] }
  path: string | null
  status: { converged: boolean; dof: number; conflicts: string[] }
  can_undo: boolean; can_redo: boolean
  components: Comp[]
  entities: Model['entities']
  bom: { item: number; part: string; file: string; qty: number; material: string | null; mass_each_kg: number | null; mass_total_kg: number | null }[]
}
type Result = { ok: boolean; message: string }

async function call<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, body === undefined ? undefined : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const d = await r.json()
  if (!r.ok) throw new Error(d.detail ?? `failed (${r.status})`)
  return d
}

const ASM = [{ name: 'AI CAD assembly', extensions: ['aicadasm'] }]
const PARTS = [{ name: 'Parts', extensions: ['aicad', 'json', 'step', 'stp'] }]

export default function AssemblyWorkspace({ modeSwitch }: { modeSwitch: React.ReactNode }) {
  const [asm, setAsm] = useState<AsmModel | null>(null)
  const [status, setStatus] = useState<{ ok: boolean; text: string } | null>(null)
  const [selection, setSelection] = useState<SelRef[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [mateValue, setMateValue] = useState('0')
  const [aligned, setAligned] = useState(false)
  const [partPath, setPartPath] = useState('')
  const [clashes, setClashes] = useState<{ a: string; b: string; volume_mm3: number; center: number[] }[] | null>(null)
  const [markers, setMarkers] = useState<number[][]>([])
  const viewRef = useRef<ViewportHandle>(null)

  const refresh = useCallback((m: AsmModel) => { setAsm(m); setClashes(null); setMarkers([]) }, [])
  useEffect(() => { call<AsmModel>('/api/asm').then(refresh).catch((e) => setStatus({ ok: false, text: e.message })) }, [refresh])

  const cmd = async (command: string, args: Record<string, unknown>) => {
    try {
      const r = await call<{ result: Result; model: AsmModel }>('/api/asm/command', { command, args })
      refresh(r.model)
      setStatus({ ok: r.result.ok, text: r.result.message })
      return r.result.ok
    } catch (e) { setStatus({ ok: false, text: (e as Error).message }); return false }
  }
  const simple = async (url: string, body?: unknown) => {
    try {
      const r = await call<{ result?: Result; model: AsmModel; path?: string }>(url, body ?? {})
      refresh(r.model)
      if (r.result) setStatus({ ok: r.result.ok, text: r.result.message })
      if (r.path) setStatus({ ok: true, text: `saved ${r.path}` })
    } catch (e) { setStatus({ ok: false, text: (e as Error).message }) }
  }

  const insert = async () => {
    const path = desktop ? await desktop.openFileDialog('Insert part', PARTS) : partPath.trim()
    if (path) cmd('add_component', { file: path })
  }
  const open = async () => {
    const path = desktop ? await desktop.openFileDialog('Open assembly', ASM) : window.prompt('Assembly file path (.aicadasm)')
    if (path) simple('/api/asm/open', { path })
  }
  const save = async () => {
    let path = asm?.path ?? null
    if (!path) path = desktop ? await desktop.saveFileDialog('assembly.aicadasm', ASM) : window.prompt('Save assembly as (path)')
    if (path) simple('/api/asm/save', { path })
  }
  const exportStep = async () => {
    if (desktop) {
      const path = await desktop.saveExportDialog('assembly.step', 'step')
      if (path) { const r = await call<Result>('/api/asm/export', { path }); setStatus({ ok: r.ok, text: r.message }) }
    } else Object.assign(document.createElement('a'), { href: '/api/asm/export.step', download: 'assembly.step' }).click()
  }
  const interference = async () => {
    const r = await call<{ items: NonNullable<typeof clashes> }>('/api/asm/interference', {})
    setClashes(r.items)
    setMarkers(r.items.map((x) => x.center))
  }

  const faces = selection.filter((s) => s.kind === 'face' && s.component)
  const pair = faces.length === 2 && faces[0].component !== faces[1].component
  const side = (s: SelRef) => ({ component: s.component, face: { point: s.local_point, ...(s.local_normal ? { normal: s.local_normal } : {}) } })
  const mate = (type: string) => cmd('add_mate', { type, a: side(faces[0]), b: side(faces[1]), value: Number(mateValue), aligned }).then((ok) => ok && setSelection([]))
  const planar = pair && faces.every((f) => f.type === 'PLANE')
  const cyl = pair && faces.every((f) => f.type === 'CYLINDER')
  const comp = asm?.components.find((c) => c.id === selected)
  const pseudo = asm ? ({ entities: asm.entities } as unknown as Model) : null
  const views: StdView[] = ['iso', 'front', 'top', 'right']

  return (
    <div className="app wide-right">
      <header className="toolbar">
        <span className="brand">AI CAD</span>
        {modeSwitch}
        <div className="group">
          <button onClick={() => simple('/api/asm/new')}>New</button>
          <button onClick={open}>Open</button>
          <button onClick={save} disabled={!asm}>Save</button>
        </div>
        <div className="group">
          <button onClick={() => simple('/api/asm/undo')} disabled={!asm?.can_undo}>↶ Undo</button>
          <button onClick={() => simple('/api/asm/redo')} disabled={!asm?.can_redo}>↷ Redo</button>
        </div>
        <div className="group">
          {!desktop && <input placeholder="/path/to/part.aicad" value={partPath} onChange={(e) => setPartPath(e.target.value)} />}
          <button className="primary" onClick={insert}>＋ Insert part</button>
        </div>
        <div className="group seg">
          {views.map((v) => <button key={v} onClick={() => viewRef.current?.view(v)}>{v}</button>)}
          <button onClick={() => viewRef.current?.fit()}>fit</button>
        </div>
        <div className="group">
          <button onClick={interference} disabled={!asm?.components.length}>Interference</button>
          <button onClick={exportStep} disabled={!asm?.components.length}>Export STEP</button>
        </div>
        {status && <span className={`status ${status.ok ? 'ok' : 'err'}`} title={status.text}>{status.text}</span>}
      </header>

      <aside className="left">
        <section>
          <h2>Components</h2>
          {!asm?.components.length && <p className="muted">Insert saved parts (.aicad or STEP). Save each part first.</p>}
          <ul className="tree">
            {asm?.components.map((c) => (
              <li key={c.id} className={selected === c.id ? 'selected' : ''} onClick={() => setSelected(c.id)} title={c.file}>
                <span className="icon">{c.fixed ? '⚓' : '◇'}</span>
                <span className="name">{c.id}</span>
                <span className="type">{c.error ? <span className="err">missing</span> : c.name}</span>
                <span className="actions" onClick={(e) => e.stopPropagation()}>
                  <button className="link" title={c.fixed ? 'Make free' : 'Fix in place'} onClick={() => cmd('set_fixed', { id: c.id, fixed: !c.fixed })}>{c.fixed ? 'free' : 'fix'}</button>
                  <button className="link danger" onClick={() => cmd('remove_component', { id: c.id })}>✕</button>
                </span>
              </li>
            ))}
          </ul>
        </section>
        <section>
          <h2>Mates</h2>
          {asm && <p className={`small ${asm.status.converged ? 'ok' : 'err'}`}>
            {asm.status.converged ? (asm.status.dof === 0 ? 'fully constrained' : `${asm.status.dof} degree(s) of freedom left`) : `conflict: ${asm.status.conflicts.join(', ')}`}</p>}
          <ul className="findings">
            {asm?.spec.mates.map((m) => (
              <li key={m.id}><span>{m.type}{m.type === 'distance' ? ` ${m.value}` : ''}{m.aligned ? ' (aligned)' : ''}: {m.a.component} ↔ {m.b.component}</span>
                <button className="link danger" onClick={() => cmd('remove_mate', { id: m.id })}>✕</button></li>
            ))}
          </ul>
        </section>
      </aside>

      <main className="center">
        <ErrorBoundary name="3D view">
          <Viewport ref={viewRef} model={pseudo} mode="face" selection={selection} selectedFeature={selected} markers={markers}
            onPick={(r, add) => {
              if (!r) { if (!add) setSelection([]); return }
              setSelected(r.feature)
              setSelection((s) => add ? (s.some((x) => x.index === r.index) ? s.filter((x) => x.index !== r.index) : [...s, r]).slice(-2) : [r])
            }} />
        </ErrorBoundary>
        <div className="readout">Shift-click two faces on different components to mate them</div>
      </main>

      <aside className="right">
        <div className="tab-body scroll">
          <section>
            <h2>Mate selected faces</h2>
            <p className="small">{faces.length === 0 ? 'No faces selected.' : faces.map((f) => `${f.type.toLowerCase()} of ${f.component}`).join('  +  ')}</p>
            <div className="sk-buttons">
              <button disabled={!planar} onClick={() => mate('coincident')}>Coincident</button>
              <button disabled={!planar} onClick={() => mate('parallel')}>Parallel</button>
              <button disabled={!planar} onClick={() => mate('distance')}>Distance</button>
              <button disabled={!cyl} onClick={() => mate('concentric')}>Concentric</button>
            </div>
            <div className="grid3">
              <label>distance (mm)<input value={mateValue} onChange={(e) => setMateValue(e.target.value)} /></label>
              <label className="row small"><input type="checkbox" checked={aligned} onChange={(e) => setAligned(e.target.checked)} /> normals aligned</label>
            </div>
          </section>
          {comp && !comp.error && (
            <section>
              <h2>{comp.id} <span className="muted">{comp.name}</span></h2>
              <p className="muted small">{comp.file} · {comp.material ?? 'no material'}{comp.mass_kg != null ? ` · ${fmt(comp.mass_kg * 1000, 1)} g` : ''}</p>
              <TransformEditor comp={comp} onApply={(position, rotation) => cmd('set_transform', { id: comp.id, position, rotation })} />
            </section>
          )}
          {comp?.error && <section><p className="err small">{comp.error}</p></section>}
          {clashes && (
            <section>
              <h2>Interference</h2>
              {clashes.length === 0 ? <p className="ok small">No overlapping volume between components (touching is allowed).</p> : (
                <ul className="findings">{clashes.map((c, i) => <li key={i} className="err" onClick={() => setMarkers([c.center])}>{c.a} ∩ {c.b}: {fmt(c.volume_mm3, 1)} mm³</li>)}</ul>
              )}
            </section>
          )}
          <section>
            <h2>Bill of materials</h2>
            <table className="dims">
              <thead><tr><th>#</th><th>part</th><th>qty</th><th>material</th><th>mass</th></tr></thead>
              <tbody>
                {asm?.bom.map((r) => <tr key={r.item}><td>{r.item}</td><td>{r.part}</td><td>{r.qty}</td><td>{r.material ?? '—'}</td>
                  <td>{r.mass_total_kg != null ? `${fmt(r.mass_total_kg * 1000, 1)} g` : '—'}</td></tr>)}
              </tbody>
            </table>
            {asm && asm.bom.length > 0 && <p className="small">Total: {fmt(asm.bom.reduce((s, r) => s + (r.mass_total_kg ?? 0), 0) * 1000, 1)} g
              {asm.bom.some((r) => r.mass_total_kg == null) ? ' (parts without material excluded)' : ''}</p>}
          </section>
        </div>
      </aside>
    </div>
  )
}

function TransformEditor({ comp, onApply }: { comp: Comp; onApply: (p: number[], r: number[]) => void }) {
  const [p, setP] = useState(comp.position.map((v) => String(Number(v.toFixed(4)))))
  const [r, setR] = useState(comp.rotation.map((v) => String(Number(v.toFixed(4)))))
  useEffect(() => { setP(comp.position.map((v) => String(Number(v.toFixed(4))))); setR(comp.rotation.map((v) => String(Number(v.toFixed(4))))) }, [comp])
  const cell = (arr: string[], set: (a: string[]) => void, i: number, label: string) => (
    <label key={label}>{label}<input value={arr[i]} onChange={(e) => set(arr.map((x, j) => (j === i ? e.target.value : x)))} /></label>
  )
  return (
    <>
      <div className="grid3">{['x', 'y', 'z'].map((l, i) => cell(p, setP, i, `position ${l}`))}</div>
      <div className="grid3">{['x', 'y', 'z'].map((l, i) => cell(r, setR, i, `rotation ${l}°`))}</div>
      <p className="muted small">Rotation is a rotation vector (axis × angle in degrees). Mates override free components.</p>
      <button onClick={() => onApply(p.map(Number), r.map(Number) as number[])}>Apply</button>
    </>
  )
}

export type { Json }
