// SPDX-License-Identifier: AGPL-3.0-or-later
// Desktop shell: starts the Python CAD backend on a free local port and shows the workspace.
// CAD work runs in that separate process, never in the UI thread.
const { app, BrowserWindow, dialog, ipcMain, Menu } = require('electron')
const { spawn } = require('child_process')
const fs = require('fs')
const http = require('http')
const net = require('net')
const path = require('path')

// Packaged app: backend code and a standalone Python live in resources/; development: the repository + .venv.
const ROOT = app.isPackaged ? path.join(process.resourcesPath, 'app') : path.resolve(__dirname, '..')
const PYTHON = process.env.AI_CAD_PYTHON ||
  (app.isPackaged ? path.join(process.resourcesPath, 'python', 'bin', 'python3') : path.join(ROOT, '.venv', 'bin', 'python'))

// WebGL must work even where a browser has blocklisted the GPU.
app.commandLine.appendSwitch('ignore-gpu-blocklist')

let backend = null
let win = null

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer()
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address()
      srv.close(() => resolve(port))
    })
    srv.on('error', reject)
  })
}

function waitForBackend(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve, reject) => {
    const attempt = () => {
      http.get(url, (res) => { res.resume(); resolve() }).on('error', () => {
        if (backend && backend.exitCode !== null) return reject(new Error(`backend exited with code ${backend.exitCode}`))
        if (Date.now() > deadline) return reject(new Error('backend did not start in time'))
        setTimeout(attempt, 250)
      })
    }
    attempt()
  })
}

async function start() {
  if (!fs.existsSync(path.join(ROOT, 'web', 'dist', 'index.html'))) {
    dialog.showErrorBox('AI CAD', 'The UI is not built. Run ./desktop.sh (it builds the UI first).')
    return app.quit()
  }
  if (!fs.existsSync(PYTHON)) {
    dialog.showErrorBox('AI CAD', `Python runtime not found at ${PYTHON}. ${app.isPackaged ? 'The installation is incomplete; reinstall.' : 'See README.md for setup.'}`)
    return app.quit()
  }
  console.log('starting backend')
  const port = await freePort()
  backend = spawn(PYTHON, ['-m', 'uvicorn', 'server.app:app', '--host', '127.0.0.1', '--port', String(port), '--log-level', 'warning'],
    { cwd: ROOT, stdio: ['ignore', 'inherit', 'inherit'], env: { ...process.env, AI_CAD_PARENT_PID: String(process.pid), AI_CAD_PORT: String(port), PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: app.isPackaged ? '1' : '' } })
  try {
    await waitForBackend(`http://127.0.0.1:${port}/api/info`, 60000)
  } catch (e) {
    dialog.showErrorBox('AI CAD', `Could not start the CAD backend: ${e.message}`)
    return app.quit()
  }

  const selftest = process.env.AI_CAD_SELFTEST // path for a screenshot; used by automated checks
  win = new BrowserWindow({
    width: 1500, height: 950, title: 'AI CAD', backgroundColor: '#15181c', show: !selftest, icon: path.join(__dirname, 'icon.png'),
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false },
  })
  // Never navigate away from the local workspace or open other windows.
  win.webContents.on('will-navigate', (e, url) => { if (!url.startsWith(`http://127.0.0.1:${port}`)) e.preventDefault() })
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  win.loadURL(`http://127.0.0.1:${port}/`)
  if (selftest) {
    console.log('SELFTEST loading UI')
    win.webContents.once('did-finish-load', () => { console.log('SELFTEST loaded'); setTimeout(() => runSelftest(selftest), 2500) })
    win.webContents.once('did-fail-load', (_e, code, desc) => { console.log('SELFTEST load failed', code, desc); app.exit(2) })
  }
}

async function runSelftest(out) {
  const report = await win.webContents.executeJavaScript(`({
    workspace: !!document.querySelector('.app .ribbon'),
    canvas: !!document.querySelector('.viewport canvas'),
    webglError: document.querySelector('.overlay.error')?.textContent ?? null,
    bridge: typeof window.desktop?.openProjectDialog === 'function',
  })`)
  const img = await win.webContents.capturePage()
  fs.writeFileSync(out, img.toPNG())
  console.log('SELFTEST', JSON.stringify(report))
  app.exit(report.workspace && report.canvas && !report.webglError && report.bridge ? 0 : 1)
}

const projectFilters = [{ name: 'AI CAD project', extensions: ['aicad', 'json'] }]
const MESH = ['stl', 'obj', 'ply', 'off', '3mf', 'glb', 'gltf', 'dae']
const CAD = ['step', 'stp', 'iges', 'igs', 'brep', 'brp']
// Linux file dialogs match extensions case-sensitively: list upper case too (robot meshes are often .STL).
const both = (exts) => exts.flatMap((e) => [e, e.toUpperCase()])
const openFilters = [
  { name: 'All supported files', extensions: both(['aicad', 'json', ...MESH, ...CAD]) },
  { name: 'AI CAD project', extensions: ['aicad', 'json'] },
  { name: 'Meshes (STL, OBJ, 3MF, PLY…)', extensions: both(MESH) },
  { name: 'CAD (STEP, IGES, BREP)', extensions: both(CAD) },
]

ipcMain.handle('open-project', async () => {
  const r = await dialog.showOpenDialog(win, { properties: ['openFile'], filters: openFilters })
  return r.canceled ? null : r.filePaths[0]
})
ipcMain.handle('open-files', async (_e, title, filters) => {
  const r = await dialog.showOpenDialog(win, { title, properties: ['openFile', 'multiSelections'],
    filters: (filters || []).map((f) => ({ ...f, extensions: both(f.extensions) })) })
  return r.canceled ? null : r.filePaths
})
ipcMain.handle('choose-folder', async (_e, title) => {
  const r = await dialog.showOpenDialog(win, { title, properties: ['openDirectory', 'createDirectory'] })
  return r.canceled ? null : r.filePaths[0]
})
ipcMain.handle('save-project', async (_e, defaultName) => {
  const r = await dialog.showSaveDialog(win, { defaultPath: defaultName, filters: projectFilters })
  return r.canceled ? null : r.filePath
})
ipcMain.handle('save-export', async (_e, defaultName, ext) => {
  const names = { step: 'STEP model', stl: 'STL mesh', gcode: 'G-code' }
  const r = await dialog.showSaveDialog(win, { defaultPath: defaultName, filters: [{ name: names[ext] || ext, extensions: [ext] }] })
  return r.canceled ? null : r.filePath
})

ipcMain.handle('open-file', async (_e, title, filters) => {
  const r = await dialog.showOpenDialog(win, { title, properties: ['openFile'],
    filters: (filters || []).map((f) => ({ ...f, extensions: both(f.extensions) })) })
  return r.canceled ? null : r.filePaths[0]
})
ipcMain.handle('save-file', async (_e, defaultName, filters) => {
  const r = await dialog.showSaveDialog(win, { defaultPath: defaultName, filters })
  return r.canceled ? null : r.filePath
})

Menu.setApplicationMenu(Menu.buildFromTemplate([
  { role: 'fileMenu' }, { role: 'editMenu' },
  { label: 'View', submenu: [{ role: 'reload' }, { role: 'toggleDevTools' }, { type: 'separator' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' }] },
]))

app.whenReady().then(start)
app.on('window-all-closed', () => app.quit())
app.on('will-quit', () => { if (backend && backend.exitCode === null) backend.kill() })
