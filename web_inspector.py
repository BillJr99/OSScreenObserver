"""
web_inspector.py — Human-facing inspection interface.

Exposes a Flask HTTP server on localhost:5001 (configurable) with:

  GET  /                   — Single-page inspection UI
  GET  /api/windows        — List windows
  GET  /api/structure      — Accessibility tree JSON
  GET  /api/description    — Textual description (mode param)
  GET  /api/sketch         — ASCII layout sketch
  GET  /api/screenshot     — Screenshot as base64 PNG
  POST /api/action         — Execute an input action

The HTML/CSS/JS is inlined as a template string so the entire server is a
single importable Python module with no external static files.
"""

import base64
import logging
import traceback
from typing import Optional

from flask import Flask, jsonify, request
from flask_cors import CORS

from ascii_renderer import ASCIIRenderer
from description import DescriptionGenerator
from observer import ScreenObserver
import tools as _tools
from errors import http_status_for

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# HTML template (single-page application, no external dependencies)
# ─────────────────────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>OS Screen Observer</title>
<style>
/* ── Reset & root ─────────────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:        #03080f;
  --surface:   #060e1c;
  --panel:     #0a1628;
  --border:    #0e2a45;
  --border2:   #1a4a6e;
  --text:      #7ab8d9;
  --text-hi:   #b8dff0;
  --text-dim:  #3a6a88;
  --cyan:      #00c8f0;
  --amber:     #f0a830;
  --green:     #30f090;
  --red:       #f04848;
  --mono:      'Cascadia Code', 'JetBrains Mono', 'Consolas', 'Courier New', monospace;
  --sans:      'Segoe UI', system-ui, sans-serif;
}

html, body { height: 100%; overflow: hidden; background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 13px; }

/* ── Layout ───────────────────────────────────────────────────────────────── */
#shell { display: grid; grid-template-rows: 44px 1fr; grid-template-columns: 260px 1fr; height: 100vh; }

/* ── Header ───────────────────────────────────────────────────────────────── */
#header {
  grid-column: 1 / -1;
  display: flex; align-items: center; gap: 16px;
  padding: 0 20px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
#header .logo { font-family: var(--mono); font-size: 15px; color: var(--cyan); letter-spacing: 0.12em; }
#header .badge {
  font-family: var(--mono); font-size: 10px; padding: 2px 8px;
  border: 1px solid var(--border2); color: var(--text-dim);
  border-radius: 2px; letter-spacing: 0.08em;
}
#header .spacer { flex: 1; }
#mock-indicator { font-family: var(--mono); font-size: 10px; color: var(--amber); letter-spacing: 0.1em; display: none; }
#mock-indicator.visible { display: inline; }
#status-text { font-family: var(--mono); font-size: 10px; color: var(--text-dim); }
#auto-refresh-wrap { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--text-dim); }
#auto-refresh-wrap input { accent-color: var(--cyan); }

/* ── Sidebar ──────────────────────────────────────────────────────────────── */
#sidebar {
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  overflow: hidden;
}
#sidebar-header {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
}
#sidebar-header h2 { font-family: var(--mono); font-size: 10px; letter-spacing: 0.15em; color: var(--text-dim); text-transform: uppercase; }
#refresh-btn {
  background: none; border: 1px solid var(--border2); color: var(--cyan);
  font-family: var(--mono); font-size: 10px; padding: 3px 8px; cursor: pointer;
  letter-spacing: 0.08em; border-radius: 2px; transition: all 0.15s;
}
#refresh-btn:hover { background: var(--border); }
#window-list { flex: 1; overflow-y: auto; padding: 6px 0; }
.win-item {
  padding: 8px 14px; cursor: pointer;
  border-left: 3px solid transparent;
  transition: all 0.12s;
  display: flex; flex-direction: column; gap: 2px;
}
.win-item:hover  { background: var(--panel); border-color: var(--border2); }
.win-item.active { background: var(--panel); border-color: var(--cyan); }
.win-title { font-family: var(--mono); font-size: 11px; color: var(--text-hi); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 220px; }
.win-proc  { font-family: var(--mono); font-size: 9px; color: var(--text-dim); }
.win-focused { display: inline-block; font-size: 9px; color: var(--green); margin-left: 4px; }

/* ── Main content ─────────────────────────────────────────────────────────── */
#main { display: flex; flex-direction: column; overflow: hidden; background: var(--bg); }

/* ── Tabs ─────────────────────────────────────────────────────────────────── */
#tab-bar {
  display: flex; align-items: stretch;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  padding: 0 12px;
  gap: 2px;
}
.tab-btn {
  background: none; border: none; border-bottom: 2px solid transparent;
  color: var(--text-dim); font-family: var(--mono); font-size: 10px;
  letter-spacing: 0.12em; padding: 10px 14px; cursor: pointer; text-transform: uppercase;
  transition: all 0.15s;
}
.tab-btn:hover  { color: var(--text); }
.tab-btn.active { color: var(--cyan); border-color: var(--cyan); }
.tab-spacer { flex: 1; }

/* Mode selector (description tab) */
#mode-wrap { display: none; align-items: center; gap: 6px; padding-right: 4px; }
#mode-wrap.visible { display: flex; }
#mode-select {
  background: var(--panel); border: 1px solid var(--border2); color: var(--text);
  font-family: var(--mono); font-size: 10px; padding: 3px 6px;
}
#fetch-btn {
  background: none; border: 1px solid var(--cyan); color: var(--cyan);
  font-family: var(--mono); font-size: 10px; padding: 3px 10px;
  cursor: pointer; letter-spacing: 0.08em; border-radius: 2px; transition: all 0.15s;
}
#fetch-btn:hover { background: var(--cyan); color: var(--bg); }

/* ── Tab panels ───────────────────────────────────────────────────────────── */
#tab-content { flex: 1; overflow: hidden; position: relative; }
.tab-panel { display: none; position: absolute; inset: 0; overflow: auto; padding: 16px 20px; }
.tab-panel.active { display: block; }

/* Empty state */
.empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; gap: 12px; color: var(--text-dim); font-family: var(--mono); font-size: 12px; letter-spacing: 0.08em; }
.empty-state .icon { font-size: 36px; opacity: 0.3; }

/* ── JSON tree ────────────────────────────────────────────────────────────── */
#structure-panel { font-family: var(--mono); font-size: 11px; }
.j-node { margin-left: 16px; }
.j-key   { color: var(--cyan); }
.j-str   { color: var(--green); }
.j-num   { color: var(--amber); }
.j-bool  { color: var(--red); }
.j-null  { color: var(--text-dim); }
details > summary { cursor: pointer; user-select: none; list-style: none; }
details > summary::before { content: '▶ '; color: var(--text-dim); font-size: 9px; }
details[open] > summary::before { content: '▼ '; }
details > summary::-webkit-details-marker { display: none; }
.j-children { border-left: 1px solid var(--border); margin-left: 10px; padding-left: 6px; }

/* ── Description panel ───────────────────────────────────────────────────── */
#description-panel pre {
  font-family: var(--mono); font-size: 11px; line-height: 1.6;
  color: var(--text-hi); white-space: pre-wrap; word-break: break-word;
}
.desc-section { margin-bottom: 20px; }
.desc-label { font-family: var(--mono); font-size: 9px; letter-spacing: 0.15em; color: var(--text-dim); text-transform: uppercase; margin-bottom: 6px; padding: 2px 8px; border-left: 2px solid var(--cyan); }

/* ── Sketch panel ────────────────────────────────────────────────────────── */
#sketch-panel pre {
  font-family: var(--mono); font-size: 10.5px; line-height: 1.35;
  color: var(--text-hi); white-space: pre;
  background: var(--surface); border: 1px solid var(--border);
  padding: 12px 16px; border-radius: 2px;
  overflow-x: auto;
}

/* ── Screenshot panel ────────────────────────────────────────────────────── */
#screenshot-panel { display: flex; flex-direction: column; align-items: flex-start; gap: 12px; }
#screenshot-panel img { max-width: 100%; border: 1px solid var(--border2); image-rendering: auto; }
.shot-meta { font-family: var(--mono); font-size: 10px; color: var(--text-dim); }

/* ── Actions panel ───────────────────────────────────────────────────────── */
#actions-panel { display: flex; flex-direction: column; gap: 20px; max-width: 540px; }
.action-group { background: var(--surface); border: 1px solid var(--border); padding: 14px 16px; border-radius: 2px; }
.action-group h3 { font-family: var(--mono); font-size: 10px; letter-spacing: 0.15em; color: var(--cyan); text-transform: uppercase; margin-bottom: 10px; }
.action-row { display: flex; gap: 8px; align-items: flex-end; flex-wrap: wrap; }
.field { display: flex; flex-direction: column; gap: 4px; }
.field label { font-family: var(--mono); font-size: 9px; color: var(--text-dim); letter-spacing: 0.1em; text-transform: uppercase; }
.field input {
  background: var(--panel); border: 1px solid var(--border2); color: var(--text-hi);
  font-family: var(--mono); font-size: 11px; padding: 5px 8px;
  outline: none; transition: border 0.15s; width: 180px;
}
.field input:focus { border-color: var(--cyan); }
.field input.small { width: 70px; }
.action-btn {
  background: var(--panel); border: 1px solid var(--border2); color: var(--amber);
  font-family: var(--mono); font-size: 10px; padding: 6px 14px; cursor: pointer;
  letter-spacing: 0.08em; border-radius: 2px; transition: all 0.15s; white-space: nowrap;
}
.action-btn:hover { background: var(--amber); color: var(--bg); }
.action-result { font-family: var(--mono); font-size: 10px; padding: 6px 10px; background: var(--panel); border-left: 2px solid var(--green); color: var(--text); margin-top: 8px; display: none; }
.action-result.error { border-color: var(--red); color: var(--red); }
.action-result.visible { display: block; }

/* ── Scrollbar ────────────────────────────────────────────────────────────── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-dim); }

/* ── Spinner ──────────────────────────────────────────────────────────────── */
.spinner { display: inline-block; width: 12px; height: 12px; border: 2px solid var(--border2); border-top-color: var(--cyan); border-radius: 50%; animation: spin 0.7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div id="shell">

  <!-- ── Header ────────────────────────────────────────────────────────── -->
  <header id="header">
    <span class="logo">OS SCREEN OBSERVER</span>
    <span class="badge">MCP + INSPECTOR</span>
    <span id="mock-indicator">⚠ MOCK DATA</span>
    <span class="spacer"></span>
    <label id="auto-refresh-wrap">
      <input type="checkbox" id="auto-refresh"/> AUTO‑REFRESH 3s
    </label>
    <span id="status-text">READY</span>
  </header>

  <!-- ── Sidebar ───────────────────────────────────────────────────────── -->
  <aside id="sidebar">
    <div id="sidebar-header">
      <h2>WINDOWS</h2>
      <button id="refresh-btn" onclick="loadWindows()">↺ SCAN</button>
    </div>
    <div id="window-list">
      <div class="empty-state"><span class="icon">⬛</span>scanning…</div>
    </div>
  </aside>

  <!-- ── Main ──────────────────────────────────────────────────────────── -->
  <main id="main">
    <div id="tab-bar">
      <button class="tab-btn active" data-tab="structure">STRUCTURE</button>
      <button class="tab-btn" data-tab="description">DESCRIPTION</button>
      <button class="tab-btn" data-tab="sketch">SKETCH</button>
      <button class="tab-btn" data-tab="screenshot">SCREENSHOT</button>
      <button class="tab-btn" data-tab="actions">ACTIONS</button>
      <span class="tab-spacer"></span>
      <div id="mode-wrap">
        <select id="mode-select">
          <option value="accessibility">accessibility</option>
          <option value="ocr">ocr</option>
          <option value="vlm">vlm</option>
          <option value="combined">combined</option>
        </select>
        <button id="fetch-btn" onclick="loadDescription()">FETCH</button>
      </div>
    </div>

    <div id="tab-content">
      <div class="tab-panel active" id="panel-structure">
        <div class="empty-state"><span class="icon">🌲</span>SELECT A WINDOW</div>
      </div>
      <div class="tab-panel" id="panel-description">
        <div class="empty-state"><span class="icon">📄</span>SELECT A WINDOW</div>
      </div>
      <div class="tab-panel" id="panel-sketch">
        <div class="empty-state"><span class="icon">🗺</span>SELECT A WINDOW</div>
      </div>
      <div class="tab-panel" id="panel-screenshot">
        <div class="empty-state"><span class="icon">📷</span>SELECT A WINDOW</div>
      </div>
      <div class="tab-panel" id="panel-actions">
        <div id="actions-panel">
          <div class="action-group">
            <h3>CLICK AT COORDINATES</h3>
            <div class="action-row">
              <div class="field"><label>X</label><input type="number" id="click-x" class="small" placeholder="0"/></div>
              <div class="field"><label>Y</label><input type="number" id="click-y" class="small" placeholder="0"/></div>
              <div class="field">
                <label>BUTTON</label>
                <select id="click-btn" style="background:var(--panel);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;padding:5px 8px;">
                  <option>left</option><option>right</option><option>middle</option>
                </select>
              </div>
              <div class="field" style="flex-direction:row;align-items:center;gap:6px;margin-bottom:1px;">
                <input type="checkbox" id="click-double" style="accent-color:var(--cyan)"/>
                <label style="text-transform:none;font-size:11px;cursor:pointer;" for="click-double">double</label>
              </div>
              <button class="action-btn" onclick="doClick()">CLICK</button>
            </div>
            <div class="action-result" id="click-result"></div>
          </div>
          <div class="action-group">
            <h3>TYPE TEXT</h3>
            <div class="action-row">
              <div class="field"><label>TEXT</label><input type="text" id="type-text" placeholder="text to type…"/></div>
              <button class="action-btn" onclick="doType()">TYPE</button>
            </div>
            <div class="action-result" id="type-result"></div>
          </div>
          <div class="action-group">
            <h3>PRESS KEY / COMBO</h3>
            <div class="action-row">
              <div class="field"><label>KEYS</label><input type="text" id="key-combo" placeholder="ctrl+c, enter, alt+f4…"/></div>
              <button class="action-btn" onclick="doKey()">SEND</button>
            </div>
            <div class="action-result" id="key-result"></div>
          </div>
          <div class="action-group">
            <h3>BRING WINDOW TO FOREGROUND</h3>
            <div class="action-row">
              <span style="color:var(--text-dim);font-size:11px;font-family:var(--mono)">Clicks the title bar of the selected window to raise it.</span>
              <button class="action-btn" onclick="doBringToForeground('bring-result-actions')">BRING TO FRONT</button>
            </div>
            <div class="action-result" id="bring-result-actions"></div>
          </div>
        </div>
      </div>
    </div>
  </main>

</div>

<script>
// ── State ────────────────────────────────────────────────────────────────────
let selectedIndex = null;
let activeTab = 'structure';
let autoTimer = null;

// ── Tab switching ────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    activeTab = btn.dataset.tab;
    document.getElementById('panel-' + activeTab).classList.add('active');
    document.getElementById('mode-wrap').classList.toggle('visible', activeTab === 'description');
    if (selectedIndex !== null && activeTab !== 'actions') loadTab(activeTab);
  });
});

// ── Auto-refresh ─────────────────────────────────────────────────────────────
document.getElementById('auto-refresh').addEventListener('change', function() {
  clearInterval(autoTimer);
  if (this.checked) {
    autoTimer = setInterval(() => {
      loadWindows(false);
      if (selectedIndex !== null && activeTab !== 'actions') loadTab(activeTab);
    }, 3000);
  }
});

// ── Utility ──────────────────────────────────────────────────────────────────
function setStatus(msg) { document.getElementById('status-text').textContent = msg; }

async function apiFetch(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function spinner() { return '<span class="spinner"></span>'; }

// ── Window list ───────────────────────────────────────────────────────────────
async function loadWindows(selectFirst = false) {
  setStatus('SCANNING…');
  try {
    const data = await apiFetch('/api/windows');
    const list = document.getElementById('window-list');

    if (data.is_mock) document.getElementById('mock-indicator').classList.add('visible');
    if (!data.windows || data.windows.length === 0) {
      list.innerHTML = '<div class="empty-state"><span class="icon">⬛</span>NO WINDOWS FOUND</div>';
      setStatus('NO WINDOWS');
      return;
    }

    list.innerHTML = data.windows.map((w, i) => `
      <div class="win-item${i === selectedIndex ? ' active' : ''}" onclick="selectWindow(${i})">
        <span class="win-title">${esc(truncate(w.title, 34))}${w.focused ? '<span class="win-focused">●</span>':''}  </span>
        <span class="win-proc">${esc(w.process)} · PID ${w.pid} · ${w.bounds.width}×${w.bounds.height}</span>
      </div>`).join('');

    if (selectFirst && selectedIndex === null && data.windows.length > 0) {
      selectWindow(data.windows.findIndex(w => w.focused) >= 0
        ? data.windows.findIndex(w => w.focused) : 0);
    }
    setStatus(`${data.windows.length} WINDOWS`);
  } catch(e) {
    setStatus('ERROR');
    console.error(e);
  }
}

function selectWindow(idx) {
  selectedIndex = idx;
  document.querySelectorAll('.win-item').forEach((el, i) =>
    el.classList.toggle('active', i === idx));
  if (activeTab !== 'actions') loadTab(activeTab);
}

// ── Tab loaders ───────────────────────────────────────────────────────────────
function loadTab(tab) {
  if (tab === 'structure')   loadStructure();
  else if (tab === 'description') loadDescription();
  else if (tab === 'sketch')      loadSketch();
  else if (tab === 'screenshot')  loadScreenshot();
}

async function loadStructure() {
  const panel = document.getElementById('panel-structure');
  panel.innerHTML = spinner();
  setStatus('LOADING TREE…');
  try {
    const url = '/api/structure' + (selectedIndex !== null ? `?window_index=${selectedIndex}` : '');
    const data = await apiFetch(url);
    if (data.error) { panel.innerHTML = `<pre style="color:var(--red)">${esc(data.error)}</pre>`; return; }
    const info = `<div style="font-family:var(--mono);font-size:9px;color:var(--text-dim);margin-bottom:10px;letter-spacing:.1em">`
      + `WINDOW: ${esc(data.window)}  ·  ${data.element_count} ELEMENTS</div>`;
    panel.innerHTML = info + renderJSON(data.tree);
    setStatus(`${data.element_count} ELEMENTS`);
  } catch(e) { panel.innerHTML = `<pre style="color:var(--red)">${esc(String(e))}</pre>`; setStatus('ERROR'); }
}

async function loadDescription() {
  const panel = document.getElementById('panel-description');
  const mode  = document.getElementById('mode-select').value;
  panel.innerHTML = spinner();
  setStatus('FETCHING DESCRIPTION…');
  try {
    const idx = selectedIndex !== null ? `&window_index=${selectedIndex}` : '';
    const data = await apiFetch(`/api/description?mode=${mode}${idx}`);
    if (data.error) { panel.innerHTML = `<pre style="color:var(--red)">${esc(data.error)}</pre>`; return; }

    let html = '';
    const modes = ['accessibility', 'ocr', 'vlm'];
    if (data.description !== undefined) {
      html = `<div class="desc-section"><div class="desc-label">${esc(mode)}</div><pre>${esc(data.description)}</pre></div>`;
    } else {
      for (const m of modes) {
        if (data[m] !== undefined) {
          html += `<div class="desc-section"><div class="desc-label">${m}</div><pre>${esc(data[m])}</pre></div>`;
        }
      }
    }
    panel.innerHTML = html || '<pre>No description returned.</pre>';
    setStatus('READY');
  } catch(e) { panel.innerHTML = `<pre style="color:var(--red)">${esc(String(e))}</pre>`; setStatus('ERROR'); }
}

async function loadSketch() {
  const panel = document.getElementById('panel-sketch');
  panel.innerHTML = spinner();
  setStatus('RENDERING SKETCH…');
  try {
    const idx = selectedIndex !== null ? `?window_index=${selectedIndex}` : '';
    const data = await apiFetch(`/api/sketch${idx}`);
    if (data.error) { panel.innerHTML = `<pre style="color:var(--red)">${esc(data.error)}</pre>`; return; }
    panel.innerHTML = `<div style="font-family:var(--mono);font-size:9px;color:var(--text-dim);margin-bottom:8px;letter-spacing:.1em">WINDOW: ${esc(data.window)}  ·  ${data.grid_width}×${data.grid_height} GRID</div>`
      + `<pre>${esc(data.sketch)}</pre>`;
    setStatus('READY');
  } catch(e) { panel.innerHTML = `<pre style="color:var(--red)">${esc(String(e))}</pre>`; setStatus('ERROR'); }
}

async function loadScreenshot() {
  const panel = document.getElementById('panel-screenshot');
  panel.innerHTML = spinner();
  setStatus('CAPTURING…');
  try {
    const idx = selectedIndex !== null ? `?window_index=${selectedIndex}` : '';
    const [shotData, areas] = await Promise.all([
      apiFetch(`/api/screenshot${idx}`),
      selectedIndex !== null ? apiFetch(`/api/visible_areas?window_index=${selectedIndex}`).catch(() => null) : Promise.resolve(null),
    ]);
    if (shotData.error) { panel.innerHTML = `<pre style="color:var(--red)">${esc(shotData.error)}</pre>`; return; }

    let html = `<div id="screenshot-panel">
      <span class="shot-meta">WINDOW: ${esc(shotData.window)}</span>
      <img src="data:image/png;base64,${shotData.data}" alt="screenshot"/>`;

    if (areas && areas.visible_regions) {
      const regs = areas.visible_regions;
      html += `<div class="desc-label" style="margin-top:14px">VISIBLE AREAS (${regs.length} region${regs.length !== 1 ? 's' : ''})</div>`;
      html += `<pre style="font-size:10px;color:var(--text-hi);background:var(--surface);border:1px solid var(--border);padding:8px 12px">${esc(JSON.stringify(regs, null, 2))}</pre>`;
      html += `<button class="action-btn" style="margin-top:8px" onclick="doBringToForeground('bring-result-shot')">BRING TO FRONT</button>`;
      html += `<div class="action-result" id="bring-result-shot"></div>`;
    }

    html += `<div class="desc-label" style="margin-top:14px">FULL DISPLAY</div>
      <button class="action-btn" id="load-full-display-btn" onclick="loadFullDisplay()">CAPTURE ALL MONITORS</button>
      <div id="full-display-content"></div>
    </div>`;
    panel.innerHTML = html;
    setStatus('READY');
  } catch(e) { panel.innerHTML = `<pre style="color:var(--red)">${esc(String(e))}</pre>`; setStatus('ERROR'); }
}

async function loadFullDisplay() {
  const btn = document.getElementById('load-full-display-btn');
  const container = document.getElementById('full-display-content');
  if (btn) btn.disabled = true;
  if (container) container.innerHTML = spinner();
  setStatus('CAPTURING ALL MONITORS…');
  try {
    const idx = selectedIndex !== null ? `?window_index=${selectedIndex}` : '';
    const data = await apiFetch(`/api/full_screenshot${idx}`);
    if (data.error) { if (container) container.innerHTML = `<pre style="color:var(--red)">${esc(data.error)}</pre>`; setStatus('ERROR'); return; }
    const dimMeta = data.width ? ` · ${data.width}×${data.height}px` : '';
    let html = `<span class="shot-meta" style="margin-top:8px;display:block">ALL MONITORS${dimMeta}</span>
      <img src="data:image/png;base64,${data.data}" alt="full display screenshot" style="max-width:100%"/>`;
    if (data.sketch) {
      html += `<div class="desc-label" style="margin-top:14px">ASCII SKETCH (selected window)</div>`;
      html += `<pre style="font-size:10px;line-height:1.3;color:var(--text-hi);background:var(--surface);border:1px solid var(--border);padding:12px 16px;overflow-x:auto">${esc(data.sketch)}</pre>`;
    }
    if (container) container.innerHTML = html;
    setStatus('READY');
  } catch(e) {
    if (container) container.innerHTML = `<pre style="color:var(--red)">${esc(String(e))}</pre>`;
    setStatus('ERROR');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── Actions ───────────────────────────────────────────────────────────────────
async function postAction(payload, resultId) {
  const el = document.getElementById(resultId);
  el.classList.remove('visible', 'error');
  setStatus('EXECUTING…');
  try {
    const r = await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await r.json();
    el.textContent = JSON.stringify(data);
    el.classList.add('visible');
    el.classList.toggle('error', data.success === false);
    setStatus(data.success ? 'ACTION OK' : 'ACTION FAILED');
  } catch(e) {
    el.textContent = String(e);
    el.classList.add('visible', 'error');
    setStatus('ERROR');
  }
}

function doClick() {
  const x = parseInt(document.getElementById('click-x').value) || 0;
  const y = parseInt(document.getElementById('click-y').value) || 0;
  const btn = document.getElementById('click-btn').value;
  const dbl = document.getElementById('click-double').checked;
  postAction({action:'click_at', x, y, button: btn, double: dbl}, 'click-result');
}

function doType() {
  const text = document.getElementById('type-text').value;
  postAction({action:'type', value: text}, 'type-result');
}

function doKey() {
  const keys = document.getElementById('key-combo').value;
  postAction({action:'key', value: keys}, 'key-result');
}

async function doBringToForeground(resultId) {
  const el = document.getElementById(resultId);
  if (el) el.classList.remove('visible', 'error');
  if (selectedIndex === null) {
    if (el) { el.textContent = 'No window selected — pick one from the sidebar first.'; el.classList.add('visible', 'error'); }
    setStatus('NO WINDOW SELECTED');
    return;
  }
  setStatus('BRINGING TO FOREGROUND…');
  try {
    const data = await apiFetch(`/api/bring_to_foreground?window_index=${selectedIndex}`);
    if (el) {
      el.textContent = JSON.stringify(data);
      el.classList.add('visible');
      el.classList.toggle('error', data.success === false);
    }
    setStatus(data.success !== false ? 'ACTION OK' : 'ACTION FAILED');
  } catch(e) {
    if (el) { el.textContent = String(e); el.classList.add('visible', 'error'); }
    setStatus('ERROR');
  }
}

// ── JSON tree renderer ────────────────────────────────────────────────────────
function renderJSON(obj, depth = 0) {
  if (obj === null) return `<span class="j-null">null</span>`;
  if (typeof obj === 'boolean') return `<span class="j-bool">${obj}</span>`;
  if (typeof obj === 'number') return `<span class="j-num">${obj}</span>`;
  if (typeof obj === 'string') {
    const s = obj.length > 120 ? obj.slice(0, 117) + '…' : obj;
    return `<span class="j-str">"${esc(s)}"</span>`;
  }
  if (Array.isArray(obj)) {
    if (obj.length === 0) return '[]';
    const items = obj.map(v => `<div class="j-node">${renderJSON(v, depth+1)}</div>`).join('');
    return depth < 2
      ? `<details open><summary><span style="color:var(--text-dim)">[${obj.length}]</span></summary><div class="j-children">${items}</div></details>`
      : `<details><summary><span style="color:var(--text-dim)">[${obj.length}]</span></summary><div class="j-children">${items}</div></details>`;
  }
  if (typeof obj === 'object') {
    const entries = Object.entries(obj);
    if (entries.length === 0) return '{}';
    const rows = entries.map(([k, v]) => {
      if (k === 'children') {
        const label = v.length ? `children [${v.length}]` : 'children []';
        if (!v.length) return `<div class="j-node"><span class="j-key">${k}</span>: []</div>`;
        const inner = v.map(c => `<div class="j-node">${renderJSON(c, depth+1)}</div>`).join('');
        return `<div class="j-node"><details${depth < 2 ? ' open' : ''}><summary><span class="j-key">${k}</span> <span style="color:var(--text-dim)">[${v.length}]</span></summary><div class="j-children">${inner}</div></details></div>`;
      }
      return `<div class="j-node"><span class="j-key">${esc(k)}</span>: ${renderJSON(v, depth+1)}</div>`;
    }).join('');

    const name = obj.name ? ` "${truncate(obj.name,28)}"` : '';
    const role = obj.role || '';
    const label = role ? `${esc(role)}${esc(name)}` : '{…}';

    if (depth === 0) return `<div class="j-node">${rows}</div>`;
    return `<details${depth < 3 ? ' open' : ''}>
      <summary><span class="j-key">${label}</span></summary>
      <div class="j-children">${rows}</div>
    </details>`;
  }
  return String(obj);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function truncate(s, n) { return s.length > n ? s.slice(0, n-1) + '…' : s; }

// ── Boot ──────────────────────────────────────────────────────────────────────
loadWindows(true);
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Flask application factory
# ─────────────────────────────────────────────────────────────────────────────

def create_web_app(
    observer:  ScreenObserver,
    renderer:  ASCIIRenderer,
    describer: DescriptionGenerator,
    config:    dict,
) -> Flask:
    """
    Create and configure the Flask inspection application.

    All routes are defined inside this factory so they close over the
    shared observer/renderer/describer instances.
    """
    app = Flask(__name__)
    CORS(app)

    ctx = _tools.ToolContext(observer=observer, renderer=renderer,
                              describer=describer, config=config)

    def _tool_response(name: str, args: dict):
        result = _tools.dispatch(ctx, name, args)
        if not result.get("ok", True):
            code = (result.get("error") or {}).get("code", "Internal")
            return jsonify(result), http_status_for(code)
        return jsonify(result)

    def _merge_query(extra: Optional[dict] = None) -> dict:
        out = {k: v for k, v in request.args.items()}
        if "window_index" in out:
            try:
                out["window_index"] = int(out["window_index"])
            except (TypeError, ValueError):
                pass
        if extra:
            out.update(extra)
        return out

    # ── UI ────────────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return _HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

    # ── API helpers ───────────────────────────────────────────────────────────

    def _window_from_args():
        """Resolve window_index query param → (WindowInfo | None, hwnd | None)."""
        windows  = observer.list_windows()
        raw      = request.args.get("window_index")
        idx      = int(raw) if raw is not None else None
        info     = observer.window_by_index(windows, idx)
        hwnd     = info.handle if info else None
        return info, hwnd, windows

    # ── /api/windows ──────────────────────────────────────────────────────────

    @app.route("/api/windows")
    def api_windows():
        return _tool_response("list_windows", {})

    # ── /api/structure ────────────────────────────────────────────────────────

    @app.route("/api/structure")
    def api_structure():
        # Forwards through tools.dispatch so callers can use roles=,
        # name_regex=, prune_empty=, max_nodes=, page_cursor= filters.
        args = _merge_query()
        for key in ("roles", "exclude_roles"):
            if key in args and isinstance(args[key], str):
                args[key] = [s for s in args[key].split(",") if s]
        for bool_key in ("visible_only", "prune_empty"):
            if bool_key in args:
                args[bool_key] = str(args[bool_key]).lower() in ("1", "true", "yes")
        for int_key in ("max_text_len", "max_nodes"):
            if int_key in args:
                try:
                    args[int_key] = int(args[int_key])
                except (TypeError, ValueError):
                    args.pop(int_key, None)
        return _tool_response("get_window_structure", args)

    # ── /api/description ──────────────────────────────────────────────────────

    @app.route("/api/description")
    def api_description():
        args = _merge_query()
        if "max_tokens" in args:
            try:
                args["max_tokens"] = int(args["max_tokens"])
            except (TypeError, ValueError):
                args.pop("max_tokens", None)
        return _tool_response("get_screen_description", args)

    # ── /api/sketch ───────────────────────────────────────────────────────────

    @app.route("/api/sketch")
    def api_sketch():
        try:
            info, hwnd, _ = _window_from_args()
            tree = observer.get_element_tree(hwnd)
            if tree is None:
                return jsonify({"error": "Could not retrieve element tree"}), 500

            gw  = request.args.get("grid_width",  type=int)
            gh  = request.args.get("grid_height", type=int)
            ref = info.bounds if info else tree.bounds

            # Optional OCR overlay: pass ?ocr=1 to enable Tesseract text overlay.
            # Requires pytesseract + tesseract on PATH; silently skipped otherwise.
            shot_bytes: Optional[bytes] = None
            if request.args.get("ocr", "").strip() in ("1", "true", "yes"):
                shot_bytes = observer.get_screenshot(hwnd)

            sketch = renderer.render(
                root             = tree,
                screen_bounds    = ref,
                grid_width       = gw,
                grid_height      = gh,
                screenshot_bytes = shot_bytes,
            )
            return jsonify({
                "window":      info.title if info else "(focused)",
                "grid_width":  gw or renderer.default_width,
                "grid_height": gh or renderer.default_height,
                "ocr_overlay": shot_bytes is not None,
                "sketch":      sketch,
            })
        except Exception as e:
            print(f"[web_inspector:/api/sketch] {e}")
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # ── /api/screenshot ───────────────────────────────────────────────────────

    @app.route("/api/screenshot")
    def api_screenshot():
        try:
            info, hwnd, _ = _window_from_args()
            shot = observer.get_screenshot(hwnd)
            if shot is None:
                return jsonify({"error": "Screenshot capture failed"}), 500
            return jsonify({
                "window":   info.title if info else "(full screen)",
                "format":   "png",
                "encoding": "base64",
                "data":     base64.b64encode(shot).decode(),
            })
        except Exception as e:
            print(f"[web_inspector:/api/screenshot] {e}")
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # ── /api/full_screenshot ──────────────────────────────────────────────────

    @app.route("/api/full_screenshot")
    def api_full_screenshot():
        """All-monitor screenshot + optional ASCII sketch in one call."""
        try:
            info, hwnd, _ = _window_from_args()
            # Always capture the full virtual desktop (all monitors combined)
            shot = observer.get_full_display_screenshot()
            if shot is None:
                return jsonify({"error": "Screenshot capture failed"}), 500

            sketch: Optional[str] = None
            tree = observer.get_element_tree(hwnd) if hwnd is not None else None
            if tree is not None:
                gw  = request.args.get("grid_width",  type=int)
                gh  = request.args.get("grid_height", type=int)
                ref = info.bounds if info else observer.get_screen_bounds()
                # Crop the full-display PNG to the window's bounds so that OCR
                # word coordinates (which are window-relative in ascii_renderer)
                # align correctly with the sketch grid.
                ocr_bytes = shot
                if info is not None:
                    try:
                        import io as _io2
                        from PIL import Image as _Image2
                        full_img = _Image2.open(_io2.BytesIO(shot))
                        screen_b = observer.get_screen_bounds()
                        crop_box = (
                            info.bounds.x - screen_b.x,
                            info.bounds.y - screen_b.y,
                            info.bounds.right - screen_b.x,
                            info.bounds.bottom - screen_b.y,
                        )
                        crop_box = (
                            max(0, crop_box[0]),
                            max(0, crop_box[1]),
                            min(full_img.width,  crop_box[2]),
                            min(full_img.height, crop_box[3]),
                        )
                        buf2 = _io2.BytesIO()
                        full_img.crop(crop_box).save(buf2, format="PNG")
                        ocr_bytes = buf2.getvalue()
                    except Exception:
                        pass
                sketch = renderer.render(
                    root             = tree,
                    screen_bounds    = ref,
                    grid_width       = gw,
                    grid_height      = gh,
                    screenshot_bytes = ocr_bytes,
                )

            try:
                import io as _io
                from PIL import Image as _Image
                _img = _Image.open(_io.BytesIO(shot))
                img_w, img_h = _img.size
            except Exception:
                img_w = img_h = None

            return jsonify({
                "window":          info.title if info else "(full screen)",
                "screenshot_scope": "full_display",
                "format":          "png",
                "encoding":        "base64",
                "width":           img_w,
                "height":          img_h,
                "data":            base64.b64encode(shot).decode(),
                "sketch":          sketch,
            })
        except Exception as e:
            print(f"[web_inspector:/api/full_screenshot] {e}")
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # ── /api/visible_areas ────────────────────────────────────────────────────

    @app.route("/api/visible_areas")
    def api_visible_areas():
        """Visible (non-occluded, on-screen) bounding boxes for a window."""
        try:
            info, hwnd, windows = _window_from_args()
            if hwnd is None:
                return jsonify({"error": "window_index is required"}), 400
            areas = observer.get_visible_areas(hwnd, windows)
            return jsonify({
                "window":          info.title if info else "(unknown)",
                "visible_regions": areas,
            })
        except Exception as e:
            print(f"[web_inspector:/api/visible_areas] {e}")
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # ── /api/bring_to_foreground ──────────────────────────────────────────────

    @app.route("/api/bring_to_foreground")
    def api_bring_to_foreground():
        """Click the title bar of a window to bring it to the foreground."""
        try:
            info, hwnd, windows = _window_from_args()
            if hwnd is None:
                return jsonify({"success": False,
                                "error": "window_index is required"}), 400
            result = observer.bring_to_foreground(hwnd, windows)
            result["window"] = info.title if info else "(unknown)"
            return jsonify(result)
        except Exception as e:
            print(f"[web_inspector:/api/bring_to_foreground] {e}")
            traceback.print_exc()
            return jsonify({"success": False, "error": str(e)}), 500

    # ── /api/action ───────────────────────────────────────────────────────────

    @app.route("/api/action", methods=["POST"])
    def api_action():
        body = request.get_json(force=True) or {}
        action = body.get("action", "")
        if action == "click_at":
            return _tool_response("click_at", {
                "x": body.get("x", 0), "y": body.get("y", 0),
                "button": body.get("button", "left"),
                "double": body.get("double", False),
            })
        if action == "type":
            return _tool_response("type_text", {"text": body.get("value", "")})
        if action == "key":
            return _tool_response("press_key", {"keys": body.get("value", "")})
        if action == "scroll":
            return _tool_response("scroll", body)
        return jsonify({"success": False, "ok": False,
                        "error": f"Unknown action: {action}"}), 400

    # ── P1: identity, capabilities, element-targeted actions ─────────────────

    @app.route("/api/capabilities")
    def api_capabilities():
        return _tool_response("get_capabilities", {})

    @app.route("/api/monitors")
    def api_monitors():
        return _tool_response("get_monitors", {})

    @app.route("/api/find_element")
    def api_find_element():
        return _tool_response("find_element", _merge_query())

    @app.route("/api/element/click", methods=["POST"])
    def api_element_click():
        return _tool_response("click_element", request.get_json(force=True) or {})

    @app.route("/api/element/focus", methods=["POST"])
    def api_element_focus():
        return _tool_response("focus_element", request.get_json(force=True) or {})

    @app.route("/api/element/set_value", methods=["POST"])
    def api_element_set_value():
        return _tool_response("set_value", request.get_json(force=True) or {})

    @app.route("/api/element/invoke", methods=["POST"])
    def api_element_invoke():
        return _tool_response("invoke_element", request.get_json(force=True) or {})

    @app.route("/api/element/select", methods=["POST"])
    def api_element_select():
        return _tool_response("select_option", request.get_json(force=True) or {})

    # ── P2: observe-with-diff, snapshots, wait_for ──────────────────────────

    @app.route("/api/observe")
    def api_observe():
        return _tool_response("observe_window", _merge_query())

    @app.route("/api/snapshot", methods=["POST"])
    def api_snapshot():
        return _tool_response("snapshot", request.get_json(silent=True) or {})

    @app.route("/api/snapshot/<sid>")
    def api_snapshot_get(sid: str):
        return _tool_response("snapshot_get", {"snapshot_id": sid})

    @app.route("/api/snapshot/diff", methods=["POST"])
    def api_snapshot_diff():
        return _tool_response("snapshot_diff", request.get_json(force=True) or {})

    @app.route("/api/snapshot/<sid>", methods=["DELETE"])
    def api_snapshot_drop(sid: str):
        return _tool_response("snapshot_drop", {"snapshot_id": sid})

    @app.route("/api/wait_for", methods=["POST"])
    def api_wait_for():
        return _tool_response("wait_for", request.get_json(force=True) or {})

    @app.route("/api/wait_idle", methods=["POST"])
    def api_wait_idle():
        return _tool_response("wait_idle", request.get_json(force=True) or {})

    @app.route("/api/element/click_and_observe", methods=["POST"])
    def api_element_click_observe():
        return _tool_response("click_element_and_observe",
                               request.get_json(force=True) or {})

    @app.route("/api/type_and_observe", methods=["POST"])
    def api_type_observe():
        return _tool_response("type_and_observe", request.get_json(force=True) or {})

    @app.route("/api/key_and_observe", methods=["POST"])
    def api_key_observe():
        return _tool_response("press_key_and_observe", request.get_json(force=True) or {})

    # ── P3: filtering, cropping, region OCR, budgeted description ───────────

    @app.route("/api/screenshot/cropped")
    def api_screenshot_cropped():
        return _tool_response("get_screenshot_cropped", _merge_query())

    @app.route("/api/ocr")
    def api_ocr():
        return _tool_response("get_ocr", _merge_query())

    # ── P4: tracing, replay, scenarios, oracles ─────────────────────────────

    @app.route("/api/trace/start", methods=["POST"])
    def api_trace_start():
        return _tool_response("trace_start", request.get_json(silent=True) or {})

    @app.route("/api/trace/stop", methods=["POST"])
    def api_trace_stop():
        return _tool_response("trace_stop", {})

    @app.route("/api/trace/status")
    def api_trace_status():
        return _tool_response("trace_status", {})

    @app.route("/api/replay/start", methods=["POST"])
    def api_replay_start():
        return _tool_response("replay_start", request.get_json(force=True) or {})

    @app.route("/api/replay/step", methods=["POST"])
    def api_replay_step():
        return _tool_response("replay_step", request.get_json(force=True) or {})

    @app.route("/api/replay/status", methods=["POST"])
    def api_replay_status():
        return _tool_response("replay_status", request.get_json(force=True) or {})

    @app.route("/api/replay/stop", methods=["POST"])
    def api_replay_stop():
        return _tool_response("replay_stop", request.get_json(force=True) or {})

    @app.route("/api/scenario/load", methods=["POST"])
    def api_scenario_load():
        return _tool_response("load_scenario", request.get_json(force=True) or {})

    @app.route("/api/assert_state", methods=["POST"])
    def api_assert_state():
        return _tool_response("assert_state", request.get_json(force=True) or {})

    # ── P5: budgets / redaction status / propose ────────────────────────────

    @app.route("/api/budget_status")
    def api_budget_status():
        return _tool_response("get_budget_status", {})

    @app.route("/api/redaction_status")
    def api_redaction_status():
        return _tool_response("get_redaction_status", {})

    @app.route("/api/propose_action", methods=["POST"])
    def api_propose():
        return _tool_response("propose_action", request.get_json(force=True) or {})

    # ── P6: extra input verbs ────────────────────────────────────────────────

    @app.route("/api/hover", methods=["POST"])
    def api_hover():
        body = request.get_json(force=True) or {}
        if "x" in body and "y" in body and not body.get("selector") and not body.get("element_id"):
            return _tool_response("hover_at", body)
        return _tool_response("hover_element", body)

    @app.route("/api/element/right_click", methods=["POST"])
    def api_right_click():
        return _tool_response("right_click_element", request.get_json(force=True) or {})

    @app.route("/api/element/double_click", methods=["POST"])
    def api_double_click():
        return _tool_response("double_click_element", request.get_json(force=True) or {})

    @app.route("/api/drag", methods=["POST"])
    def api_drag():
        return _tool_response("drag", request.get_json(force=True) or {})

    @app.route("/api/element/key", methods=["POST"])
    def api_key_into():
        return _tool_response("key_into_element", request.get_json(force=True) or {})

    @app.route("/api/element/clear_text", methods=["POST"])
    def api_clear_text():
        return _tool_response("clear_text", request.get_json(force=True) or {})

    # ── Telemetry: metrics in Prometheus format ─────────────────────────────

    @app.route("/api/metrics")
    def api_metrics():
        from session import get_session
        s = get_session()
        lines = [
            "# HELP oso_step_count Total tool calls processed",
            "# TYPE oso_step_count counter",
            f"oso_step_count {s.steps.count}",
            "# HELP oso_uptime_seconds Process uptime",
            "# TYPE oso_uptime_seconds gauge",
            f"oso_uptime_seconds {int(s.steps.uptime_s)}",
        ]
        if s.budgets is not None:
            st = s.budgets.status()
            lines += [
                "# TYPE oso_actions_used counter",
                f"oso_actions_used {st['actions']['used']}",
                "# TYPE oso_screenshots_used counter",
                f"oso_screenshots_used {st['screenshots']['used']}",
            ]
        if s.active_trace is not None:
            lines.append("oso_active_trace 1")
        else:
            lines.append("oso_active_trace 0")
        body = "\n".join(lines) + "\n"
        return body, 200, {"Content-Type": "text/plain; version=0.0.4"}

    @app.route("/api/healthz")
    def api_healthz():
        from session import get_session
        s = get_session()
        return jsonify({
            "ok": True,
            "uptime_s": int(s.steps.uptime_s),
            "step_count": s.steps.count,
            "adapter": type(observer._adapter).__name__,
            "version": (config.get("mcp", {}) or {}).get("version", "0.2.0"),
        })

    return app
