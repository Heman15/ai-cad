// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from 'react'
import { desktop, fmt, type DrawingDimension, type Json, type Model } from './api'
import { ValueField, type Run } from './panels'

/* Source drawing with traceable dimensions, and the generated 2D drawing. */

const STATUS_COLOR: Record<string, string> = { read: '#2f8f4e', confirmed: '#2f6fd6', inferred: '#c58a10', unclear: '#c23b3b' }

function modelValue(model: Model, parameter?: string): number | string | null {
  if (!parameter) return null
  if (parameter.startsWith('vars.')) return model.evaluated[parameter.slice(5)] ?? null
  const v = model.parameters[parameter]
  return typeof v === 'number' || typeof v === 'string' ? v : null
}

export function SourceDrawingPanel({ model, run, onInterpret, onImport, aiReady }: {
  model: Model; run: Run; onInterpret: () => void; onImport: () => void; aiReady: boolean
}) {
  const drawing = model.spec.source?.drawing as { filename: string; pages: number; sha256: string } | undefined
  const analysis = model.spec.source?.analysis as { dimensions: DrawingDimension[]; questions?: string[]; units?: string; projection?: string; notes?: string[] } | undefined
  const [page, setPage] = useState(1)
  const [focus, setFocus] = useState<string | null>(null)
  const [imgError, setImgError] = useState(false)
  useEffect(() => { setPage(1); setImgError(false) }, [drawing?.sha256])

  if (!drawing) {
    return (
      <section>
        <h2>Source drawing</h2>
        <p className="muted">Import a PDF or image of a dimensioned drawing. The assistant reads every dimension, shows where it found it,
          asks about anything missing, and builds a parametric model linked to those dimensions.</p>
        <button className="primary" onClick={onImport}>Import drawing…</button>
      </section>
    )
  }
  const dims = analysis?.dimensions ?? []
  return (
    <>
      <section>
        <h2>
          {drawing.filename}
          <span className="row">
            {drawing.pages > 1 && <select value={page} onChange={(e) => setPage(+e.target.value)}>
              {Array.from({ length: drawing.pages }, (_, i) => <option key={i} value={i + 1}>page {i + 1}</option>)}
            </select>}
            <button className="link" onClick={onImport}>replace</button>
          </span>
        </h2>
        <div className="src-page">
          {imgError ? <p className="err small">The drawing file is not in the local cache any more; import it again to see it.</p> : (
            <img src={`/api/source-drawing/page/${page}?v=${drawing.sha256}`} onError={() => setImgError(true)} alt="source drawing" />
          )}
          {!imgError && dims.filter((d) => (d.page ?? 1) === page && d.bbox).map((d) => {
            const [x0, y0, x1, y1] = d.bbox!
            return (
              <div key={d.id} className={`dim-box ${focus === d.id ? 'focus' : ''}`} title={`${d.id}: ${d.value} — ${d.describes}`}
                onClick={() => setFocus(d.id)}
                style={{ left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`, borderColor: STATUS_COLOR[d.confirmed ? 'confirmed' : d.status] }}>
                <span style={{ background: STATUS_COLOR[d.confirmed ? 'confirmed' : d.status] }}>{d.id}</span>
              </div>
            )
          })}
        </div>
        <div className="row">
          <button className="primary" disabled={!aiReady} onClick={onInterpret}>{analysis ? 'Re-interpret with AI' : 'Interpret with AI'}</button>
          {!aiReady && <span className="muted small">needs an API key (⚙)</span>}
        </div>
      </section>
      {analysis && (
        <section>
          <h2>Dimensions ({dims.length}) <span className="muted">{analysis.units} · {analysis.projection} angle</span></h2>
          <table className="dims">
            <thead><tr><th>id</th><th>drawing</th><th>meaning</th><th>model</th></tr></thead>
            <tbody>
              {dims.map((d) => {
                const mv = modelValue(model, d.parameter)
                const mismatch = typeof mv === 'number' && typeof d.value === 'number' && Math.abs(mv - d.value) > 1e-6
                return (
                  <tr key={d.id} className={focus === d.id ? 'focus' : ''} onClick={() => setFocus(d.id)}>
                    <td><span className="dot" style={{ background: STATUS_COLOR[d.confirmed ? 'confirmed' : d.status] }} />{d.id}</td>
                    <td><ValueField value={d.value as Json} onCommit={(v) => run('update_drawing_dimension', { dimension_id: d.id, value: v })} /></td>
                    <td className="small">{d.describes}{d.tolerance ? ` (${d.tolerance})` : ''}
                      <div className="muted">{d.confirmed ? 'confirmed by you' : `${d.status}, ${d.confidence} confidence`}</div></td>
                    <td className={`small ${mismatch ? 'err' : ''}`}>
                      {d.parameter ? <>{typeof mv === 'number' ? fmt(mv, 3) : String(mv ?? '—')}<div className="muted">{d.parameter}</div></> : <span className="muted">not linked</span>}
                      {mismatch && <div>≠ drawing</div>}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <p className="muted small">Edit a drawing value to correct a misread; the linked model parameter changes with it (one undo step).
            Colours: <span style={{ color: STATUS_COLOR.read }}>read</span>, <span style={{ color: STATUS_COLOR.inferred }}>inferred</span>,
            {' '}<span style={{ color: STATUS_COLOR.unclear }}>unclear</span>, <span style={{ color: STATUS_COLOR.confirmed }}>confirmed</span>.</p>
          {!!analysis.questions?.length && <><h3>Open questions</h3><ul>{analysis.questions.map((q, i) => <li key={i}>{q}</li>)}</ul></>}
          {!!analysis.notes?.length && <><h3>Drawing notes</h3><ul className="muted">{analysis.notes.map((q, i) => <li key={i}>{q}</li>)}</ul></>}
        </section>
      )}
    </>
  )
}

export function DrawingDialog({ model, onClose, setStatus }: { model: Model; onClose: () => void; setStatus: (s: { ok: boolean; text: string }) => void }) {
  const [sheet, setSheet] = useState('A3')
  const [projection, setProjection] = useState('first')
  const [compare, setCompare] = useState(false)
  const source = model.spec.source?.drawing as { sha256: string } | undefined
  const q = `sheet=${sheet}&projection=${projection}&v=${encodeURIComponent(String(model.volume_mm3))}`

  const save = async (ext: 'pdf' | 'svg') => {
    const name = `${model.spec.name || 'part'}_drawing.${ext}`
    if (desktop) {
      const path = await desktop.saveExportDialog(name, ext)
      if (!path) return
      const r = await fetch('/api/drawing/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path, sheet, projection }) })
      const d = await r.json()
      setStatus({ ok: r.ok, text: r.ok ? d.message : d.detail })
    } else {
      Object.assign(document.createElement('a'), { href: ext === 'pdf' ? `/api/drawing.pdf?${q}` : `/api/drawing.svg?${q}`, download: name }).click()
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal drawing-modal" onClick={(e) => e.stopPropagation()}>
        <h2>2D drawing
          <span className="row">
            <select value={sheet} onChange={(e) => setSheet(e.target.value)}><option>A4</option><option>A3</option><option>A2</option></select>
            <select value={projection} onChange={(e) => setProjection(e.target.value)}><option value="first">first angle (ISO)</option><option value="third">third angle (US)</option></select>
            {source && <label className="row small"><input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} /> compare with source</label>}
            <button onClick={() => save('pdf')}>Save PDF</button>
            <button onClick={() => save('svg')}>Save SVG</button>
            <button onClick={onClose}>Close</button>
          </span>
        </h2>
        <div className={`drawing-views ${compare ? 'two' : ''}`}>
          <img src={`/api/drawing.svg?${q}`} alt="generated drawing" />
          {compare && source && <img src={`/api/source-drawing/page/1?v=${source.sha256}`} alt="source drawing" />}
        </div>
        <p className="muted small">Views are exact hidden-line projections of the solid. Dimensions shown: overall size, hole positions and hole callouts.
          Tolerances, surface finish and other annotations are not generated; add them before manufacture.</p>
      </div>
    </div>
  )
}

export function FlatPatternDialog({ model, onClose, setStatus }: { model: Model; onClose: () => void; setStatus: (s: { ok: boolean; text: string }) => void }) {
  const info = model.sheet
  const save = async (ext: 'dxf' | 'svg') => {
    const name = `${model.spec.name || 'part'}_flat.${ext}`
    if (desktop) {
      const path = await desktop.saveExportDialog(name, ext)
      if (!path) return
      const r = await fetch('/api/flat/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }) })
      const d = await r.json()
      setStatus({ ok: r.ok, text: r.ok ? d.message : d.detail })
    } else {
      Object.assign(document.createElement('a'), { href: `/api/flat.${ext}`, download: name }).click()
    }
  }
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal drawing-modal" onClick={(e) => e.stopPropagation()}>
        <h2>Flat pattern
          <span className="row">
            <button disabled={!info?.flat_ok} onClick={() => save('dxf')}>Save DXF</button>
            <button disabled={!info?.flat_ok} onClick={() => save('svg')}>Save SVG</button>
            <button onClick={onClose}>Close</button>
          </span>
        </h2>
        {info?.flat_ok ? (
          <>
            <p className="small">{info.flat_size!.map((v) => v.toFixed(2)).join(' × ')} mm · {info.bends} bend(s). Bend lines (red, dashed) mark both ends of each bend allowance.</p>
            <div className="drawing-views"><img src={`/api/flat.svg?v=${encodeURIComponent(String(model.volume_mm3))}`} alt="flat pattern" /></div>
          </>
        ) : <p className="err">{info?.message ?? 'Not a sheet-metal part.'}</p>}
      </div>
    </div>
  )
}
