// SPDX-License-Identifier: AGPL-3.0-or-later
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { refOf, type Model, type Overlay, type SelRef } from './api'

export type PickMode = 'feature' | 'face' | 'edge'
export type StdView = 'iso' | 'front' | 'back' | 'top' | 'bottom' | 'right' | 'left'

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
}

const COLORS = { base: 0x9aa7b4, featureSel: 0xe8833a, faceSel: 0xff9d4d, edge: 0x1b1f24, edgeSel: 0xffb000 }

interface Ctx {
  renderer: THREE.WebGLRenderer
  scene: THREE.Scene
  camera: THREE.PerspectiveCamera
  controls: OrbitControls
  part: THREE.Group
  faces: THREE.Mesh[]
  edges: THREE.Line[]
  fitted: boolean
}

const VIEW_DIRS: Record<StdView, [number, number, number]> = {
  iso: [1, -1.2, 0.9], front: [0, -1, 0], back: [0, 1, 0], top: [0, 0, 1], bottom: [0, 0, -1], right: [1, 0, 0], left: [-1, 0, 0],
}

const Viewport = forwardRef<ViewportHandle, Props>(function Viewport({ model, mode, selection, selectedFeature, onPick, overlay, markers }, ref) {
  const hostRef = useRef<HTMLDivElement>(null)
  const ctxRef = useRef<Ctx | null>(null)
  const [glError, setGlError] = useState<string | null>(null)
  const pickRef = useRef({ mode, onPick, model })
  pickRef.current = { mode, onPick, model }

  useImperativeHandle(ref, () => ({
    fit: () => ctxRef.current && fit(ctxRef.current),
    view: (v) => ctxRef.current && setView(ctxRef.current, v),
  }))
  // Test hook: switch to any standard view (incl. ones without a toolbar button).
  ;(window as unknown as { __cadView?: unknown }).__cadView = (v: StdView) => ctxRef.current && setView(ctxRef.current, v)

  // One-time renderer setup.
  useEffect(() => {
    const host = hostRef.current!
    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true })
    } catch (e) {
      // No WebGL (GPU blocklisted, acceleration off, remote session): the rest of the app stays usable.
      setGlError((e as Error).message || 'WebGL is not available')
      return
    }
    renderer.setPixelRatio(window.devicePixelRatio)
    host.appendChild(renderer.domElement)

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(getComputedStyle(host).getPropertyValue('--viewport-bg').trim() || '#20242b')
    const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 100000)
    camera.up.set(0, 0, 1) // CAD convention: Z up
    camera.position.set(150, -200, 150)
    scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.5))
    const sun = new THREE.DirectionalLight(0xffffff, 1.5)
    sun.position.set(1, -2, 3)
    camera.add(sun)
    scene.add(camera)

    const grid = new THREE.GridHelper(400, 40, 0x556070, 0x3a414c)
    grid.rotation.x = Math.PI / 2
    grid.position.z = -0.05 // keep the grid from z-fighting with faces at z=0
    scene.add(grid)
    scene.add(new THREE.AxesHelper(30))

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.screenSpacePanning = true
    controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.PAN, RIGHT: THREE.MOUSE.PAN }

    const part = new THREE.Group()
    scene.add(part)
    const ctx: Ctx = { renderer, scene, camera, controls, part, faces: [], edges: [], fitted: false }
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
      camera.aspect = w / Math.max(h, 1)
      camera.updateProjectionMatrix()
    }
    const ro = new ResizeObserver(resize)
    ro.observe(host)
    resize()

    // Left click (not drag) picks a face or edge; shift adds to the selection.
    let down: { x: number; y: number } | null = null
    const onDown = (e: PointerEvent) => (down = { x: e.clientX, y: e.clientY })
    const onUp = (e: PointerEvent) => {
      if (e.button !== 0 || !down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4) return
      const { mode, onPick, model } = pickRef.current
      const ents = model?.entities
      if (!ents) return
      const rect = renderer.domElement.getBoundingClientRect()
      const ndc = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1)
      const ray = new THREE.Raycaster()
      ray.setFromCamera(ndc, camera)
      const dist = camera.position.distanceTo(controls.target)
      ray.params.Line = { threshold: dist / 250 }
      if (mode === 'edge') {
        const hit = ray.intersectObjects(ctx.edges)[0]
        onPick(hit ? refOf(ents.edges[hit.object.userData.index], 'edge') : null, e.shiftKey)
      } else {
        const hit = ray.intersectObjects(ctx.faces)[0]
        onPick(hit ? refOf(ents.faces[hit.object.userData.index], 'face') : null, e.shiftKey)
      }
    }
    renderer.domElement.addEventListener('pointerdown', onDown)
    renderer.domElement.addEventListener('pointerup', onUp)

    let frame = 0
    const loop = () => {
      frame = requestAnimationFrame(loop)
      controls.update()
      renderer.render(scene, camera)
    }
    loop()
    return () => {
      cancelAnimationFrame(frame)
      ro.disconnect()
      controls.dispose()
      renderer.dispose()
      host.removeChild(renderer.domElement)
    }
  }, [])

  // Rebuild geometry when the model changes.
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return
    for (const child of [...ctx.part.children]) {
      ctx.part.remove(child)
      if (child instanceof THREE.Mesh || child instanceof THREE.Line) child.geometry.dispose()
    }
    ctx.faces = []
    ctx.edges = []
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
      const m = new THREE.Mesh(g, new THREE.MeshStandardMaterial({
        color: COLORS.base, metalness: 0.15, roughness: 0.6, side: THREE.DoubleSide,
        polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
      }))
      m.userData = { index: f.index, feature: f.feature }
      ctx.part.add(m)
      ctx.faces.push(m)
    }
    for (const e of ents.edges) {
      const g = new THREE.BufferGeometry()
      g.setAttribute('position', new THREE.Float32BufferAttribute(e.polyline, 3))
      const l = new THREE.Line(g, new THREE.LineBasicMaterial({ color: COLORS.edge }))
      l.userData = { index: e.index, feature: e.feature }
      ctx.part.add(l)
      ctx.edges.push(l)
    }
    if (!ctx.fitted) {
      setView(ctx, 'iso')
      ctx.fitted = true
    }
  }, [model])

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
    const size = new THREE.Box3().setFromObject(ctx.part).getSize(new THREE.Vector3()).length() || 100
    for (const p of markers ?? []) {
      const s = new THREE.Mesh(new THREE.SphereGeometry(size / 80, 16, 12), new THREE.MeshBasicMaterial({ color: 0xff2d55, depthTest: false }))
      s.position.set(p[0], p[1], p[2])
      s.renderOrder = 10
      group.add(s)
    }
    ctx.scene.add(group)
  }, [overlay, markers, model])

  // Recolour for selection without rebuilding geometry.
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return
    const selFaces = new Set(selection.filter((s) => s.kind === 'face').map((s) => s.index))
    const selEdges = new Set(selection.filter((s) => s.kind === 'edge').map((s) => s.index))
    for (const m of ctx.faces) {
      const inFeature = (mode === 'feature' || selection.length === 0) && selectedFeature !== null && m.userData.feature === selectedFeature
      ;(m.material as THREE.MeshStandardMaterial).color.setHex(
        selFaces.has(m.userData.index) ? COLORS.faceSel : inFeature ? COLORS.featureSel : COLORS.base)
    }
    for (const l of ctx.edges) {
      ;(l.material as THREE.LineBasicMaterial).color.setHex(selEdges.has(l.userData.index) ? COLORS.edgeSel : COLORS.edge)
    }
  }, [model, selection, selectedFeature, mode])

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
        <div className="overlay hint">No part yet. Use “Create” on the left, or describe a part in the chat.</div>
      )}
    </div>
  )
})

export default Viewport

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
