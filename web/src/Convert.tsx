// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from 'react'
import { FilePlus, Trash2, X } from 'lucide-react'
import { api, desktop, MODEL_FILTERS } from './api'

/** Convert any model file (meshes, STEP/IGES, AI CAD parts and assemblies) to STL. */
export default function ConvertDialog({ onClose, setStatus, partName, hasPart }: {
  onClose: () => void; setStatus: (s: { ok: boolean; text: string }) => void; partName: string; hasPart: boolean
}) {
  const [files, setFiles] = useState<string[]>([])
  const [units, setUnits] = useState('mm')
  const [quality, setQuality] = useState('normal')
  const [binary, setBinary] = useState(true)
  const [nextTo, setNextTo] = useState(true)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; message: string; failed?: { source: string; error: string }[] } | null>(null)

  const add = async () => {
    if (desktop) {
      const picked = desktop.openFilesDialog ? await desktop.openFilesDialog('Files to convert to STL', MODEL_FILTERS)
        : await desktop.openFileDialog('File to convert to STL', MODEL_FILTERS).then((p) => (p ? [p] : null))
      if (picked) setFiles((f) => [...new Set([...f, ...picked])])
    } else {
      const p = window.prompt('Path of the file to convert (on this computer)')
      if (p) setFiles((f) => [...new Set([...f, p.trim()])])
    }
  }

  const run = async () => {
    setBusy(true)
    setResult(null)
    try {
      let out: string | null
      if (files.length === 1 && !nextTo) {
        const stem = files[0].split('/').pop()!.replace(/\.[^.]+$/, '')
        out = desktop ? await desktop.saveExportDialog(`${stem}.stl`, 'stl') : window.prompt('Save STL as (path)', `${stem}.stl`)
      } else if (nextTo) {
        out = null
      } else {
        out = desktop?.chooseFolderDialog ? await desktop.chooseFolderDialog('Folder for the STL files') : window.prompt('Folder for the STL files')
      }
      if (!nextTo && !out) { setBusy(false); return }
      const opts = { units, quality, binary }
      let r: { ok: boolean; message: string; failed?: { source: string; error: string }[] }
      if (nextTo) {  // each file next to its source
        const done: string[] = [], failed: { source: string; error: string }[] = []
        for (const f of files) {
          try { done.push((await api.convertStl([f], f.replace(/\.[^./]+$/, '') + '.stl', opts)).message) }
          catch (e) { failed.push({ source: f, error: (e as Error).message }) }
        }
        r = { ok: !failed.length, message: done.length === 1 ? done[0] : `converted ${done.length} of ${files.length} files next to the originals`, failed }
      } else r = await api.convertStl(files, out!, opts)
      setResult(r)
      setStatus({ ok: r.ok, text: r.message })
    } catch (e) {
      setResult({ ok: false, message: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  const exportPart = async () => {
    const path = desktop ? await desktop.saveExportDialog(`${partName || 'part'}.stl`, 'stl') : null
    if (!desktop) { Object.assign(document.createElement('a'), { href: '/api/export/stl', download: '' }).click(); return }
    if (!path) return
    try { const r = await api.exportTo('stl', path); setStatus({ ok: r.ok, text: r.message }); onClose() }
    catch (e) { setResult({ ok: false, message: (e as Error).message }) }
  }

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label="Convert to STL">
        <h2>Convert to STL <button className="link" onClick={onClose} aria-label="Close"><X size={14} /></button></h2>
        <p className="muted small">Any STEP, IGES, BREP, OBJ, 3MF, PLY, STL file, AI CAD part (.aicad) or assembly (.aicadasm). Assemblies are written as one STL with every part in place.</p>
        {hasPart && <button className="wide" onClick={exportPart}>Export the open part ({partName || 'part'}) as STL…</button>}
        <div className="row"><button onClick={add}><FilePlus size={14} /> Add files…</button><span className="muted small">{files.length ? `${files.length} file${files.length > 1 ? 's' : ''}` : 'no files yet'}</span></div>
        <ul className="findings">
          {files.map((f) => <li key={f}><span title={f}>{f.split('/').pop()}</span><button className="link danger" onClick={() => setFiles(files.filter((x) => x !== f))} aria-label="Remove"><Trash2 size={13} /></button></li>)}
        </ul>
        <div className="grid3">
          <label>Mesh units<select value={units} onChange={(e) => setUnits(e.target.value)} title="Units of STL/OBJ/PLY/3MF inputs; CAD files carry their own">
            <option value="mm">millimetres</option><option value="cm">centimetres</option><option value="m">metres</option><option value="in">inches</option></select></label>
          <label>Quality<select value={quality} onChange={(e) => setQuality(e.target.value)} title="How closely triangles follow curved CAD surfaces">
            <option value="coarse">coarse (small file)</option><option value="normal">normal (0.02 mm)</option><option value="fine">fine (0.005 mm)</option></select></label>
          <label>Format<select value={binary ? 'binary' : 'ascii'} onChange={(e) => setBinary(e.target.value === 'binary')}>
            <option value="binary">binary</option><option value="ascii">ASCII</option></select></label>
        </div>
        <label className="check"><input type="checkbox" checked={nextTo} onChange={(e) => setNextTo(e.target.checked)} /> Save each STL next to its original</label>
        {result && (
          <div className={result.ok ? 'ok small' : 'err small'}>
            <p>{result.message}</p>
            {result.failed?.map((f) => <p key={f.source}>{f.source.split('/').pop()}: {f.error}</p>)}
          </div>
        )}
        <div className="row end">
          <button onClick={onClose}>Close</button>
          <button className="primary" disabled={!files.length || busy} onClick={run}>{busy ? 'Converting…' : 'Convert'}</button>
        </div>
      </div>
    </div>
  )
}
