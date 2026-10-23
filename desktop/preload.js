// SPDX-License-Identifier: AGPL-3.0-or-later
// Minimal, explicit bridge: native file dialogs only. The page gets no Node.js access.
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('desktop', {
  openProjectDialog: () => ipcRenderer.invoke('open-project'),
  saveProjectDialog: (defaultName) => ipcRenderer.invoke('save-project', defaultName),
  saveExportDialog: (defaultName, ext) => ipcRenderer.invoke('save-export', defaultName, ext),
  openFileDialog: (title, filters) => ipcRenderer.invoke('open-file', title, filters),
  openFilesDialog: (title, filters) => ipcRenderer.invoke('open-files', title, filters),
  chooseFolderDialog: (title) => ipcRenderer.invoke('choose-folder', title),
  saveFileDialog: (defaultName, filters) => ipcRenderer.invoke('save-file', defaultName, filters),
})
