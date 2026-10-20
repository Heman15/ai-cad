// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from 'react'
import { api, type Settings } from './api'

type Preset = { provider: string; base_url: string | null; model: string; needs_key: boolean; note: string }

const LABELS: Record<string, string> = {
  claude_code: 'Claude Code — your Claude login (no key)', claude: 'Claude (Anthropic API key)', openai: 'OpenAI', gemini: 'Google Gemini (free tier)', groq: 'Groq (free tier)',
  openrouter: 'OpenRouter (free models available)', ollama: 'Ollama — free, runs locally', custom: 'Other OpenAI-compatible API',
}

export default function SettingsDialog({ settings, onSaved, onClose }: {
  settings: Settings; onSaved: (s: Settings) => void; onClose: () => void
}) {
  const presets = (settings as unknown as { presets: Record<string, Preset> }).presets
  const current = Object.entries(presets).find(([, p]) => p.provider === settings.provider && (p.base_url ?? null) === (settings.base_url ?? null))?.[0]
    ?? (settings.provider === 'anthropic' ? 'claude' : 'custom')
  const [preset, setPreset] = useState(current)
  const [key, setKey] = useState('')
  const [model, setModel] = useState(settings.model)
  const [effort, setEffort] = useState(settings.effort)
  const [baseUrl, setBaseUrl] = useState(settings.base_url ?? '')
  const [error, setError] = useState<string | null>(null)
  const p = presets[preset]
  const changedService = preset !== current

  const choose = (name: string) => {
    setPreset(name)
    setModel(presets[name].model)
    setBaseUrl(presets[name].base_url ?? '')
  }

  const save = async (clearKey = false) => {
    try {
      const s = await api.saveSettings({
        provider: p.provider, model, effort, base_url: baseUrl.trim(),
        ...(clearKey ? { api_key: '' } : key.trim() ? { api_key: key.trim() } : {}),
      } as Partial<Settings> & { api_key?: string })
      onSaved(s)
      onClose()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>AI assistant settings</h2>
        <div className="form">
          <label>Provider
            <select value={preset} onChange={(e) => choose(e.target.value)}>
              {Object.keys(presets).map((k) => <option key={k} value={k}>{LABELS[k] ?? k}</option>)}
            </select>
          </label>
          <p className="muted small">{p.note}</p>
          {p.provider === 'openai' && (
            <label>API base URL<input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://…/v1" /></label>
          )}
          <label>Model{p.provider === 'claude_code' ? ' (optional: sonnet, opus… blank = your default)' : ''}<input value={model} onChange={(e) => setModel(e.target.value)} /></label>
          {p.provider !== 'claude_code' && <><label>API key {p.needs_key ? '' : '(not needed for local models)'}
            <input type="password" autoComplete="off" value={key}
              placeholder={!changedService && settings.has_key ? '•••••••• (leave empty to keep)' : p.needs_key ? 'paste your key' : 'optional'}
              onChange={(e) => setKey(e.target.value)} />
          </label>
          <p className="small">Key status: {!changedService && settings.has_key ? <b className="ok">configured ({settings.key_source})</b>
            : <b className={p.needs_key ? 'err' : 'muted'}>{p.needs_key ? 'not set' : 'not required'}</b>}.
            {' '}Stored only in {settings.config_path} (readable by you only), never shown again.</p></>}
          {p.provider === 'claude_code' && <p className="small">Runs Claude Code on this computer with your existing sign-in. If it was never used,
            open Claude Code once (VS Code extension or `claude` in a terminal) and sign in. It may only use the AI CAD tools and read files you attach.</p>}
          {p.provider === 'anthropic' && (
            <label>Reasoning effort
              <select value={effort} onChange={(e) => setEffort(e.target.value)}>
                {['low', 'medium', 'high', 'xhigh', 'max'].map((x) => <option key={x}>{x}</option>)}
              </select>
            </label>
          )}
          <p className="muted small">
            The assistant needs a model that supports <b>tool calling</b>; reading drawings also needs <b>image input</b>.
            Small free models may follow instructions less reliably.
          </p>
          {error && <p className="err small">{error}</p>}
          <div className="row end">
            {settings.key_source === 'config file' && !changedService && <button className="danger-btn" onClick={() => save(true)}>Remove stored key</button>}
            <button onClick={onClose}>Cancel</button>
            <button className="primary" onClick={() => save()}>Save</button>
          </div>
        </div>
      </div>
    </div>
  )
}
