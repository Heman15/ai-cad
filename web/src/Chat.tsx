// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useRef, useState } from 'react'
import { api, type Alternative, type ChatEvent, type Diff, type Json, type Model, type SelRef } from './api'

interface Step { tool: string; ok: boolean; message: string; input: Record<string, Json> }
interface Msg {
  role: 'user' | 'assistant' | 'error'
  text: string
  steps?: Step[]
  diff?: Diff
  alternatives?: Alternative[]
  selection?: string
}

const show = (v: Json) => (typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v))

function DiffView({ diff }: { diff: Diff }) {
  const empty = !diff.changed.length && !diff.added.length && !diff.removed.length && !diff.material && !diff.suppression_changed?.length
  if (empty) return null
  return (
    <div className="diff">
      <b>Changes</b>
      <ul>
        {diff.added.map((a) => <li key={'a' + a} className="ok">+ added {a}</li>)}
        {diff.removed.map((a) => <li key={'r' + a} className="err">− removed {a}</li>)}
        {diff.changed.map((c) => <li key={c.name}>{c.name}: {show(c.old)} → <b>{show(c.new)}</b></li>)}
        {diff.suppression_changed?.map((a) => <li key={'s' + a}>suppression toggled: {a}</li>)}
        {diff.material && <li>material: {diff.material.old ?? '—'} → <b>{diff.material.new}</b></li>}
      </ul>
    </div>
  )
}

export interface ChatTrigger { nonce: number; text: string; interpret: boolean }

export default function Chat({ selection, onModel, aiReady, openSettings, trigger }: {
  selection: SelRef[]; onModel: (m: Model) => void; aiReady: boolean; openSettings: () => void; trigger?: ChatTrigger | null
}) {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [files, setFiles] = useState<{ id: string; name: string }[]>([])
  const [dragging, setDragging] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const attach = async (list: FileList | File[]) => {
    const types: Record<string, string> = { pdf: 'application/pdf', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp', gif: 'image/gif' }
    for (const f of Array.from(list)) {
      const media = f.type || types[f.name.split('.').pop()?.toLowerCase() ?? ''] || ''
      const buf = new Uint8Array(await f.arrayBuffer())
      let bin = ''
      for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000))
      try {
        const r = await api.attach(f.name, media, btoa(bin))
        setFiles((x) => [...x.filter((y) => y.id !== r.id), { id: r.id, name: r.name }])
      } catch (e) {
        setMsgs((m) => [...m, { role: 'error', text: `${f.name}: ${(e as Error).message}` }])
      }
    }
  }
  const endRef = useRef<HTMLDivElement>(null)
  useEffect(() => endRef.current?.scrollIntoView({ behavior: 'smooth' }), [msgs, busy])

  const patchLast = (fn: (m: Msg) => Msg) => setMsgs((ms) => [...ms.slice(0, -1), fn(ms[ms.length - 1])])

  useEffect(() => { if (trigger) send(trigger.text, trigger.interpret) }, [trigger?.nonce]) // eslint-disable-line react-hooks/exhaustive-deps

  const send = async (override?: string, interpret = false) => {
    const t = (override ?? text).trim() || (interpret ? 'Interpret the attached drawing.' : files.length ? 'Build a CAD model of the attached file.' : '')
    if (!t || busy) return
    if (override === undefined) setText('')
    const selLabel = selection.length ? selection.map((s) => `${s.kind} of ${s.feature ?? '?'}`).join(', ') : undefined
    const sent = files
    setFiles([])
    setMsgs((m) => [...m, { role: 'user', text: t + (sent.length ? `\n📎 ${sent.map((f) => f.name).join(', ')}` : ''), selection: selLabel },
      { role: 'assistant', text: '', steps: [] }])
    setBusy(true)
    try {
      await api.chat(interpret ? '' : t, selection, (e: ChatEvent) => {
        if (e.type === 'tool') {
          if (e.model) onModel(e.model)
          patchLast((m) => ({ ...m, steps: [...(m.steps ?? []), { tool: e.tool, ok: e.ok, message: e.message, input: e.input }] }))
        } else if (e.type === 'text') {
          patchLast((m) => ({ ...m, text: m.text ? `${m.text}\n${e.text}` : e.text }))
        } else if (e.type === 'alternatives') {
          patchLast((m) => ({ ...m, alternatives: e.items }))
        } else if (e.type === 'done') {
          onModel(e.model)
          patchLast((m) => ({ ...m, text: m.text ? `${m.text}\n${e.reply}` : e.reply, diff: e.diff }))
        } else if (e.type === 'error') {
          patchLast(() => ({ role: 'error', text: e.message }))
        }
      }, interpret, sent.map((f) => f.id))
    } catch (err) {
      patchLast(() => ({ role: 'error', text: (err as Error).message }))
    } finally {
      setBusy(false)
    }
  }

  const accept = async (alt: Alternative) => {
    const r = await api.acceptAlternative(alt.id)
    onModel(r.model)
    setMsgs((m) => [...m, r.result.ok
      ? { role: 'assistant', text: `Applied “${alt.title}”. Undo reverts it in one step.` }
      : { role: 'error', text: r.result.message }])
  }

  return (
    <div className="chat">
      <h2>
        Assistant
        <button className="link" onClick={() => { api.resetChat(); setMsgs([]) }}>new conversation</button>
      </h2>
      <div className="messages">
        {!aiReady && (
          <div className="msg error">Choose an AI provider first — e.g. “Claude Code (your login)”, no key needed. <button className="link" onClick={openSettings}>Open settings (⚙)</button></div>
        )}
        {msgs.length === 0 && aiReady && (
          <p className="muted">
            Describe a part, e.g. “a 120 × 80 mm aluminium mounting plate with four holes”. The assistant asks for missing
            dimensions, builds an editable model, and edits it with you. Select a face or hole and say “make this 10 mm”.
          </p>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            {m.selection && <div className="chip">about: {m.selection}</div>}
            {m.steps && m.steps.length > 0 && (
              <ul className="steps">
                {m.steps.map((s, j) => (
                  <li key={j} className={s.ok ? 'ok' : 'err'} title={JSON.stringify(s.input, null, 1)}>
                    {s.ok ? '✓' : '✗'} <code>{s.tool}</code> {s.message}
                  </li>
                ))}
              </ul>
            )}
            {m.text && <div className="text">{m.text}</div>}
            {m.alternatives && (
              <div className="alts">
                {m.alternatives.map((a) => (
                  <div key={a.id} className={`alt ${a.preview.ok ? '' : 'invalid'}`}>
                    <b>{a.title}</b>
                    <p>{a.description}</p>
                    {a.preview.ok ? (
                      <>
                        <p className="muted small">
                          {a.preview.bbox_mm?.map((x) => x.toFixed(1)).join(' × ')} mm · {a.preview.volume_mm3?.toFixed(0)} mm³
                          {a.preview.mass_kg !== undefined && ` · ${(a.preview.mass_kg * 1000).toFixed(1)} g`}
                        </p>
                        {a.preview.diff && <DiffView diff={a.preview.diff} />}
                        <button className="primary" onClick={() => accept(a)}>Accept</button>
                      </>
                    ) : <p className="err small">Invalid: {a.preview.message}</p>}
                  </div>
                ))}
              </div>
            )}
            {m.diff && <DiffView diff={m.diff} />}
          </div>
        ))}
        {busy && <div className="muted small">Working…</div>}
        <div ref={endRef} />
      </div>
      <div className={`composer ${dragging ? 'dragging' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); attach(e.dataTransfer.files) }}>
        {files.length > 0 && (
          <div className="files">{files.map((f) => (
            <span key={f.id} className="file-chip">📎 {f.name}<button className="link" onClick={() => setFiles(files.filter((x) => x.id !== f.id))}>✕</button></span>
          ))}</div>
        )}
        {selection.length > 0 && <div className="chip">“this” = {selection.map((s) => `${s.kind} of ${s.feature ?? '?'}${s.hole_instance ? ` (hole ${s.hole_instance.index + 1}/${s.hole_instance.count})` : ''}`).join(', ')}</div>}
        <textarea value={text} placeholder="Describe a part or a change…" rows={3} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} />
        <div className="row">
          <button title="Attach an image or PDF (or drop it here)" onClick={() => fileRef.current?.click()}>📎 Attach</button>
          <input ref={fileRef} type="file" accept=".pdf,.png,.jpg,.jpeg,.webp,.gif" multiple hidden
            onChange={(e) => { if (e.target.files) attach(e.target.files); e.target.value = '' }} />
          <button className="primary" style={{ flex: 1 }} onClick={() => send()} disabled={busy || (!text.trim() && !files.length)}>Send</button>
        </div>
      </div>
    </div>
  )
}
