// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from 'react'
import { fmt, type Model, type SelRef } from './api'
import { parseValue, type Run } from './panels'

/* Tolerances, datums, GD&T frames and surface finish, attached to the shared model. */

const CHARS: [string, string][] = [
  ['flatness', 'Flatness'], ['straightness', 'Straightness'], ['circularity', 'Circularity'], ['cylindricity', 'Cylindricity'],
  ['profile_line', 'Profile of a line'], ['profile_surface', 'Profile of a surface'], ['parallelism', 'Parallelism'],
  ['perpendicularity', 'Perpendicularity'], ['angularity', 'Angularity'], ['position', 'Position'], ['concentricity', 'Concentricity'],
  ['symmetry', 'Symmetry'], ['circular_runout', 'Circular runout'], ['total_runout', 'Total runout'],
]

function describe(a: Record<string, unknown>): string {
  switch (a.type) {
    case 'tolerance': return `${a.parameter} ${a.fit ?? `+${a.plus}/${a.minus}`}`
    case 'general_tolerance': return String(a.standard)
    case 'datum': return `datum ${a.label}`
    case 'gdt': return `${String(a.characteristic).replace('_', ' ')} ${a.diameter_zone ? 'Ø' : ''}${a.tolerance}${a.modifier ? ` (${a.modifier})` : ''}${(a.datums as string[])?.length ? ` | ${(a.datums as string[]).join('|')}` : ''} → ${(a.target as { feature?: string })?.feature ?? 'face'}`
    case 'surface_finish': return `Ra ${a.ra}${a.all ? ' (default)' : ''}`
    default: return String(a.type)
  }
}

export default function AnnotationsPanel({ model, selection, selectedFeature, run }: {
  model: Model; selection: SelRef[]; selectedFeature: string | null; run: Run
}) {
  const [kind, setKind] = useState('tolerance')
  const [f, setF] = useState<Record<string, string>>({ parameter: '', mode: 'pm', plus: '0.1', minus: '-0.1', fit: 'H7', standard: 'ISO 2768-m', label: 'A', characteristic: 'flatness', tolerance: '0.05', datums: '', modifier: '', diameter: '', ra: '1.6', target: 'face' })
  const set = (k: string) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value })
  const face = selection.find((s) => s.kind === 'face')
  const faceRef = face && { point: face.point, ...(face.normal ? { normal: face.normal } : {}) }
  const status = Object.fromEntries(model.annotations.map((s) => [s.id, s]))
  const params = Object.entries(model.parameters).filter(([k, v]) => typeof v !== 'object' && (k.startsWith('vars.') || (selectedFeature && k.startsWith(`${selectedFeature}.`))))
    .map(([k]) => k)
  const datums = model.spec.annotations.filter((a) => a.type === 'datum').map((a) => String(a.label))

  const add = async () => {
    const args: Record<string, unknown> = { type: kind }
    if (kind === 'tolerance') Object.assign(args, { parameter: f.parameter || params[0] }, f.mode === 'fit' ? { fit: f.fit } : { plus: Number(f.plus), minus: Number(f.minus) })
    if (kind === 'general_tolerance') args.standard = f.standard
    if (kind === 'datum') Object.assign(args, { label: f.label, face: faceRef })
    if (kind === 'gdt') Object.assign(args, {
      characteristic: f.characteristic, tolerance: parseValue(f.tolerance), diameter_zone: f.diameter === 'yes', modifier: f.modifier || undefined,
      datums: f.datums.split(/[\s,|]+/).filter(Boolean),
      target: f.target === 'feature' && selectedFeature ? { feature: selectedFeature } : { face: faceRef },
    })
    if (kind === 'surface_finish') Object.assign(args, f.target === 'all' ? { ra: Number(f.ra), all: true } : { ra: Number(f.ra), face: faceRef })
    await run('add_annotation', args)
  }
  const needsFace = (kind === 'datum' || (kind === 'gdt' && f.target === 'face') || (kind === 'surface_finish' && f.target !== 'all')) && !faceRef

  return (
    <section>
      <h2>Tolerances & GD&T</h2>
      <ul className="ann-list">
        {model.spec.annotations.map((a) => {
          const st = status[a.id]
          return (
            <li key={a.id} className={st && !st.ok ? 'bad' : ''}>
              <span>{describe(a)}
                {st?.ok && st.nominal !== undefined && <span className="muted"> [{fmt(st.min, 4)} … {fmt(st.max, 4)}]</span>}
                {st && !st.ok && <div className="err small">{st.message}</div>}
              </span>
              <button className="link danger" onClick={() => run('remove_annotation', { id: a.id })}>✕</button>
            </li>
          )
        })}
        {!model.spec.annotations.length && <li className="muted">None. Drawings will say tolerances are not specified.</li>}
      </ul>
      <div className="form">
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="tolerance">Dimension tolerance</option><option value="general_tolerance">General tolerance (ISO 2768)</option>
          <option value="datum">Datum (selected face)</option><option value="gdt">GD&T frame</option><option value="surface_finish">Surface finish</option>
        </select>
        {kind === 'tolerance' && (
          <div className="grid3">
            <label className="wide">parameter<select value={f.parameter || params[0]} onChange={set('parameter')}>{params.map((p) => <option key={p}>{p}</option>)}</select></label>
            <label>type<select value={f.mode} onChange={set('mode')}><option value="pm">± limits</option><option value="fit">ISO fit</option></select></label>
            {f.mode === 'fit' ? <label>class<input value={f.fit} onChange={set('fit')} placeholder="H7, g6…" /></label>
              : <><label>upper (mm)<input value={f.plus} onChange={set('plus')} /></label><label>lower (mm)<input value={f.minus} onChange={set('minus')} /></label></>}
          </div>
        )}
        {kind === 'general_tolerance' && (
          <select value={f.standard} onChange={set('standard')}>
            {['f', 'm', 'c', 'v'].flatMap((c) => ['', 'H', 'K', 'L'].map((g) => `ISO 2768-${c}${g}`)).map((s) => <option key={s}>{s}</option>)}
          </select>
        )}
        {kind === 'datum' && <label>label<input value={f.label} onChange={set('label')} maxLength={1} /></label>}
        {kind === 'gdt' && (
          <div className="grid3">
            <label className="wide">characteristic<select value={f.characteristic} onChange={set('characteristic')}>{CHARS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></label>
            <label>tolerance (mm)<input value={f.tolerance} onChange={set('tolerance')} /></label>
            <label>datums<input value={f.datums} onChange={set('datums')} placeholder={datums.join(' ') || 'none defined'} /></label>
            <label>Ø zone<select value={f.diameter} onChange={set('diameter')}><option value="">no</option><option value="yes">yes</option></select></label>
            <label>modifier<select value={f.modifier} onChange={set('modifier')}><option value="">none</option><option value="M">MMC (M)</option><option value="L">LMC (L)</option></select></label>
            <label>applies to<select value={f.target} onChange={set('target')}><option value="face">selected face</option><option value="feature" disabled={!selectedFeature}>feature {selectedFeature ?? ''}</option></select></label>
          </div>
        )}
        {kind === 'surface_finish' && (
          <div className="grid3">
            <label>Ra (µm)<input value={f.ra} onChange={set('ra')} /></label>
            <label>applies to<select value={f.target} onChange={set('target')}><option value="face">selected face</option><option value="all">all (default)</option></select></label>
          </div>
        )}
        {needsFace && <p className="warn small">Select a face in the 3D view first (Face mode).</p>}
        <button className="primary" disabled={needsFace || (kind === 'tolerance' && !params.length)} onClick={add}>Add</button>
      </div>
    </section>
  )
}
