// SPDX-License-Identifier: AGPL-3.0-or-later
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js'
import { LineSegments2 } from 'three/examples/jsm/lines/LineSegments2.js'
import { LineSegmentsGeometry } from 'three/examples/jsm/lines/LineSegmentsGeometry.js'

/** Build plate: outline polygon (mm), bounding box and printable height. */
export interface Bed { shape: number[][]; min: number[]; max: number[]; center: number[]; height: number; circular: boolean }
/** Triangles of a plate object, centred on its own origin. */
export interface MeshData { V: Float32Array; F: Uint32Array }
/** One copy of an object on the plate: rotation/scale matrix (3x3, row-major) and the bed position of its centre. */
export interface PlateItem { key: string; object: string; matrix: number[]; x: number; y: number }
export interface Toolpaths { positions: Float32Array; types: Uint8Array; layerZ: Float32Array; layerStart: Uint32Array; typeNames: string[] }

/** Feature colours of the G-code preview (PrusaSlicer's legend). */
export const TYPE_COLORS: Record<string, string> = {
  'Perimeter': '#ffb000', 'External perimeter': '#ff7d38', 'Overhang perimeter': '#1f6fff', 'Internal infill': '#b03029',
  'Solid infill': '#9654cc', 'Top solid infill': '#f04040', 'Ironing': '#ff8c69', 'Bridge infill': '#4d80ba', 'Gap fill': '#d8d8d8',
  'Skirt/Brim': '#00876e', 'Support material': '#3fd63f', 'Support material interface': '#008000', 'Wipe tower': '#b3e3ab',
  'Custom': '#5ed194', 'Travel': '#3a7bd5',
}

export interface PrintViewHandle { fit(): void; view(v: 'iso' | 'top' | 'front'): void }

interface Props {
  bed: Bed | null
  texture?: string | null
  items: PlateItem[]
  meshes: Map<string, MeshData>
  selected: string[]
  outside: string[]
  mode: 'prepare' | 'preview'
  toolpaths: Toolpaths | null
  layers: [number, number]
  hiddenTypes: Set<number>
  facePick: boolean
  onSelect: (key: string | null, additive: boolean) => void
  onMove: (key: string, x: number, y: number) => void
  onFacePick: (key: string, normal: number[]) => void
}

/** World bounds and 4x4 matrix of an item: matrix applied, centred at (x, y), resting on the bed. */
const placeCache = new Map<string, { m4: THREE.Matrix4; min: number[]; max: number[] }>()
export function placement(mesh: MeshData, item: Pick<PlateItem, 'object' | 'matrix' | 'x' | 'y'>) {
  const key = `${item.object}|${item.matrix.join(',')}`
  let base = placeCache.get(key)
  if (!base) {
    const m = item.matrix
    const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity]
    const V = mesh.V
    for (let i = 0; i < V.length; i += 3) {
      const x = V[i], y = V[i + 1], z = V[i + 2]
      const p = [m[0] * x + m[1] * y + m[2] * z, m[3] * x + m[4] * y + m[5] * z, m[6] * x + m[7] * y + m[8] * z]
      for (let k = 0; k < 3; k++) { if (p[k] < lo[k]) lo[k] = p[k]; if (p[k] > hi[k]) hi[k] = p[k] }
    }
    const m4 = new THREE.Matrix4().set(m[0], m[1], m[2], 0, m[3], m[4], m[5], 0, m[6], m[7], m[8], 0, 0, 0, 0, 1)
    base = { m4, min: lo, max: hi }
    if (placeCache.size > 200) placeCache.clear()
    placeCache.set(key, base)
  }
  const t = [item.x - (base.min[0] + base.max[0]) / 2, item.y - (base.min[1] + base.max[1]) / 2, -base.min[2]]
  const m4 = new THREE.Matrix4().makeTranslation(t[0], t[1], t[2]).multiply(base.m4)
  return { m4, min: base.min.map((v, k) => v + t[k]), max: base.max.map((v, k) => v + t[k]) }
}

const COL = { object: 0xf2c14e, selected: 0x4f9dff, outside: 0xe05555, bed: 0xd9dee6, bedDark: 0x2b313a }

interface Ctx {
  renderer: THREE.WebGLRenderer
  scene: THREE.Scene
  camera: THREE.PerspectiveCamera
  controls: OrbitControls
  bedGroup: THREE.Group
  objGroup: THREE.Group
  pathGroup: THREE.Group
  geoms: Map<string, THREE.BufferGeometry>
  meshes: Map<string, THREE.Mesh>
  bed: Bed | null
  paths: { lines: THREE.LineSegments; layerStart: Uint32Array; top: LineSegments2 | null; pos: Float32Array; col: Float32Array } | null
}

const PrintView = forwardRef<PrintViewHandle, Props>(function PrintView(props, ref) {
  const host = useRef<HTMLDivElement>(null)
  const ctxRef = useRef<Ctx | null>(null)
  const propsRef = useRef(props)
  propsRef.current = props
  const [glError, setGlError] = useState<string | null>(null)

  useImperativeHandle(ref, () => ({
    fit: () => ctxRef.current && fitBed(ctxRef.current),
    view: (v) => ctxRef.current && fitBed(ctxRef.current, v),
  }))

  // Renderer, camera, controls, picking and dragging.
  useEffect(() => {
    const el = host.current!
    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    } catch (e) {
      setGlError((e as Error).message || 'WebGL is not available')
      return
    }
    renderer.setPixelRatio(window.devicePixelRatio)
    renderer.setClearColor(0x000000, 0)
    el.appendChild(renderer.domElement)
    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(35, 1, 1, 100000)
    camera.up.set(0, 0, 1)
    scene.add(new THREE.HemisphereLight(0xffffff, 0x8090a4, 2.0))
    scene.add(new THREE.AmbientLight(0xffffff, 0.45))
    const sun = new THREE.DirectionalLight(0xffffff, 1.7)
    sun.position.set(1, -2, 3)
    camera.add(sun)
    scene.add(camera)
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.screenSpacePanning = true
    controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.PAN, RIGHT: THREE.MOUSE.PAN }
    const bedGroup = new THREE.Group(), objGroup = new THREE.Group(), pathGroup = new THREE.Group()
    scene.add(bedGroup, objGroup, pathGroup)
    const ctx: Ctx = { renderer, scene, camera, controls, bedGroup, objGroup, pathGroup, geoms: new Map(), meshes: new Map(), bed: null, paths: null }
    ctxRef.current = ctx

    const resize = () => {
      const w = el.clientWidth, h = el.clientHeight
      if (!w || !h) return
      renderer.setSize(w, h)
      camera.aspect = w / h
      camera.updateProjectionMatrix()
      const top = ctx.paths?.top
      if (top) (top.material as LineMaterial).resolution.set(w, h)
    }
    const ro = new ResizeObserver(resize)
    ro.observe(el)
    resize()

    // ----- picking and dragging objects on the bed -----
    const ray = new THREE.Raycaster()
    const ndc = (e: PointerEvent) => {
      const r = renderer.domElement.getBoundingClientRect()
      return new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1)
    }
    const hitObject = (e: PointerEvent) => {
      ray.setFromCamera(ndc(e), camera)
      const hits = ray.intersectObjects([...ctx.meshes.values()].filter((m) => m.visible), false)
      return hits[0] ?? null
    }
    const bedPoint = (e: PointerEvent) => {
      ray.setFromCamera(ndc(e), camera)
      const p = new THREE.Vector3()
      return ray.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 0, 1), 0), p) ? p : null
    }
    let down: { x: number; y: number; key: string | null; start: THREE.Vector3 | null; item: PlateItem | null; moved: boolean } | null = null
    const onDown = (e: PointerEvent) => {
      if (e.button !== 0) return
      const P = propsRef.current
      const hit = P.mode === 'prepare' ? hitObject(e) : null
      const key = hit ? (hit.object.userData.key as string) : null
      if (hit && P.facePick && hit.face) {
        const n = hit.face.normal.clone().applyMatrix3(new THREE.Matrix3().getNormalMatrix(hit.object.matrixWorld)).normalize()
        P.onFacePick(key!, [n.x, n.y, n.z])
        return
      }
      const item = key ? P.items.find((i) => i.key === key) ?? null : null
      if (item) {
        controls.enabled = false
        renderer.domElement.setPointerCapture(e.pointerId)
        if (!P.selected.includes(item.key) || e.shiftKey) P.onSelect(item.key, e.shiftKey)
      }
      down = { x: e.clientX, y: e.clientY, key, start: item ? bedPoint(e) : null, item, moved: false }
    }
    const onMove = (e: PointerEvent) => {
      if (!down?.item || !down.start) return
      if (Math.hypot(e.clientX - down.x, e.clientY - down.y) < 3 && !down.moved) return
      const p = bedPoint(e)
      if (!p) return
      down.moved = true
      const mesh = ctx.meshes.get(down.item.key)
      const data = propsRef.current.meshes.get(down.item.object)
      if (mesh && data) {
        const pl = placement(data, { ...down.item, x: down.item.x + p.x - down.start.x, y: down.item.y + p.y - down.start.y })
        mesh.matrix.copy(pl.m4)
      }
    }
    const onUp = (e: PointerEvent) => {
      if (!down) return
      const d = down
      down = null
      controls.enabled = true
      if (d.item && d.moved && d.start) {
        const p = bedPoint(e)
        if (p) propsRef.current.onMove(d.item.key, round(d.item.x + p.x - d.start.x), round(d.item.y + p.y - d.start.y))
      } else if (!d.key && Math.hypot(e.clientX - d.x, e.clientY - d.y) < 4) {
        propsRef.current.onSelect(null, false)
      }
    }
    renderer.domElement.addEventListener('pointerdown', onDown)
    renderer.domElement.addEventListener('pointermove', onMove)
    renderer.domElement.addEventListener('pointerup', onUp)

    let raf = 0
    const tick = () => {
      raf = requestAnimationFrame(tick)
      controls.update()
      renderer.render(scene, camera)
    }
    tick()
    ;(window as unknown as { __printView?: unknown }).__printView = { scene, camera }
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      controls.dispose()
      renderer.dispose()
      renderer.domElement.remove()
      ctxRef.current = null
    }
  }, [])

  // Bed: plate, grid, build volume outline, optional printer texture.
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return
    clear(ctx.bedGroup)
    ctx.bed = props.bed
    if (!props.bed) return
    buildBed(ctx.bedGroup, props.bed, props.texture ?? null)
    fitBed(ctx)
  }, [props.bed, props.texture])

  // Objects.
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return
    const keep = new Set(props.items.map((i) => i.key))
    for (const [k, m] of ctx.meshes) if (!keep.has(k)) { ctx.objGroup.remove(m); (m.material as THREE.Material).dispose(); ctx.meshes.delete(k) }
    const used = new Set(props.items.map((i) => i.object))
    for (const [o, g] of ctx.geoms) if (!used.has(o)) { g.dispose(); ctx.geoms.delete(o) }
    const preview = props.mode === 'preview' && !!props.toolpaths
    for (const item of props.items) {
      const data = props.meshes.get(item.object)
      if (!data) continue
      let g = ctx.geoms.get(item.object)
      if (!g) {
        g = new THREE.BufferGeometry()
        g.setAttribute('position', new THREE.BufferAttribute(data.V, 3))
        g.setIndex(new THREE.BufferAttribute(data.F, 1))
        g.computeVertexNormals()
        g.computeBoundingSphere()
        ctx.geoms.set(item.object, g)
      }
      let mesh = ctx.meshes.get(item.key)
      if (!mesh || mesh.geometry !== g) {
        if (mesh) ctx.objGroup.remove(mesh)
        mesh = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ flatShading: true, metalness: 0.05, roughness: 0.65, side: THREE.DoubleSide }))
        mesh.matrixAutoUpdate = false
        mesh.userData.key = item.key
        ctx.objGroup.add(mesh)
        ctx.meshes.set(item.key, mesh)
      }
      mesh.matrix.copy(placement(data, item).m4)
      const mat = mesh.material as THREE.MeshStandardMaterial
      const sel = props.selected.includes(item.key)
      mat.color.setHex(props.outside.includes(item.key) ? COL.outside : sel ? COL.selected : COL.object)
      mat.emissive.setHex(sel ? 0x0b2a5a : 0x000000)
      mat.transparent = preview
      mat.opacity = preview ? 0.12 : 1
      mat.depthWrite = !preview
    }
  }, [props.items, props.meshes, props.selected, props.outside, props.mode, props.toolpaths])

  // Toolpaths (G-code preview): one line set, drawn up to the chosen layer; the top layer is drawn wide.
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return
    clear(ctx.pathGroup)
    ctx.paths = null
    const tp = props.toolpaths
    if (!tp) return
    const n = tp.types.length
    const colors = tp.typeNames.map((t) => new THREE.Color(TYPE_COLORS[t] ?? '#cccccc'))
    let count = 0
    for (let i = 0; i < n; i++) if (!props.hiddenTypes.has(tp.types[i])) count++
    const pos = new Float32Array(count * 6), col = new Float32Array(count * 6)
    const layerStart = new Uint32Array(tp.layerStart.length + 1)
    let L = 0, j = 0
    for (let i = 0; i < n; i++) {
      while (L < tp.layerStart.length && tp.layerStart[L] <= i) layerStart[L++] = j
      const t = tp.types[i]
      if (props.hiddenTypes.has(t)) continue
      pos.set(tp.positions.subarray(i * 6, i * 6 + 6), j * 6)
      const c = colors[t]
      col[j * 6] = col[j * 6 + 3] = c.r
      col[j * 6 + 1] = col[j * 6 + 4] = c.g
      col[j * 6 + 2] = col[j * 6 + 5] = c.b
      j++
    }
    while (L <= tp.layerStart.length) layerStart[L++] = j
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3))
    g.setAttribute('color', new THREE.BufferAttribute(col, 3))
    const lines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ vertexColors: true }))
    lines.frustumCulled = false
    ctx.pathGroup.add(lines)
    ctx.paths = { lines, layerStart, top: null, pos, col }
  }, [props.toolpaths, props.hiddenTypes])

  // Layer range.
  useEffect(() => {
    const ctx = ctxRef.current
    const p = ctx?.paths
    if (!ctx || !p) return
    const nl = p.layerStart.length - 1
    const lo = Math.max(0, Math.min(props.layers[0], nl - 1)), hi = Math.max(lo, Math.min(props.layers[1], nl - 1))
    const a = p.layerStart[lo], b = p.layerStart[hi + 1]
    const topA = p.layerStart[hi]
    p.lines.geometry.setDrawRange(a * 2, (topA - a) * 2)
    if (p.top) { ctx.pathGroup.remove(p.top); p.top.geometry.dispose(); (p.top.material as LineMaterial).dispose(); p.top = null }
    if (b > topA) {
      const geo = new LineSegmentsGeometry()
      geo.setPositions(p.pos.subarray(topA * 6, b * 6))
      geo.setColors(p.col.subarray(topA * 6, b * 6))
      const el = host.current!
      const mat = new LineMaterial({ vertexColors: true, linewidth: 2.5, resolution: new THREE.Vector2(el.clientWidth, el.clientHeight) })
      p.top = new LineSegments2(geo, mat)
      p.top.frustumCulled = false
      ctx.pathGroup.add(p.top)
    }
  }, [props.layers, props.toolpaths, props.hiddenTypes])

  useEffect(() => {
    const ctx = ctxRef.current
    if (ctx) ctx.pathGroup.visible = props.mode === 'preview'
  }, [props.mode, props.toolpaths])

  return (
    <div ref={host} className={`viewport print-view ${props.facePick ? 'face-pick' : ''}`}>
      {glError && <div className="overlay error">3D view unavailable: {glError}</div>}
    </div>
  )
})

export default PrintView

const round = (v: number) => Math.round(v * 100) / 100

function clear(g: THREE.Group) {
  for (const c of [...g.children]) {
    g.remove(c)
    c.traverse((o) => {
      const any = o as THREE.Mesh
      any.geometry?.dispose?.()
      const m = any.material as THREE.Material | THREE.Material[] | undefined
      ;[m].flat().forEach((x) => x?.dispose?.())
    })
  }
}

function buildBed(group: THREE.Group, bed: Bed, texture: string | null) {
  const dark = document.documentElement.dataset.theme === 'dark' ||
    (!document.documentElement.dataset.theme && window.matchMedia?.('(prefers-color-scheme: dark)').matches)
  const shape = new THREE.Shape(bed.shape.map(([x, y]) => new THREE.Vector2(x, y)))
  const plateMat = new THREE.MeshStandardMaterial({ color: dark ? COL.bedDark : COL.bed, roughness: 0.9, metalness: 0, side: THREE.DoubleSide,
    polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 })
  const plate = new THREE.Mesh(new THREE.ShapeGeometry(shape, 64), plateMat)
  plate.position.z = -0.02
  group.add(plate)
  if (texture && texture.endsWith('.svg')) {
    new THREE.TextureLoader().load(texture, (tex) => {
      const [x0, y0] = bed.min, [x1, y1] = bed.max
      const geo = new THREE.PlaneGeometry(x1 - x0, y1 - y0)
      const m = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ map: tex, transparent: true, opacity: 0.55, depthWrite: false }))
      m.position.set((x0 + x1) / 2, (y0 + y1) / 2, -0.01)
      group.add(m)
    }, undefined, () => { /* no texture: plain plate */ })
  }
  // Grid every 10 mm inside the bed outline, a heavier line every 50 mm.
  const inside = (x: number, y: number) => pointInPoly(x, y, bed.shape)
  const minor: number[] = [], major: number[] = []
  const [x0, y0] = bed.min, [x1, y1] = bed.max
  const step = 10
  for (let x = Math.ceil(x0 / step) * step; x <= x1 + 1e-6; x += step) segmentsAlong(minor, major, x, y0, y1, true, inside, x % 50 === 0)
  for (let y = Math.ceil(y0 / step) * step; y <= y1 + 1e-6; y += step) segmentsAlong(minor, major, y, x0, x1, false, inside, y % 50 === 0)
  const lineMat = (o: number) => new THREE.LineBasicMaterial({ color: dark ? 0x8b95a3 : 0x5d6b7c, transparent: true, opacity: o, depthWrite: false })
  for (const [arr, o] of [[minor, 0.22], [major, 0.45]] as [number[], number][]) {
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.Float32BufferAttribute(arr, 3))
    group.add(new THREE.LineSegments(g, lineMat(o)))
  }
  // Build volume: outline at the bottom and top, joined at the corners (or 16 lines for a round bed).
  const pts = bed.shape
  const vol: number[] = []
  const h = bed.height
  for (let i = 0; i < pts.length; i++) {
    const [ax, ay] = pts[i], [bx, by] = pts[(i + 1) % pts.length]
    vol.push(ax, ay, 0, bx, by, 0, ax, ay, h, bx, by, h)
    if (!bed.circular || i % Math.max(1, Math.floor(pts.length / 16)) === 0) vol.push(ax, ay, 0, ax, ay, h)
  }
  const vg = new THREE.BufferGeometry()
  vg.setAttribute('position', new THREE.Float32BufferAttribute(vol, 3))
  group.add(new THREE.LineSegments(vg, new THREE.LineBasicMaterial({ color: dark ? 0x6f7b8b : 0x7d8a9b, transparent: true, opacity: 0.6 })))
  // Origin marker (X red, Y green), as printers count from it.
  const ax = new THREE.AxesHelper(Math.min(x1 - x0, y1 - y0) * 0.12)
  ax.position.set(0, 0, 0.05)
  group.add(ax)
}

function segmentsAlong(minor: number[], major: number[], c: number, a0: number, a1: number, vertical: boolean,
  inside: (x: number, y: number) => boolean, heavy: boolean) {
  const out = heavy ? major : minor
  const n = 120
  let start: number | null = null
  for (let i = 0; i <= n; i++) {
    const a = a0 + ((a1 - a0) * i) / n
    const ok = vertical ? inside(c, a) : inside(a, c)
    if (ok && start === null) start = a
    if ((!ok || i === n) && start !== null) {
      const end = ok ? a : a0 + ((a1 - a0) * (i - 1)) / n
      if (end > start) out.push(...(vertical ? [c, start, 0, c, end, 0] : [start, c, 0, end, c, 0]))
      start = null
    }
  }
}

export function pointInPoly(x: number, y: number, poly: number[][]): boolean {
  let inside = false
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j]
    if ((yi > y) !== (yj > y) && x <= ((xj - xi) * (y - yi)) / (yj - yi) + xi + 1e-6) inside = !inside
  }
  // Points on the outline count as inside (beds are often exactly the grid's extent).
  return inside || poly.some(([px, py], i) => {
    const [qx, qy] = poly[(i + 1) % poly.length]
    const cross = (qx - px) * (y - py) - (qy - py) * (x - px)
    return Math.abs(cross) < 1e-6 && x >= Math.min(px, qx) - 1e-6 && x <= Math.max(px, qx) + 1e-6 && y >= Math.min(py, qy) - 1e-6 && y <= Math.max(py, qy) + 1e-6
  })
}

function fitBed(ctx: Ctx, v: 'iso' | 'top' | 'front' = 'iso') {
  const b = ctx.bed
  const lo = b ? b.min : [-100, -100], hi = b ? b.max : [100, 100]
  const h = b ? b.height : 100
  const c = new THREE.Vector3((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, v === 'top' ? 0 : h * 0.15)
  const size = Math.max(hi[0] - lo[0], hi[1] - lo[1], h * 0.6)
  const dir = v === 'top' ? new THREE.Vector3(0, -0.001, 1) : v === 'front' ? new THREE.Vector3(0, -1, 0.35) : new THREE.Vector3(0.55, -1.1, 0.85)
  const dist = size / (2 * Math.tan((ctx.camera.fov * Math.PI) / 360)) * 1.35
  ctx.camera.position.copy(c).add(dir.normalize().multiplyScalar(dist))
  ctx.camera.near = Math.max(0.5, dist / 1000)
  ctx.camera.far = dist * 20
  ctx.camera.updateProjectionMatrix()
  ctx.controls.target.copy(c)
  ctx.controls.update()
}
