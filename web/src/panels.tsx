// SPDX-License-Identifier: AGPL-3.0-or-later
import { Fragment, useEffect, useState } from 'react'
import { Dot, Eye, EyeOff, Trash2 } from 'lucide-react'
import { FEATURE_ICONS } from './ui'
import { api, fmt, type Feature, type Json, type Model, type SelRef } from './api'

export type Run = (command: string, args: Record<string, unknown>) => Promise<boolean>

/** Parse a field: plain numbers become numbers; formulas ("=a+1"), units ("2 in") and words stay strings. */
export function parseValue(text: string): Json {
  const t = text.trim()
  if (t === 'true' || t === 'false') return t === 'true'
  if (/^-?(\d+\.?\d*|\.\d+)(e-?\d+)?$/i.test(t)) return Number(t)
  return t
}

const show = (v: Json) => (typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v))

/* ---------------- editable value ---------------- */

export function ValueField({ value, onCommit, evaluated, placeholder }: {
  value: Json; onCommit: (v: Json) => Promise<boolean> | void; evaluated?: number; placeholder?: string
}) {
  const [draft, setDraft] = useState(show(value))
  useEffect(() => setDraft(show(value)), [value])
  const commit = async () => {
    if (draft === show(value)) return
    const ok = await onCommit(parseValue(draft))
    if (ok === false) setDraft(show(value))
  }
  const isFormula = typeof value === 'string' && value.startsWith('=')
  return (
    <span className="value-field">
      <input value={draft} placeholder={placeholder} className={isFormula ? 'formula' : ''}
        onChange={(e) => setDraft(e.target.value)} onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
          if (e.key === 'Escape') setDraft(show(value))
        }} />
      {evaluated !== undefined && isFormula && <span className="evaluated">= {fmt(evaluated, 3)}</span>}
    </span>
  )
}

/* ---------------- feature tree ---------------- */


export function FeatureTree({ model, selected, onSelect, run }: {
  model: Model; selected: string | null; onSelect: (id: string | null) => void; run: Run
}) {
  const deps = model.dependencies
  return (
    <section>
      <h2>Feature tree</h2>
      {model.spec.features.length === 0 && <p className="muted">Empty. Create a part or add a feature.</p>}
      <ul className="tree">
        {model.spec.features.map((f) => {
          const d = deps[f.id]
          const usedBy = Object.entries(deps).filter(([, v]) => v.features.includes(f.id)).map(([k]) => k)
          return (
            <li key={f.id} className={`${selected === f.id ? 'selected' : ''} ${f.suppressed ? 'suppressed' : ''}`}
              onClick={() => onSelect(f.id)}
              title={[
                d?.features.length ? `uses: ${d.features.join(', ')}` : '',
                d?.variables.length ? `variables: ${d.variables.join(', ')}` : '',
                d?.geometry ? 'depends on earlier geometry' : '',
                usedBy.length ? `used by: ${usedBy.join(', ')}` : '',
              ].filter(Boolean).join('\n')}>
              <span className="icon">{(() => { const I = FEATURE_ICONS[f.type] ?? Dot; return <I size={15} strokeWidth={1.7} /> })()}</span>
              <span className="name">{f.id}</span>
              <span className="type">{f.type}</span>
              <span className="actions" onClick={(e) => e.stopPropagation()}>
                <button className="link" title={f.suppressed ? 'Unsuppress' : 'Suppress'}
                  aria-label={f.suppressed ? 'Unsuppress' : 'Suppress'}
                  onClick={() => run('suppress_feature', { id: f.id, suppressed: !f.suppressed })}>{f.suppressed ? <EyeOff size={13} /> : <Eye size={13} />}</button>
                <button className="link danger" title="Delete" aria-label="Delete" onClick={() => run('remove_feature', { id: f.id })}><Trash2 size={13} /></button>
              </span>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

/* ---------------- variables ---------------- */

export function Variables({ model, run }: { model: Model; run: Run }) {
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const vars = model.spec.variables
  return (
    <section>
      <h2>Variables</h2>
      <table className="vars">
        <tbody>
          {Object.entries(vars).map(([k, v]) => (
            <tr key={k}>
              <td className="vname" title={model.spec.limits[k] ? `limits: ${JSON.stringify(model.spec.limits[k])}` : ''}>{k}</td>
              <td><ValueField value={v} evaluated={model.evaluated[k]} onCommit={(nv) => run('set_parameter', { name: `vars.${k}`, value: nv })} /></td>
              <td><button className="link danger" title="Remove" onClick={() => run('remove_variable', { name: k })}>✕</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row add-var">
        <input placeholder="name" value={name} onChange={(e) => setName(e.target.value)} />
        <input placeholder="value or =formula" value={value} onChange={(e) => setValue(e.target.value)} />
        <button disabled={!name || !value} onClick={() => run('set_variable', { name, value: parseValue(value) })
          .then((ok) => { if (ok) { setName(''); setValue('') } })}>Add</button>
      </div>
      <p className="muted small">Values accept formulas (<code>=width/2 - 5</code>) and units (<code>2 in</code>, <code>3 cm</code>).</p>
    </section>
  )
}

/* ---------------- property editor (any feature) ---------------- */

function ParamRows({ path, value, model, run }: { path: string; value: Json; model: Model; run: Run }) {
  if (Array.isArray(value)) {
    const scalarPair = value.every((x) => typeof x !== 'object' || x === null)
    if (scalarPair) {
      return (
        <div className="param-row">
          <span className="pkey">{path.split('.').slice(1).join('.')}</span>
          <span className="vec">
            {value.map((x, i) => (
              <ValueField key={i} value={x} onCommit={(nv) => run('set_parameter', { name: `${path}[${i}]`, value: nv })} />
            ))}
          </span>
        </div>
      )
    }
    return <>{value.map((x, i) => <ParamRows key={i} path={`${path}[${i}]`} value={x} model={model} run={run} />)}</>
  }
  if (value !== null && typeof value === 'object') {
    return (
      <div className="param-group">
        <div className="pgroup-title">{path.split('.').slice(1).join('.') || 'params'}</div>
        {Object.entries(value).map(([k, v]) => <ParamRows key={k} path={`${path}.${k}`} value={v} model={model} run={run} />)}
      </div>
    )
  }
  return (
    <div className="param-row">
      <span className="pkey">{path.split('.').slice(1).join('.')}</span>
      <ValueField value={value} onCommit={(nv) => run('set_parameter', { name: path, value: nv })} />
    </div>
  )
}

export function FeatureProperties({ feature, model, run }: { feature: Feature; model: Model; run: Run }) {
  const d = model.dependencies[feature.id]
  return (
    <section>
      <h2>{feature.id} <span className="muted">({feature.type})</span></h2>
      {feature.suppressed && <p className="warn">Suppressed: not part of the solid.</p>}
      {d && (d.features.length > 0 || d.geometry) && (
        <p className="muted small">
          {d.features.length > 0 && <>Uses {d.features.join(', ')}. </>}
          {d.geometry && <>Depends on earlier geometry (face/edge references).</>}
        </p>
      )}
      <div className="params">
        {Object.entries(feature.params).map(([k, v]) => (
          <ParamRows key={k} path={`${feature.id}.${k}`} value={v} model={model} run={run} />
        ))}
      </div>
    </section>
  )
}

/* ---------------- selection & measurement ---------------- */

export function SelectionPanel({ selection, model, onClear }: { selection: SelRef[]; model: Model; onClear: () => void }) {
  const [measure, setMeasure] = useState<Record<string, Json> | null>(null)
  useEffect(() => {
    setMeasure(null)
    if (selection.length === 0 || selection.length > 2 || !model.entities) return
    api.measure(selection).then(setMeasure).catch((e) => setMeasure({ ok: false, message: (e as Error).message }))
  }, [selection, model])
  if (selection.length === 0) return null
  return (
    <section>
      <h2>Selection <button className="link" onClick={onClear}>clear</button></h2>
      <ul className="sel-list">
        {selection.map((s, i) => (
          <li key={i}>
            <b>{s.kind}</b> {s.type.toLowerCase()} · feature <b>{s.feature ?? '—'}</b>
            {s.hole_instance && <> · hole {s.hole_instance.index + 1} of {s.hole_instance.count}</>}
          </li>
        ))}
      </ul>
      {measure && (
        <dl className="stats">
          {measure.ok === false && <><dt>Measure</dt><dd className="err">{String(measure.message)}</dd></>}
          {Object.entries(measure).filter(([k]) => k.endsWith('_mm') || k.endsWith('_mm2') || k.endsWith('_deg')).map(([k, v]) => (
            <Fragment key={k}><dt>{k.replace(/_(mm2|mm|deg)$/, '').replace(/_/g, ' ')}</dt>
              <dd>{fmt(v as number, 3)} {k.endsWith('_mm2') ? 'mm²' : k.endsWith('_deg') ? '°' : 'mm'}</dd></Fragment>
          ))}
        </dl>
      )}
      <p className="muted small">Shift-click adds to the selection. Two entities show distance and angle. Mention “this” in chat to refer to the selection.</p>
    </section>
  )
}

/* ---------------- design intent, material & mass ---------------- */

export function IntentPanel({ model }: { model: Model }) {
  const intent = model.spec.intent
  const notes = model.spec.source?.migration_notes as string[] | undefined
  if (!intent && !notes) return null
  return (
    <section>
      <h2>Design intent</h2>
      {intent && (
        <>
          {intent.requirements.length > 0 && <><h3>Required by you</h3><ul>{intent.requirements.map((r, i) => <li key={i}>{r}</li>)}</ul></>}
          {intent.assumptions.length > 0 && <><h3>Assumed by the assistant</h3><ul className="assumed">{intent.assumptions.map((r, i) => <li key={i}>{r}</li>)}</ul></>}
          {intent.open_questions?.length > 0 && <><h3>Open questions</h3><ul>{intent.open_questions.map((r, i) => <li key={i}>{r}</li>)}</ul></>}
        </>
      )}
      {notes && <><h3>Migration notes</h3><ul className="muted">{notes.map((n, i) => <li key={i}>{n}</li>)}</ul></>}
    </section>
  )
}

export function MaterialPanel({ model, materials, run }: { model: Model; materials: Record<string, number>; run: Run }) {
  const current = model.spec.material
  const [custom, setCustom] = useState({ name: '', density: '' })
  return (
    <section>
      <h2>Material & mass</h2>
      <select value={current && current.name in materials ? current.name : ''} onChange={(e) => e.target.value && run('assign_material', { name: e.target.value })}>
        <option value="">{current && !(current.name in materials) ? `${current.name} (custom)` : '— choose material —'}</option>
        {Object.entries(materials).map(([n, d]) => <option key={n} value={n}>{n} ({d} kg/m³)</option>)}
      </select>
      <div className="row">
        <input placeholder="custom name" value={custom.name} onChange={(e) => setCustom({ ...custom, name: e.target.value })} />
        <input placeholder="kg/m³" value={custom.density} onChange={(e) => setCustom({ ...custom, density: e.target.value })} />
        <button disabled={!custom.name || !Number(custom.density)} onClick={() => run('assign_material', { name: custom.name, density: Number(custom.density) })}>Set</button>
      </div>
      <dl className="stats">
        <dt>Volume</dt><dd>{fmt(model.volume_mm3, 1)} mm³</dd>
        <dt>Density</dt><dd>{current ? `${current.density} kg/m³` : '—'}</dd>
        <dt>Mass</dt><dd>{model.mass_kg !== null ? `${fmt(model.mass_kg * 1000, 2)} g` : 'assign a material'}</dd>
        <dt>Size</dt><dd>{model.bbox_mm ? model.bbox_mm.map((v) => fmt(v, 2)).join(' × ') + ' mm' : '—'}</dd>
      </dl>
      <p className="muted small">Mass = solid volume × the density you assigned. No strength or manufacturability analysis is performed.</p>
    </section>
  )
}
