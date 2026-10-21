// SPDX-License-Identifier: AGPL-3.0-or-later
export type Scalar = number | string | boolean | null
export type Json = Scalar | Json[] | { [k: string]: Json }

export interface Feature {
  id: string
  type: string
  params: Record<string, Json>
  suppressed: boolean
}

export interface Spec {
  name: string
  variables: Record<string, Json>
  limits: Record<string, { min?: number; max?: number }>
  features: Feature[]
  material: { name: string; density: number; E?: number | null; nu?: number | null; yield_strength?: number | null } | null
  units: string
  source: Record<string, Json> | null
  intent: { requirements: string[]; assumptions: string[]; open_questions: string[] } | null
  annotations: (Record<string, Json> & { id: string; type: string })[]
  sheet: { thickness: Json; bend_radius: Json; k_factor: Json } | null
  analysis: { supports: { face: FaceRefJ }[]; loads: { face: FaceRefJ; force?: number[]; pressure?: number }[] } | null
}

export interface FaceRefJ { point: number[]; normal?: number[] }

export interface Overlay {
  vertices: number[]
  triangles: number[]
  values: number[]
  displacement?: number[]
  deformScale: number
  min: number
  max: number
}

export interface FaceEntity {
  index: number
  feature: string | null
  type: string
  point: number[]
  normal?: number[]
  area: number
  center: number[]
  radius?: number
  axis_dir?: number[]
  axis_point?: number[]
  hole_instance?: { index: number; count: number; position: number[]; diameter: number }
  component?: string
  local_point?: number[]
  local_normal?: number[] | null
  color?: string | null
  vertices: number[]
  triangles: number[]
}

export interface EdgeEntity {
  index: number
  feature: string | null
  type: string
  point: number[]
  length: number
  radius?: number
  center?: number[]
  component?: string
  local_point?: number[]
  polyline: number[]
}

export interface VertexEntity {
  index: number
  point: number[]
  component?: string
  local_point?: number[]
}

export interface Model {
  spec: Spec
  parameters: Record<string, Json>
  dependencies: Record<string, { features: string[]; variables: string[]; geometry: boolean }>
  can_undo: boolean
  can_redo: boolean
  path: string | null
  entities: { faces: FaceEntity[]; edges: EdgeEntity[]; vertices?: VertexEntity[] } | null
  volume_mm3: number | null
  bbox_mm: [number, number, number] | null
  mass_kg: number | null
  evaluated: Record<string, number>
  annotations: { id: string; type: string; ok: boolean; message: string; nominal?: number; upper?: number; lower?: number; min?: number; max?: number }[]
  threads?: { feature: string; label: string; internal: boolean; modeled: boolean; length: number }[]
  sheet?: { flat_ok: boolean; flat_size?: [number, number]; bends?: number; message?: string }
}

/** A stored selection: a geometric signature, re-checked against the model after every change. */
export interface SelRef {
  kind: 'face' | 'edge' | 'vertex'
  point: number[]
  type: string
  normal?: number[]
  area?: number
  length?: number
  feature: string | null
  index: number
  radius?: number
  hole_instance?: FaceEntity['hole_instance']
  component?: string
  local_point?: number[]
  local_normal?: number[] | null
}

export interface CommandResult {
  ok: boolean
  message: string
  data?: Record<string, Json>
}

export interface Diff {
  changed: { name: string; old: Json; new: Json }[]
  added: string[]
  removed: string[]
  suppression_changed?: string[]
  material?: { old: string | null; new: string | null }
}

export interface Alternative {
  id: number
  title: string
  description: string
  preview: { ok: boolean; message?: string; diff?: Diff; volume_mm3?: number; bbox_mm?: number[]; mass_kg?: number }
}

export interface Settings {
  ready: boolean
  provider: string
  model: string
  base_url: string | null
  effort: string
  has_key: boolean
  key_source: string | null
  config_path: string
}

export type ChatEvent =
  | { type: 'tool'; tool: string; input: Record<string, Json>; ok: boolean; message: string; model?: Model }
  | { type: 'text'; text: string }
  | { type: 'alternatives'; items: Alternative[] }
  | { type: 'done'; reply: string; diff: Diff; model: Model }
  | { type: 'error'; message: string }

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, { headers: { 'Content-Type': 'application/json' }, ...init })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail ?? `request failed (${res.status})`)
  return data as T
}

const post = <T,>(path: string, body?: unknown) =>
  call<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

type WithModel = { result: CommandResult; model: Model }

export const api = {
  info: () => call<{ commands: string[]; materials: Record<string, number>; feature_types: string[]; ai: Settings }>('/info'),
  model: () => call<Model>('/model'),
  command: (command: string, args: Record<string, unknown>) => post<WithModel>('/command', { command, args }),
  batch: (commands: { command: string; args: Record<string, unknown> }[], label: string) =>
    post<WithModel>('/batch', { commands, label }),
  undo: () => post<WithModel>('/undo'),
  redo: () => post<WithModel>('/redo'),
  newProject: () => post<{ model: Model }>('/new'),
  projectJson: () => call<unknown>('/project'),
  uploadProject: (spec: unknown) => post<{ model: Model }>('/project', spec),
  saveTo: (path: string) => post<{ ok: boolean; path: string; model: Model }>('/project/save', { path }),
  openFrom: (path: string) => post<{ model: Model }>('/project/open', { path }),
  exportTo: (fmt: 'step' | 'stl', path: string) => post<CommandResult>(`/export/${fmt}`, { path }),
  resolve: (refs: SelRef[]) => post<{ items: (Partial<SelRef> & { status: string; reason?: string })[] }>('/resolve', { refs }),
  measure: (refs: SelRef[]) => post<Record<string, Json> & { ok: boolean; message?: string }>('/measure', { refs }),
  settings: () => call<Settings>('/settings'),
  saveSettings: (s: Partial<Settings> & { api_key?: string }) => post<Settings>('/settings', s),
  resetChat: () => post<{ ok: boolean }>('/chat/reset'),
  version: () => call<{ part: string; asm: string }>('/version'),
  shareSelection: (selection: SelRef[]) => post<{ ok: boolean }>('/ui/selection', { selection }),
  acceptAlternative: (id: number) => post<{ result: CommandResult; model: Model }>(`/alternatives/${id}/accept`),

  uploadDrawing: (filename: string, media_type: string, data: string) =>
    post<WithModel>('/source-drawing', { filename, media_type, data }),
  exportDrawing: (path: string, sheet: string, projection: string) =>
    post<{ ok: boolean; message: string }>('/drawing/export', { path, sheet, projection }),

  /** Streams assistant events (server-sent events over a POST response). */
  attach: (filename: string, media_type: string, data: string) =>
    post<{ id: string; name: string; media_type: string }>('/chat/attachment', { filename, media_type, data }),

  async chat(text: string, selection: SelRef[], onEvent: (e: ChatEvent) => void, interpret = false, attachments: string[] = []): Promise<void> {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, selection, interpret_drawing: interpret, attachments }),
    })
    if (!res.ok || !res.body) {
      const detail = await res.json().then((d) => d.detail).catch(() => null)
      throw new Error(detail ?? `chat failed (${res.status})`)
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let i
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i)
        buf = buf.slice(i + 2)
        if (chunk.startsWith('data: ')) onEvent(JSON.parse(chunk.slice(6)))
      }
    }
  },
}

/** Desktop bridge (Electron preload). Absent when running in a normal browser. */
export interface DesktopBridge {
  openProjectDialog(): Promise<string | null>
  saveProjectDialog(defaultName: string): Promise<string | null>
  saveExportDialog(defaultName: string, ext: string): Promise<string | null>
  openFileDialog(title: string, filters: { name: string; extensions: string[] }[]): Promise<string | null>
  saveFileDialog(defaultName: string, filters: { name: string; extensions: string[] }[]): Promise<string | null>
}

export interface DrawingDimension {
  id: string
  value: Json
  unit?: string
  kind: string
  tolerance?: string
  describes: string
  view?: string
  page?: number
  bbox?: [number, number, number, number]
  status: string
  confidence: string
  parameter?: string
  confirmed?: boolean
}

export const desktop: DesktopBridge | undefined = (window as unknown as { desktop?: DesktopBridge }).desktop

export function refOf(e: FaceEntity | EdgeEntity | VertexEntity, kind: 'face' | 'edge' | 'vertex'): SelRef {
  if (kind === 'vertex') {
    const v = e as VertexEntity
    return { kind, point: v.point, type: 'VERTEX', feature: v.component ?? null, index: v.index, component: v.component, local_point: v.local_point }
  }
  const f0 = e as FaceEntity | EdgeEntity
  const base = { kind, point: f0.point, type: f0.type, feature: f0.feature, index: f0.index }
  if (kind === 'face') {
    const f = e as FaceEntity
    return { ...base, normal: f.normal, area: f.area, radius: f.radius, hole_instance: f.hole_instance,
      component: f.component, local_point: f.local_point, local_normal: f.local_normal }
  }
  const g = e as EdgeEntity
  return { ...base, length: g.length, radius: g.radius, component: g.component, local_point: g.local_point }
}

export const fmt = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? '—' : Number(v.toFixed(digits)).toString()
