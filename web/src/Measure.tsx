// SPDX-License-Identifier: AGPL-3.0-or-later
import { Fragment, useEffect, useState } from 'react'
import { Ruler, Trash2, X } from 'lucide-react'
import { api, fmt, type Json, type Model, type SelRef } from './api'
import type { MeasureGraphic } from './Viewport'

type Result = Record<string, Json> & { ok: boolean; message?: string }

const ROWS: [string, string, string][] = [
  ['min_distance_mm', 'Distance', 'mm'], ['center_distance_mm', 'Centre distance', 'mm'], ['parallel_distance_mm', 'Normal distance', 'mm'],
  ['angle_deg', 'Angle', '°'], ['dx_mm', 'dX', 'mm'], ['dy_mm', 'dY', 'mm'], ['dz_mm', 'dZ', 'mm'],
  ['diameter_mm', 'Diameter', 'mm'], ['length_mm', 'Length', 'mm'], ['area_mm2', 'Area', 'mm²'],
  ['x_mm', 'X', 'mm'], ['y_mm', 'Y', 'mm'], ['z_mm', 'Z', 'mm'],
]

const n = (v: Json) => fmt(v as number, 3)
const mid = (a: number[], b: number[]) => a.map((x, i) => (x + b[i]) / 2)

/** What to draw in the 3D view for a measurement result. */
function graphic(r: Result, sel: SelRef[]): MeasureGraphic | null {
  if (!r.ok) return null
  const pts = r.points as number[][] | undefined
  const centers = r.centers as number[][] | undefined
  if (pts) {
    if (centers && r.center_distance_mm !== undefined) {
      return { line: [centers[0], centers[1]], label: `${n(r.center_distance_mm)} mm (centres)`, anchor: mid(centers[0], centers[1]) }
    }
    const d = r.min_distance_mm as number
    const text = d > 1e-6 ? `${n(d)} mm` : r.angle_deg !== undefined ? `${n(r.angle_deg)}°` : '0 mm (touching)'
    const withAngle = d > 1e-6 && r.angle_deg !== undefined && (r.angle_deg as number) > 1e-6 ? ` · ${n(r.angle_deg)}°` : ''
    return { line: [pts[0], pts[1]], label: text + withAngle, anchor: mid(pts[0], pts[1]) }
  }
  const at = sel[0]?.point
  if (!at) return null
  const label = r.x_mm !== undefined ? `X ${n(r.x_mm)}  Y ${n(r.y_mm)}  Z ${n(r.z_mm)}`
    : r.diameter_mm !== undefined ? `Ø ${n(r.diameter_mm)} mm`
    : r.length_mm !== undefined ? `${n(r.length_mm)} mm`
    : r.area_mm2 !== undefined ? `${n(r.area_mm2)} mm²` : ''
  return label ? { label, anchor: at } : null
}

export default function MeasurePanel({ selection, model, onClear, onClose, onGraphic }: {
  selection: SelRef[]; model: Model; onClear: () => void; onClose: () => void; onGraphic: (g: MeasureGraphic | null) => void
}) {
  const [result, setResult] = useState<Result | null>(null)
  useEffect(() => {
    setResult(null)
    onGraphic(null)
    if (!selection.length || !model.entities) return
    let live = true
    api.measure(selection)
      .then((r) => { if (live) { setResult(r); onGraphic(graphic(r, selection)) } })
      .catch((e) => live && setResult({ ok: false, message: (e as Error).message }))
    return () => { live = false }
  }, [selection, model, onGraphic])

  return (
    <div className="measure-panel" role="dialog" aria-label="Measure">
      <div className="mp-head">
        <Ruler size={15} /> <b>Measure</b>
        <span className="spacer" />
        <button className="icon-btn" title="Clear picks (Esc)" aria-label="Clear measurement" onClick={onClear} disabled={!selection.length}><Trash2 size={14} /></button>
        <button className="icon-btn" title="Close measure" aria-label="Close measure" onClick={onClose}><X size={14} /></button>
      </div>
      {!selection.length && <p className="muted small">Click a point, edge or face. Click a second one for distance, dX/dY/dZ and angle.</p>}
      {selection.length > 0 && (
        <ol className="mp-picks">
          {selection.map((s, i) => (
            <li key={i}><span className={`pick-dot k${i}`} />{s.kind === 'vertex' ? 'Point' : s.kind === 'edge' ? 'Edge' : 'Face'}
              <span className="muted"> · {s.kind === 'vertex' ? s.point.map((v) => fmt(v, 2)).join(', ') : s.type.toLowerCase()}</span></li>
          ))}
        </ol>
      )}
      {result && !result.ok && <p className="err small">{result.message}</p>}
      {result?.ok && (
        <dl className="mp-results">
          {ROWS.filter(([k]) => result[k] !== undefined).map(([k, label, unit]) => (
            <Fragment key={k}><dt>{label}</dt><dd>{n(result[k])} <span className="muted">{unit}</span></dd></Fragment>
          ))}
        </dl>
      )}
      {selection.length === 2 && <p className="muted small">A third click starts a new measurement.</p>}
    </div>
  )
}
