// SPDX-License-Identifier: AGPL-3.0-or-later
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { CSS2DObject, CSS2DRenderer } from 'three/examples/jsm/renderers/CSS2DRenderer.js'
import { refOf, type Model, type Overlay, type SelRef } from './api'

/** What a click selects. 'measure' picks the nearest vertex, then edge, then face. */
export type PickMode = 'feature' | 'face' | 'edge' | 'measure'
export type StdView = 'iso' | 'front' | 'back' | 'top' | 'bottom' | 'right' | 'left'
export type DisplayStyle = 'shaded-edges' | 'shaded' | 'wireframe'

/** Dimension graphics drawn in the 3D view (from a measurement). */
export interface MeasureGraphic {
  line?: [number[], number[]]
  centerLine?: [number[], number[]]
  label: string
  anchor: number[]
}

export interface ViewportHandle {
  fit(): void
  view(v: StdView): void
}

interface Props {
  model: Model | null
  mode: PickMode
  selection: SelRef[]
  selectedFeature: string | null
  onPick: (ref: SelRef | null, additive: boolean) => void
  overlay?: Overlay | null
  markers?: number[][]
  display?: DisplayStyle
  showDims?: boolean
  measure?: MeasureGraphic | null
  /** Assembly: extra motion per component (column-major 4x4), e.g. while dragging a robot joint. */
  poses?: Record<string, number[]>
}

const COLORS = {
  base: 0xb3bfcc, featureSel: 0x4f9dff, faceSel: 0x2f86ff, hover: 0x9cc8ff, edge: 0x1b1f24,
  edgeSel: 0x0a6cff, edgeHover: 0x2fa8ff, vertex: 0x1b1f24, vertexSel: 0x0a6cff, dim: 0xff7a1a,
}

interface Ctx {
  renderer: THREE.WebGLRenderer
  labels: CSS2DRenderer
  scene: THREE.Scene
  camera: THREE.PerspectiveCamera
  controls: OrbitControls
  part: THREE.Group
  faces: THREE.Mesh[]
  edges: THREE.Line[]
  vertices: THREE.Points | null
  groups: Map<string, THREE.Group>
  fitted: boolean
  hover: { kind: 'face' | 'edge' | 'vertex'; index: number } | null
  paint: () => void
}

const VIEW_DIRS: Record<StdView, [number, number, number]> = {
  iso: [1, -1.2, 0.9], front: [0, -1, 0], back: [0, 1, 0], top: [0, 0, 1], bottom: [0, 0, -1], right: [1, 0, 0], left: [-1, 0, 0],
}

const Viewport = forwardRef<ViewportHandle, Props>(function Viewport(
  { model, mode, selection, selectedFeature, onPick, overlay, markers, display = 'shaded-edges', showDims = false, measure, poses }, ref) {
  const hostRef = useRef<HTMLDivElement>(null)
  const ctxRef = useRef<Ctx | null>(null)
  const [glError, setGlError] = useState<string | null>(null)
  const pickRef = useRef({ mode, onPick, model })
  pickRef.current = { mode, onPick, model }
  const paintRef = useRef({ selection, selectedFeature, mode })
  paintRef.current = { selection, selectedFeature, mode }

  useImperativeHandle(ref, () => ({
    fit: () => ctxRef.current && fit(ctxRef.current),
    view: (v) => ctxRef.current && setView(ctxRef.current, v),
  }))
  // Test hook: switch to any standard view.
  ;(window as unknown as { __cadView?: unknown }).__cadView = (v: StdView) => ctxRef.current && setView(ctxRef.current, v)

  // One-time renderer setup.
  useEffect(() => {
    const host = hostRef.current!
    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    } catch (e) {
      // No WebGL (GPU blocklisted, acceleration off, remote session): the rest of the app stays usable.
      setGlError((e as Error).message || 'WebGL is not available')
      return
    }
    renderer.setPixelRatio(window.devicePixelRatio)
    renderer.setClearColor(0x000000, 0) // the CSS gradient behind the canvas is the background
    renderer.autoClear = false
    host.appendChild(renderer.domElement)
    const labels = new CSS2DRenderer()
    labels.domElement.className = 'vp-labels'
    host.appendChild(labels.domElement)

    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 100000)
    camera.up.set(0, 0, 1) // CAD convention: Z up
    camera.position.set(150, -200, 150)
    scene.add(new THREE.HemisphereLight(0xffffff, 0x8090a4, 2.2))
    scene.add(new THREE.AmbientLight(0xffffff, 0.5))
    const sun = new THREE.DirectionalLight(0xffffff, 1.8)
    sun.position.set(1, -2, 3)
    camera.add(sun)
    scene.add(camera)

    const grid = new THREE.GridHelper(400, 40, 0x6b7a8c, 0x8b98a8)
    grid.rotation.x = Math.PI / 2
    grid.position.z = -0.05 // keep the grid from z-fighting with faces at z=0
    grid.name = 'grid'
    for (const m of [grid.material].flat()) Object.assign(m, { transparent: true, opacity: 0.28, depthWrite: false })
    scene.add(grid)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.screenSpacePanning = true
    controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.PAN, RIGHT: THREE.MOUSE.PAN }

    const part = new THREE.Group()
    scene.add(part)

    // Orientation triad, drawn in the lower-left corner with its own camera.
    const triScene = new THREE.Scene()
    const triCam = new THREE.OrthographicCamera(-1.6, 1.6, 1.6, -1.6, 0.1, 10)
    const axes: [THREE.Vector3, number, string][] = [
      [new THREE.Vector3(1, 0, 0), 0xe5484d, 'X'], [new THREE.Vector3(0, 1, 0), 0x30a46c, 'Y'], [new THREE.Vector3(0, 0, 1), 0x3e7bfa, 'Z']]
    for (const [dir, color, name] of axes) {
      triScene.add(new THREE.ArrowHelper(dir, new THREE.Vector3(), 1, color, 0.28, 0.14))
      const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: textTexture(name, color), depthTest: false }))
      s.position.copy(dir).multiplyScalar(1.32)
      s.scale.setScalar(0.42)
      triScene.add(s)
    }

    const ctx: Ctx = {
      renderer, labels, scene, camera, controls, part, faces: [], edges: [], vertices: null, groups: new Map(), fitted: false, hover: null,
      paint: () => paint(ctx, paintRef.current),
    }
    ctxRef.current = ctx
    // Test hook: page coordinates of a model point (used by the automated UI tests).
    ;(window as unknown as { __cadToScreen?: unknown }).__cadToScreen = (x: number, y: number, z: number) => {
      controls.update()
      const v = new THREE.Vector3(x, y, z).project(camera)
      const r = renderer.domElement.getBoundingClientRect()
      return { x: r.left + ((v.x + 1) / 2) * r.width, y: r.top + ((1 - v.y) / 2) * r.height }
    }

    const resize = () => {
      const { clientWidth: w, clientHeight: h } = host
      renderer.setSize(w, h)
      labels.setSize(w, h)
      camera.aspect = w / Math.max(h, 1)
      camera.updateProjectionMatrix()
    }
    const ro = new ResizeObserver(resize)
    ro.observe(host)
    resize()

    const hitAt = (clientX: number, clientY: number) => {
      const { mode, model } = pickRef.current
      const ents = model?.entities
      if (!ents) return null
      const rect = renderer.domElement.getBoundingClientRect()
      const ndc = new THREE.Vector2(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1)
      const ray = new THREE.Raycaster()
      ray.setFromCamera(ndc, camera)
      const dist = camera.position.distanceTo(controls.target)
      ray.params.Line = { threshold: dist / 250 }
      const face = ray.intersectObjects(ctx.faces)[0]
      if (mode === 'measure') {
        // Nearest visible vertex within 9 px, then an edge in front of the face, then the face.
        let best: { i: number; d: number } | null = null
        for (const v of ents.vertices ?? []) {
          const p = new THREE.Vector3(...v.point)
          const s = p.clone().project(camera)
          const d = Math.hypot(((s.x + 1) / 2) * rect.width + rect.left - clientX, ((1 - s.y) / 2) * rect.height + rect.top - clientY)
          if (d > 9 || (best && d >= best.d)) continue
          if (face && face.distance < camera.position.distanceTo(p) - dist / 200) continue // hidden behind a face
          best = { i: v.index, d }
        }
        if (best) return { kind: 'vertex' as const, index: best.i }
        const edge = ray.intersectObjects(ctx.edges)[0]
        if (edge && (!face || edge.distance <= face.distance + dist / 100)) return { kind: 'edge' as const, index: edge.object.userData.index as number }
        return face ? { kind: 'face' as const, index: face.object.userData.index as number } : null
      }
      if (mode === 'edge') {
        const edge = ray.intersectObjects(ctx.edges)[0]
        return edge ? { kind: 'edge' as const, index: edge.object.userData.index as number } : null
      }
      return face ? { kind: 'face' as const, index: face.object.userData.index as number } : null
    }

    // Left click (not drag) picks; shift adds to the selection.
    let down: { x: number; y: number } | null = null
    const onDown = (e: PointerEvent) => (down = { x: e.clientX, y: e.clientY })
    const onUp = (e: PointerEvent) => {
      if (e.button !== 0 || !down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4) return
      const ents = pickRef.current.model?.entities
      if (!ents) return
      const hit = hitAt(e.clientX, e.clientY)
      const ref = !hit ? null : hit.kind === 'vertex' ? refOf(ents.vertices![hit.index], 'vertex')
        : hit.kind === 'edge' ? refOf(ents.edges[hit.index], 'edge') : refOf(ents.faces[hit.index], 'face')
      pickRef.current.onPick(ref, e.shiftKey)
    }
    // Pre-selection highlight under the cursor.
    let pending: PointerEvent | null = null
    const onMove = (e: PointerEvent) => {
      if (e.buttons) { if (ctx.hover) { ctx.hover = null; ctx.paint() } return }
      if (!pending) requestAnimationFrame(() => {
        const ev = pending!
        pending = null
        const h = hitAt(ev.clientX, ev.clientY)
        if (h?.kind !== ctx.hover?.kind || h?.index !== ctx.hover?.index) { ctx.hover = h; ctx.paint() }
        renderer.domElement.style.cursor = h ? 'pointer' : ''
      })
      pending = e
    }
    const onLeave = () => { if (ctx.hover) { ctx.hover = null; ctx.paint() } }
    renderer.domElement.addEventListener('pointerdown', onDown)
    renderer.domElement.addEventListener('pointerup', onUp)
    renderer.domElement.addEventListener('pointermove', onMove)
    renderer.domElement.addEventListener('pointerleave', onLeave)

    let frame = 0
    const loop = () => {
      frame = requestAnimationFrame(loop)
      controls.update()
      const { clientWidth: w, clientHeight: h } = host
      renderer.setViewport(0, 0, w, h)
      renderer.clear()
      renderer.render(scene, camera)
      labels.render(scene, camera)
      // triad follows the main camera's orientation
      triCam.position.copy(camera.position).sub(controls.target).normalize().multiplyScalar(4)
      triCam.up.copy(camera.up)
      triCam.lookAt(0, 0, 0)
      renderer.clearDepth()
      renderer.setViewport(8, 8, 96, 96)
      renderer.render(triScene, triCam)
    }
    loop()
    return () => {
      cancelAnimationFrame(frame)
      ro.disconnect()
      controls.dispose()
      renderer.dispose()
      host.removeChild(renderer.domElement)
      host.removeChild(labels.domElement)
    }
  }, [])

  // Rebuild geometry when the model changes.
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return
    ctx.part.traverse((child) => {
      if (child instanceof THREE.Mesh || child instanceof THREE.Line || child instanceof THREE.Points) child.geometry.dispose()
    })
    ctx.part.clear()
    ctx.groups = new Map()
    // Assembly components get their own group, so they can move (joints) without rebuilding.
    const holder = (component?: string) => {
      if (!component) return ctx.part
      let g = ctx.groups.get(component)
      if (!g) {
        g = new THREE.Group()
        g.matrixAutoUpdate = false
        ctx.groups.set(component, g)
        ctx.part.add(g)
      }
      return g
    }
    ctx.faces = []
    ctx.edges = []
    ctx.vertices = null
    ctx.hover = null
    const ents = model?.entities
    if (!ents) {
      ctx.fitted = false
      return
    }
    for (const f of ents.faces) {
      const g = new THREE.BufferGeometry()
      g.setAttribute('position', new THREE.Float32BufferAttribute(f.vertices, 3))
      g.setIndex(f.triangles)
      g.computeVertexNormals()
      const base = f.color ? readable(f.color) : COLORS.base
      const m = new THREE.Mesh(g, new THREE.MeshStandardMaterial({
        color: base, metalness: 0.2, roughness: 0.5, side: THREE.DoubleSide, flatShading: f.type === 'MESH',
        polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
      }))
      m.userData = { index: f.index, feature: f.feature, base }
      holder(f.component).add(m)
      ctx.faces.push(m)
    }
    for (const e of ents.edges) {
      const g = new THREE.BufferGeometry()
      g.setAttribute('position', new THREE.Float32BufferAttribute(e.polyline, 3))
      const l = new THREE.Line(g, new THREE.LineBasicMaterial({ color: COLORS.edge }))
      l.userData = { index: e.index, feature: e.feature }
      holder(e.component).add(l)
      ctx.edges.push(l)
    }
    if (ents.vertices?.length) {
      const g = new THREE.BufferGeometry()
      g.setAttribute('position', new THREE.Float32BufferAttribute(ents.vertices.flatMap((v) => v.point), 3))
      g.setAttribute('color', new THREE.Float32BufferAttribute(ents.vertices.flatMap(() => [0, 0, 0]), 3))
      ctx.vertices = new THREE.Points(g, new THREE.PointsMaterial({ size: 7, sizeAttenuation: false, vertexColors: true }))
      ctx.part.add(ctx.vertices)
    }
    // floor grid just under the part (robots often have their origin at the hips)
    const grid = ctx.scene.getObjectByName('grid')
    const low = new THREE.Box3().setFromObject(ctx.part).min.z
    if (grid) grid.position.z = Math.min(0, Number.isFinite(low) ? low : 0) - 0.05
    if (!ctx.fitted) {
      setView(ctx, 'iso')
      ctx.fitted = true
    }
  }, [model])

  // Component motion (assembly joints).
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return
    for (const [id, g] of ctx.groups) {
      const p = poses?.[id]
      if (p) g.matrix.fromArray(p)
      else g.matrix.identity()
      g.matrixWorldNeedsUpdate = true
    }
  }, [poses, model])

  // Display style.
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return
    for (const m of ctx.faces) (m.material as THREE.MeshStandardMaterial).visible = display !== 'wireframe'
    for (const l of ctx.edges) l.visible = display !== 'shaded'
  }, [model, display])

  // Analysis overlay (stress colours, optional deformed shape) and finding markers.
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return
    const old = ctx.scene.getObjectByName('analysis')
    if (old) ctx.scene.remove(old)
    const group = new THREE.Group()
    group.name = 'analysis'
    for (const m of ctx.faces) m.visible = !overlay
    if (overlay) {
      const pos = overlay.vertices.slice()
      if (overlay.displacement && overlay.deformScale) for (let i = 0; i < pos.length; i++) pos[i] += overlay.displacement[i] * overlay.deformScale
      const g = new THREE.BufferGeometry()
      g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3))
      g.setIndex(overlay.triangles)
      const colors: number[] = []
      const span = overlay.max - overlay.min || 1
      for (const v of overlay.values) colors.push(...turbo((v - overlay.min) / span))
      g.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3))
      g.computeVertexNormals()
      group.add(new THREE.Mesh(g, new THREE.MeshLambertMaterial({ vertexColors: true, side: THREE.DoubleSide })))
    }
    const size = partSize(ctx)
    for (const p of markers ?? []) {
      const s = new THREE.Mesh(new THREE.SphereGeometry(size / 80, 16, 12), new THREE.MeshBasicMaterial({ color: 0xff2d55, depthTest: false }))
      s.position.set(p[0], p[1], p[2])
      s.renderOrder = 10
      group.add(s)
    }
    ctx.scene.add(group)
  }, [overlay, markers, model])

  // Overall dimensions of the part (bounding box) and the current measurement.
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return
    const old = ctx.scene.getObjectByName('dims')
    if (old) { ctx.scene.remove(old); disposeLabels(old) }
    const group = new THREE.Group()
    group.name = 'dims'
    const size = partSize(ctx)
    if (showDims && model?.entities) {
      const b = new THREE.Box3().setFromObject(ctx.part)
      const off = size * 0.08
      const { min: a, max: z } = b
      const dims: [THREE.Vector3, THREE.Vector3, THREE.Vector3][] = [
        [new THREE.Vector3(a.x, a.y, a.z), new THREE.Vector3(z.x, a.y, a.z), new THREE.Vector3(0, -off, 0)],
        [new THREE.Vector3(z.x, a.y, a.z), new THREE.Vector3(z.x, z.y, a.z), new THREE.Vector3(off, 0, 0)],
        [new THREE.Vector3(a.x, a.y, a.z), new THREE.Vector3(a.x, a.y, z.z), new THREE.Vector3(-off * 0.7, -off * 0.7, 0)],
      ]
      for (const [p, q, o] of dims) {
        const len = p.distanceTo(q)
        if (len < 1e-6) continue
        dimension(group, p, q, o, len.toFixed(2).replace(/\.?0+$/, ''), size, 'overall')
      }
    }
    if (measure) {
      if (measure.centerLine) {
        const [p, q] = measure.centerLine.map((v) => new THREE.Vector3(...v))
        group.add(line([p, q], COLORS.dim, true))
      }
      if (measure.line) {
        const [p, q] = measure.line.map((v) => new THREE.Vector3(...v))
        if (p.distanceTo(q) > 1e-6) dimension(group, p, q, new THREE.Vector3(), '', size, 'measure')
        for (const v of [p, q]) {
          const s = new THREE.Mesh(new THREE.SphereGeometry(size / 160, 12, 8), new THREE.MeshBasicMaterial({ color: COLORS.dim, depthTest: false }))
          s.position.copy(v)
          s.renderOrder = 20
          group.add(s)
        }
      }
      group.add(label(measure.label, new THREE.Vector3(...measure.anchor), 'measure'))
    }
    ctx.scene.add(group)
  }, [model, showDims, measure])

  // Recolour for selection/hover without rebuilding geometry.
  useEffect(() => { ctxRef.current?.paint() }, [model, selection, selectedFeature, mode, display])

  return (
    <div className="viewport" ref={hostRef}>
      {glError && (
        <div className="overlay error">
          <b>3D view unavailable: this browser could not start WebGL.</b>
          <p>Modelling, chat and export still work. Run the desktop app (it has its own GPU-enabled renderer), or turn on
            hardware acceleration in your browser settings and restart it.</p>
          <p className="muted">{glError}</p>
        </div>
      )}
      {!glError && !model?.entities && (
        <div className="overlay hint">No part yet. Start a <b>Sketch</b>, pick a template with <b>Create part…</b>, or describe a part to the assistant.</div>
      )}
    </div>
  )
})

export default Viewport

function paint(ctx: Ctx, { selection, selectedFeature, mode }: { selection: SelRef[]; selectedFeature: string | null; mode: PickMode }) {
  const sel = (k: string) => new Set(selection.filter((s) => s.kind === k).map((s) => s.index))
  const selFaces = sel('face'), selEdges = sel('edge'), selVerts = sel('vertex')
  const hv = ctx.hover
  for (const m of ctx.faces) {
    const i = m.userData.index
    const inFeature = (mode === 'feature' || selection.length === 0) && selectedFeature !== null && m.userData.feature === selectedFeature
    const hoveredFace = hv?.kind === 'face' ? ctx.faces.find((f) => f.userData.index === hv.index) : undefined
    const hovered = hoveredFace && (hoveredFace === m || (mode === 'feature' && m.userData.feature && hoveredFace.userData.feature === m.userData.feature))
    ;(m.material as THREE.MeshStandardMaterial).color.setHex(
      selFaces.has(i) ? COLORS.faceSel : inFeature ? COLORS.featureSel : hovered ? COLORS.hover : m.userData.base ?? COLORS.base)
  }
  for (const l of ctx.edges) {
    const i = l.userData.index
    const mat = l.material as THREE.LineBasicMaterial
    mat.color.setHex(selEdges.has(i) ? COLORS.edgeSel : hv?.kind === 'edge' && hv.index === i ? COLORS.edgeHover : COLORS.edge)
  }
  if (ctx.vertices) {
    const show = mode === 'measure'
    ctx.vertices.visible = show || selVerts.size > 0
    const c = ctx.vertices.geometry.getAttribute('color') as THREE.BufferAttribute
    const base = new THREE.Color(COLORS.vertex), on = new THREE.Color(COLORS.vertexSel), hov = new THREE.Color(COLORS.edgeHover)
    for (let i = 0; i < c.count; i++) {
      const col = selVerts.has(i) ? on : hv?.kind === 'vertex' && hv.index === i ? hov : base
      c.setXYZ(i, col.r, col.g, col.b)
    }
    c.needsUpdate = true
  }
}

/** A component colour, lifted when very dark so shading stays visible. */
function readable(css: string) {
  const c = new THREE.Color(css)
  const hsl = { h: 0, s: 0, l: 0 }
  c.getHSL(hsl)
  if (hsl.l < 0.22) c.setHSL(hsl.h, hsl.s, 0.22)
  return c.getHex()
}

function line(points: THREE.Vector3[], color: number, dashed = false) {
  const g = new THREE.BufferGeometry().setFromPoints(points)
  const len = points[0].distanceTo(points[points.length - 1]) || 1
  const mat = dashed ? new THREE.LineDashedMaterial({ color, dashSize: len / 30, gapSize: len / 50, depthTest: false })
    : new THREE.LineBasicMaterial({ color, depthTest: false })
  const l = new THREE.Line(g, mat)
  if (dashed) l.computeLineDistances()
  l.renderOrder = 15
  return l
}

/** Dimension p–q, offset by o: extension lines, a dimension line with arrowheads, and a label. */
function dimension(group: THREE.Group, p: THREE.Vector3, q: THREE.Vector3, o: THREE.Vector3, text: string, size: number, cls: string) {
  const color = cls === 'measure' ? COLORS.dim : 0x2b6fd6
  const a = p.clone().add(o), b = q.clone().add(o)
  if (o.lengthSq() > 0) {
    const ext = o.clone().normalize().multiplyScalar(size * 0.015)
    group.add(line([p, a.clone().add(ext)], color))
    group.add(line([q, b.clone().add(ext)], color))
  }
  group.add(line([a, b], color))
  const dir = b.clone().sub(a).normalize()
  const head = Math.min(size * 0.03, a.distanceTo(b) / 4)
  for (const [tip, d] of [[a, dir.clone().negate()], [b, dir]] as const) {
    const cone = new THREE.Mesh(new THREE.ConeGeometry(head * 0.3, head, 12), new THREE.MeshBasicMaterial({ color, depthTest: false }))
    cone.position.copy(tip).addScaledVector(d, -head / 2)
    cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), d)
    cone.renderOrder = 15
    group.add(cone)
  }
  if (text) group.add(label(text, a.clone().add(b).multiplyScalar(0.5), cls))
}

function label(text: string, at: THREE.Vector3, cls: string) {
  const el = document.createElement('div')
  el.className = `vp-label ${cls}`
  el.textContent = text
  const o = new CSS2DObject(el)
  o.position.copy(at)
  return o
}

function disposeLabels(obj: THREE.Object3D) {
  obj.traverse((o) => { if (o instanceof CSS2DObject) o.element.remove() })
}

function textTexture(text: string, color: number) {
  const c = document.createElement('canvas')
  c.width = c.height = 64
  const g = c.getContext('2d')!
  g.font = 'bold 44px system-ui, sans-serif'
  g.textAlign = 'center'
  g.textBaseline = 'middle'
  g.fillStyle = `#${color.toString(16).padStart(6, '0')}`
  g.fillText(text, 32, 34)
  return new THREE.CanvasTexture(c)
}

/** Turbo colour map (blue = low, red = high). */
export function turbo(t: number): [number, number, number] {
  const x = Math.min(1, Math.max(0, t))
  const r = 0.1357 + x * (4.5974 - x * (42.3277 - x * (130.5887 - x * (150.5666 - x * 58.1375))))
  const g = 0.0914 + x * (2.1856 + x * (4.8052 - x * (14.0195 - x * (4.2109 + x * 2.7747))))
  const b = 0.1067 + x * (12.5925 - x * (60.1097 - x * (109.0745 - x * (88.5066 - x * 26.8183))))
  return [Math.min(1, Math.max(0, r)), Math.min(1, Math.max(0, g)), Math.min(1, Math.max(0, b))]
}

function partBox(ctx: Ctx) {
  const box = new THREE.Box3().setFromObject(ctx.part)
  return box.isEmpty() ? new THREE.Box3(new THREE.Vector3(-50, -50, -50), new THREE.Vector3(50, 50, 50)) : box
}

function partSize(ctx: Ctx) {
  return partBox(ctx).getSize(new THREE.Vector3()).length() || 100
}

function fit(ctx: Ctx, dir?: THREE.Vector3) {
  const box = partBox(ctx)
  const center = box.getCenter(new THREE.Vector3())
  const size = box.getSize(new THREE.Vector3()).length()
  const d = dir ?? ctx.camera.position.clone().sub(ctx.controls.target).normalize()
  const dist = (size / (2 * Math.tan((ctx.camera.fov * Math.PI) / 360))) * 1.15
  ctx.camera.position.copy(center).addScaledVector(d, dist)
  ctx.controls.target.copy(center)
  ctx.camera.near = size / 1000
  ctx.camera.far = size * 100
  ctx.camera.updateProjectionMatrix()
}

function setView(ctx: Ctx, v: StdView) {
  const d = new THREE.Vector3(...VIEW_DIRS[v]).normalize()
  // Looking straight down/up the Z axis needs a different 'up' to stay well defined.
  const vertical = v === 'top' || v === 'bottom'
  ctx.camera.up.set(0, vertical ? 1 : 0, vertical ? 0 : 1)
  fit(ctx, d)
  ctx.controls.update()
}
