// Test-only boundary substitutes. Never packaged.
const e = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const run = require('../scripts/test-run-directory.cjs').testRunDirectory();
e.app.setPath('userData', path.join(run, 's02-runtime-data'));
globalThis.__s02 = { handlers: new Map(), notifications: [], dialogs: [], trays: [], downloadMode: 'success', login: false };
const state = globalThis.__s02;
const handle = e.ipcMain.handle.bind(e.ipcMain);
e.ipcMain.handle = (name, handler) => { state.handlers.set(name, handler); handle(name, handler); };
class Notice extends EventEmitter { static isSupported() { return true; } constructor(options) { super(); this.options = options; state.notifications.push(this); } show() {} close() {} }
const Tray = e.Tray;
const mockElectron = { ...e, Notification: Notice, Tray: function (...args) { const tray = new Tray(...args); state.trays.push(tray); return tray; } };
const Module = require('node:module');
const load = Module._load;
Module._load = function (request, ...args) { return request === 'electron' ? mockElectron : load.call(this, request, ...args); };
const build = e.Menu.buildFromTemplate.bind(e.Menu);
e.Menu.buildFromTemplate = template => { if (template.some(item => item.label === '退出')) state.menu = template; return build(template); };
e.dialog.showMessageBox = async (_win, options) => { state.dialogs.push(options); return { response: options.cancelId ?? 0 }; };
e.dialog.showSaveDialogSync = () => state.downloadMode === 'cancel' ? undefined : path.join(run, state.downloadMode === 'failure' ? 'target-directory' : 'runtime-saved.csv');
e.app.setLoginItemSettings = settings => { if (state.failStartup) throw new Error('simulated OS failure'); state.login = settings.openAtLogin; };
e.app.getLoginItemSettings = () => ({ openAtLogin: state.login });
require(path.join(run, 'isolated-app/dist/main.js'));
