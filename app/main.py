"""Application entrypoint.

Creates the FastAPI app, wires the API router, initialises the database on
start-up and serves a self-contained single-page dashboard at ``GET /``
(HTML, CSS and JavaScript are embedded below, no build step or CDN required).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse

from app.api.routes.files import router as files_router
from app.db.session import init_db

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Geospatial Measurement Console</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --primary: #526B5D;
    --primary-light: #7A9084;
    --primary-dark: #3B4F43;
    --bg-light: #FBF9F5;
    --bg-white: #FFFFFF;
    --bg-active: #F2F6F1;
    --text-dark: #222222;
    --text-gray: #4A4A4A;
    --border: #222222;
    --success-light: #E8F0EA;
    --warning: #C18C4D;
    --warning-light: #FDF3E5;
    --danger: #BA5A52;
    --danger-light: #F9EBEA;
    --ease: cubic-bezier(.16, 1, .3, 1);
    --radius: 20px;
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; scroll-padding-top: 24px; }
  body {
    margin: 0;
    min-height: 100vh;
    color: var(--text-dark);
    font-family: "Plus Jakarta Sans", "Segoe UI", system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg-light);
    line-height: 1.6;
    letter-spacing: -0.01em;
  }
  .orb { display: none; }
  main, header { position: relative; z-index: 1; }
  .wrap { width: min(1080px, 92vw); margin: 0 auto; }

  /* ---------- scroll-to-reveal ---------- */
  .reveal {
    opacity: 0;
    transform: translateY(20px);
    transition: opacity 0.55s var(--ease), transform 0.55s var(--ease);
  }
  .reveal.visible { opacity: 1; transform: none; }

  @media (prefers-reduced-motion: reduce) {
    .reveal { opacity: 1; transform: none; transition: none; }
    html { scroll-behavior: auto; }
    *, *::before, *::after { scroll-behavior: auto !important; animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
  }

  /* ---------- layout ---------- */
  .hero { padding: 72px 0 34px; text-align: center; }
  .hero .wrap { padding: clamp(28px, 5vw, 54px); background: var(--bg-white); border: 1px solid var(--border);
                border-radius: 24px; box-shadow: 4px 4px 0 rgba(34,34,34,.08); }
  .eyebrow { display: inline-block; padding: 6px 14px; border: 1px solid var(--border); border-radius: 20px;
             background: var(--bg-active); color: var(--primary-dark); font-size: 0.76rem; font-weight: 700;
             letter-spacing: 0.1em; text-transform: uppercase; }
  h1 { font-size: clamp(2.4rem, 5vw, 4rem); font-weight: 800; letter-spacing: -0.03em;
       margin: 18px 0 12px; line-height: 1.1; color: var(--text-dark); }
  .lead { max-width: 680px; margin: 0 auto; color: var(--text-gray); font-size: 1.02rem; }
  section { margin: 42px 0; scroll-margin-top: 24px; }
  h2 { font-size: clamp(1.45rem, 3vw, 2rem); font-weight: 800; letter-spacing: -0.02em;
       line-height: 1.2; margin: 0 0 16px; color: var(--text-dark); }
  .card { background: var(--bg-white); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px;
          box-shadow: 4px 4px 0 rgba(34,34,34,.08); }
  .hidden { display: none !important; }

  /* ---------- dropzone ---------- */
  .dropzone { position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center;
              gap: 10px; min-height: 230px; padding: 28px; text-align: center; cursor: pointer; outline: none;
              border: 2px dashed var(--primary-light); border-radius: var(--radius);
              background: var(--bg-active);
              transition: all .2s var(--ease); }
  .dropzone:hover, .dropzone:focus-visible { border-color: var(--primary); background: var(--success-light);
                                              transform: translateY(-2px); box-shadow: 4px 4px 0 rgba(34,34,34,.1); }
  .dropzone.dragover { border-style: solid; border-color: var(--primary-dark); background: var(--success-light);
                       transform: translateY(-2px); box-shadow: 4px 4px 0 rgba(34,34,34,.12); }
  .dropzone.busy { border-color: var(--primary-light); cursor: progress; }
  .dropzone.success { border-color: var(--primary); }
  .dropzone.error { border-color: var(--danger); }
  .dz-icon { font-size: 2.6rem; }
  .dz-title { font-size: 1.15rem; font-weight: 700; }
  .dz-hint { color: var(--text-gray); font-size: 0.92rem; }
  .dz-status { min-height: 1.4em; font-size: 0.95rem; }
  .dz-status.ok { color: var(--primary-dark); } .dz-status.err { color: var(--danger); } .dz-status.busy { color: var(--primary); }
  .spinner { width: 22px; height: 22px; border-radius: 50%; border: 3px solid rgba(82,107,93,.2);
             border-top-color: var(--primary); animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  button { font: inherit; color: var(--text-dark); background: var(--bg-white); font-weight: 700;
           border: 1px solid var(--border); border-radius: 16px; padding: 10px 18px; cursor: pointer;
           box-shadow: 3px 3px 0 rgba(34,34,34,.08); transition: all .2s var(--ease); }
  button:hover { background: var(--bg-active); transform: translate(-2px, -2px); box-shadow: 5px 5px 0 rgba(34,34,34,.1); }
  button:active { transform: translate(1px, 1px); box-shadow: 1px 1px 0 rgba(34,34,34,.08); }
  button.ghost { background: var(--bg-white); color: var(--text-dark); border: 1px solid var(--border); }
  button:focus-visible, a:focus-visible { outline: 3px solid var(--primary-light); outline-offset: 3px; }

  /* ---------- results ---------- */
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; }
  .stat { position: relative; overflow: hidden; background: var(--bg-white); border: 1px solid var(--border);
          border-radius: 16px; padding: 18px 16px 16px; box-shadow: 3px 3px 0 rgba(34,34,34,.06);
          transition: all .2s var(--ease); }
  .stat::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 5px; background: var(--primary); }
  .stat:hover { transform: translateY(-2px); box-shadow: 5px 5px 0 rgba(34,34,34,.1); }
  .stat .k { color: var(--text-gray); font-size: 0.76rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }
  .stat .v { font-size: 1.4rem; font-weight: 800; margin-top: 4px; word-break: break-word; line-height: 1.35; }
  .stat .u { color: var(--text-gray); font-size: 0.85rem; font-weight: 500; }
  .notes { margin-top: 16px; display: grid; gap: 8px; }
  .note { padding: 10px 14px; border-radius: 12px; border: 1px solid var(--warning); background: var(--warning-light);
          color: #65451F; font-size: 0.9rem; }
  .table-wrap { overflow-x: auto; border-radius: 14px; border: 1px solid var(--border); background: var(--bg-white); }
  table { width: 100%; border-collapse: collapse; font-size: 0.92rem; min-width: 720px; }
  th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid rgba(34,34,34,.12); vertical-align: top; }
  th { position: sticky; top: 0; background: var(--bg-active); color: var(--text-gray); font-weight: 700;
       font-size: 0.78rem; letter-spacing: 0.08em; text-transform: uppercase; }
  tbody tr:hover { background: var(--bg-active); }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .badge { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 0.75rem;
           font-weight: 700; border: 1px solid rgba(34,34,34,.22); background: var(--bg-active); }
  .badge.MEASURED { color: var(--primary-dark); border-color: var(--primary-light); background: var(--success-light); }
  .badge.NOT_REQUIRED { color: var(--text-gray); }
  .badge.UNSUPPORTED { color: #65451F; border-color: var(--warning); background: var(--warning-light); }
  .badge.INVALID { color: var(--danger); border-color: var(--danger); background: var(--danger-light); }
  .caption { color: var(--text-gray); font-size: 0.85rem; margin-top: 10px; }

  /* ---------- console ---------- */
  details.console > summary { cursor: pointer; list-style: none; display: flex; align-items: center; justify-content: space-between;
                              font-size: 1.35rem; font-weight: 800; }
  details.console > summary::-webkit-details-marker { display: none; }
  details.console > summary::after { content: "▾"; color: var(--primary); transition: transform 0.2s var(--ease); }
  details.console:not([open]) > summary::after { transform: rotate(-90deg); }
  .console-bar { display: flex; gap: 10px; align-items: center; margin: 14px 0 10px; flex-wrap: wrap; }
  .pill { padding: 4px 12px; border-radius: 20px; background: var(--bg-active); border: 1px solid var(--border);
          font-size: 0.8rem; font-weight: 600; color: var(--text-gray); }
  #console-log { max-height: 420px; overflow: auto; display: grid; gap: 8px; padding-right: 4px;
                 font-family: "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.82rem; }
  .log { border: 1px solid var(--border); border-radius: 12px; background: var(--bg-white); overflow: hidden; }
  .log > summary { cursor: pointer; padding: 9px 12px; color: var(--text-dark); }
  .log.request > summary { color: var(--primary-dark); } .log.ok > summary { color: var(--primary); }
  .log.error > summary { color: var(--danger); } .log.info > summary { color: var(--text-gray); }
  .log pre { margin: 0; padding: 12px; max-height: 300px; overflow: auto;
             border-top: 1px solid rgba(34,34,34,.15); white-space: pre-wrap; word-break: break-word;
             color: var(--text-dark); background: var(--bg-active); }
  .empty-log { color: var(--text-gray); padding: 10px 4px; }

  /* ---------- api reference ---------- */
  .endpoint { display: flex; gap: 12px; align-items: baseline; padding: 12px 0;
              border-bottom: 1px solid rgba(34,34,34,.15); flex-wrap: wrap; }
  .endpoint:last-child { border-bottom: 0; }
  .method { font-weight: 700; font-size: 0.78rem; padding: 3px 10px; border-radius: 8px;
            background: var(--bg-active); color: var(--primary-dark); border: 1px solid var(--primary-light); }
  .method.get { background: var(--success-light); color: var(--primary-dark); }
  code { font-family: "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         color: var(--primary-dark); font-size: .92em; }
  footer { color: var(--text-gray); font-size: 0.92rem; padding: 34px 0 48px; }
  .developer-card { padding: clamp(22px, 4vw, 38px); background: var(--bg-white); border: 1px solid var(--border);
                    border-radius: 24px; box-shadow: 4px 4px 0 rgba(34,34,34,.08); }
  .developer-kicker { display: inline-block; margin-bottom: 10px; color: var(--primary-dark); font-size: .76rem;
                      font-weight: 800; letter-spacing: .1em; text-transform: uppercase; }
  .developer-card h2 { margin-bottom: 22px; }
  .developer-grid { display: grid; grid-template-columns: minmax(0, .9fr) minmax(0, 1.1fr); gap: clamp(24px, 5vw, 56px); }
  .developer-grid h3 { margin: 0 0 8px; color: var(--text-dark); font-size: 1.2rem; font-weight: 800; }
  .developer-role { margin: 0 0 12px; color: var(--primary-dark); font-weight: 700; }
  .developer-grid p { margin: 0 0 12px; }
  .developer-project { padding: 16px; border: 1px solid rgba(34,34,34,.18); border-radius: 16px;
                       background: var(--bg-active); }
  .developer-links { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
  .developer-link { display: inline-flex; align-items: center; justify-content: center; padding: 9px 14px;
                    border: 1px solid var(--border); border-radius: 14px; background: var(--bg-white);
                    box-shadow: 2px 2px 0 rgba(34,34,34,.08); color: var(--primary-dark);
                    font-weight: 700; text-decoration: none; transition: all .2s var(--ease); }
  .developer-link:hover { transform: translate(-1px, -1px); background: var(--bg-active);
                          box-shadow: 3px 3px 0 rgba(34,34,34,.1); }
  .developer-link:active { transform: translate(1px, 1px); box-shadow: 1px 1px 0 rgba(34,34,34,.08); }
  .developer-link.portfolio-link.is-activated,
  .developer-link.portfolio-link:visited { color: #2563EB; }
  .footer-stack { margin: 22px 0 0; padding-top: 14px; border-top: 1px solid rgba(34,34,34,.15);
                  color: var(--text-gray); font-size: .82rem; text-align: center; }
  a { color: var(--primary-dark); font-weight: 700; text-underline-offset: 3px; }
  a:hover { color: var(--primary); }

  /* ---------- scrollbar ---------- */
  * { scrollbar-width: thin; scrollbar-color: #AEB7B0 var(--bg-light); }
  *::-webkit-scrollbar { width: 8px; height: 8px; }
  *::-webkit-scrollbar-track { background: var(--bg-light); }
  *::-webkit-scrollbar-thumb { background: #AEB7B0; border-radius: 8px; }
  *::-webkit-scrollbar-thumb:hover { background: var(--primary-light); }

  @media (max-width: 640px) {
    .wrap { width: min(100% - 28px, 1080px); }
    .hero { padding: 24px 0 16px; }
    .hero .wrap { padding: 28px 20px; }
    section { margin: 30px 0; }
    .card { padding: 18px; }
    .dropzone { min-height: 205px; padding: 22px 16px; }
    .stats { grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 10px; }
    .stat { padding: 16px 12px 12px; }
    .stat .v { font-size: 1.2rem; }
    .console-bar { gap: 8px; }
    .console-bar button { padding: 8px 11px; font-size: .88rem; }
    .endpoint { align-items: flex-start; gap: 8px; }
    .endpoint > span:last-child { flex-basis: 100%; }
    .developer-grid { grid-template-columns: 1fr; gap: 20px; }
    .developer-links { flex-direction: column; align-items: stretch; }
    .developer-link { text-align: center; }
  }
</style>
</head>
<body>
<div class="orb a"></div><div class="orb b"></div>

<header class="hero">
  <div class="wrap reveal">
    <span class="eyebrow">Geospatial File Measurement API</span>
    <h1>Measure any Shapefile or KML in true metres</h1>
    <p class="lead">Upload a zipped Shapefile or a KML. Geometries are reprojected into the best-fit local UTM zone
      before measuring, so every area is in square metres and every length is in metres, never in degrees.</p>
  </div>
</header>

<main class="wrap">
  <section id="upload" class="reveal">
    <h2>1. Ingest a file</h2>
    <div id="dropzone" class="dropzone" role="button" tabindex="0" aria-label="Drop a geospatial file here or press Enter to browse">
      <div class="dz-icon" aria-hidden="true">🛰️</div>
      <div class="dz-title">Drag &amp; drop a <code>.zip</code> (Shapefile) or <code>.kml</code></div>
      <div class="dz-hint">or click to browse &middot; processed instantly, nothing leaves this server</div>
      <div id="dz-status" class="dz-status" aria-live="polite"></div>
      <input id="file-input" type="file" accept=".zip,.kml" class="hidden">
    </div>
  </section>

  <section id="results" class="reveal hidden">
    <h2>2. File summary</h2>
    <div class="card">
      <div id="stats" class="stats"></div>
      <div id="notes" class="notes"></div>
    </div>
  </section>

  <section id="measurements" class="reveal hidden">
    <h2>3. Feature measurements</h2>
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Geometry</th><th>Name / properties</th><th class="num">Area (m²)</th><th class="num">Length (m)</th><th>Status</th></tr></thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
      <div id="table-caption" class="caption"></div>
    </div>
  </section>

  <section id="console-section" class="reveal">
    <div class="card">
      <details class="console" id="console-panel" open>
        <summary>Live metrics console</summary>
        <div class="console-bar">
          <span class="pill" id="log-count">0 events</span>
          <button type="button" class="ghost" id="expand-all">Expand all</button>
          <button type="button" class="ghost" id="collapse-all">Collapse all</button>
          <button type="button" class="ghost" id="clear-log">Clear</button>
        </div>
        <div id="console-log"><div class="empty-log">Requests and JSON responses will appear here.</div></div>
      </details>
    </div>
  </section>

  <section id="api" class="reveal">
    <h2>API reference</h2>
    <div class="card">
      <div class="endpoint"><span class="method">POST</span><code>/api/files/</code><span>Upload and process a file (multipart field <code>file</code>)</span></div>
      <div class="endpoint"><span class="method get">GET</span><code>/api/files/{id}/</code><span>Processing state, feature count and CRS</span></div>
      <div class="endpoint"><span class="method get">GET</span><code>/api/files/{id}/measurements/</code><span>Per-feature geometry, properties and metrics</span></div>
      <div class="endpoint"><span class="method get">GET</span><code><a href="/docs">/docs</a></code><span>Interactive OpenAPI documentation</span></div>
    </div>
  </section>
</main>

<footer class="wrap reveal" id="meet-the-developer">
  <div class="developer-card">
    <span class="developer-kicker">The people and methods behind this dashboard</span>
    <h2>Meet the developer</h2>
    <div class="developer-grid">
      <section aria-labelledby="developer-name">
        <h3 id="developer-name">Yeshwanth J</h3>
        <p class="developer-role">AI/ML &amp; Full Stack Engineer</p>
        <p>Master of Computer Applications graduate (2026), with a 9.19 CGPA. Deeply focused on the intersection of artificial intelligence and robust web architecture.</p>
        <p>Other than this project, I also built Resolve.AI, demonstrating end-to-end LLM pipelines, including messy Kaggle CSV extraction with Pandas, manual Golden Dataset construction to avoid data leakage, strict JSON parsing, deterministic rule fallbacks, ChromaDB RAG retrieval, and LLM-as-a-Judge evaluation with Scikit-Learn metrics.</p>
        <div class="developer-links">
          <a class="developer-link portfolio-link" href="https://yesh-portfolio-dev.vercel.app/" target="_blank" rel="noopener noreferrer">Yesh - Personal Portfolio</a>
          <a class="developer-link" href="https://github.com/Yesh6957" target="_blank" rel="noopener noreferrer">View Full GitHub Repository</a>
        </div>
      </section>
      <section aria-labelledby="project-methods">
        <h3 id="project-methods">What this project analyzes</h3>
        <div class="developer-project">
          <p>This geospatial measurement API accepts zipped Shapefiles and KML files, selects a suitable local projected coordinate system, and reports polygon areas and line lengths in metric units.</p>
          <p>Its workflow combines validated file ingestion, coordinate reference system handling, geospatial measurement, and an interactive dashboard for reviewing results.</p>
        </div>
      </section>
    </div>
    <p class="footer-stack">FastAPI &middot; GeoPandas &middot; Shapely &middot; PyProj &middot; SQLite</p>
  </div>
</footer>

<script>
(function () {
  "use strict";

  var portfolioLink = document.querySelector(".portfolio-link");
  portfolioLink.addEventListener("click", function () {
    portfolioLink.classList.add("is-activated");
  });

  /* ---------- scroll-to-reveal (IntersectionObserver) ---------- */
  var revealItems = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window) {
    var observer = new IntersectionObserver(function (entries, obs) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("visible");
          obs.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
    revealItems.forEach(function (el) { observer.observe(el); });
  } else {
    revealItems.forEach(function (el) { el.classList.add("visible"); });
  }

  /* ---------- helpers ---------- */
  function $(selector) { return document.querySelector(selector); }
  var dropzone = $("#dropzone"), fileInput = $("#file-input"), statusEl = $("#dz-status");
  var logEl = $("#console-log"), logCount = $("#log-count"), eventTotal = 0;

  function fmt(value, digits) {
    if (value === null || value === undefined) { return "—"; }
    return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits === undefined ? 2 : digits });
  }

  function setState(state, message) {
    dropzone.classList.remove("busy", "success", "error", "dragover");
    statusEl.className = "dz-status";
    statusEl.textContent = "";
    if (state === "busy") {
      dropzone.classList.add("busy");
      statusEl.classList.add("busy");
      var spin = document.createElement("span");
      spin.className = "spinner";
      statusEl.appendChild(spin);
      statusEl.appendChild(document.createTextNode(" " + message));
    } else if (state === "success") {
      dropzone.classList.add("success"); statusEl.classList.add("ok"); statusEl.textContent = message;
    } else if (state === "error") {
      dropzone.classList.add("error"); statusEl.classList.add("err"); statusEl.textContent = message;
    }
  }

  /* ---------- live console ---------- */
  function log(kind, title, payload) {
    var empty = logEl.querySelector(".empty-log");
    if (empty) { empty.remove(); }
    var entry = document.createElement("details");
    entry.className = "log " + kind;
    var summary = document.createElement("summary");
    summary.textContent = "[" + new Date().toLocaleTimeString() + "] " + title;
    var pre = document.createElement("pre");
    pre.textContent = typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
    entry.appendChild(summary);
    entry.appendChild(pre);
    if (kind === "ok" || kind === "error") { entry.open = true; }
    logEl.insertBefore(entry, logEl.firstChild);
    eventTotal += 1;
    logCount.textContent = eventTotal + (eventTotal === 1 ? " event" : " events");
  }

  $("#expand-all").addEventListener("click", function () {
    logEl.querySelectorAll("details").forEach(function (d) { d.open = true; });
  });
  $("#collapse-all").addEventListener("click", function () {
    logEl.querySelectorAll("details").forEach(function (d) { d.open = false; });
  });
  $("#clear-log").addEventListener("click", function () {
    logEl.innerHTML = '<div class="empty-log">Requests and JSON responses will appear here.</div>';
    eventTotal = 0; logCount.textContent = "0 events";
  });

  /* ---------- API calls ---------- */
  function describeError(body, status) {
    if (body && typeof body === "object" && body.detail !== undefined) {
      var d = body.detail;
      if (typeof d === "string") { return d; }
      if (d && typeof d === "object" && d.message) { return d.message; }
      return JSON.stringify(d);
    }
    return "Request failed with status " + status;
  }

  async function request(method, url, options) {
    log("request", method + " " + url, options && options.body ? "multipart/form-data upload" : "(no body)");
    var started = performance.now();
    var response = await fetch(url, Object.assign({ method: method }, options || {}));
    var body = null;
    try { body = await response.json(); } catch (err) { body = { detail: "Response was not JSON." }; }
    var ms = Math.round(performance.now() - started);
    log(response.ok ? "ok" : "error", response.status + " " + method + " " + url + " (" + ms + " ms)", body);
    return { ok: response.ok, status: response.status, body: body };
  }

  /* ---------- rendering ---------- */
  function addStat(container, label, value, unit) {
    var box = document.createElement("div"); box.className = "stat";
    var k = document.createElement("div"); k.className = "k"; k.textContent = label;
    var v = document.createElement("div"); v.className = "v"; v.textContent = value;
    if (unit) {
      var u = document.createElement("span"); u.className = "u"; u.textContent = " " + unit; v.appendChild(u);
    }
    box.appendChild(k); box.appendChild(v); container.appendChild(box);
  }

  function renderSummary(info, data) {
    var stats = $("#stats"), notes = $("#notes");
    stats.innerHTML = ""; notes.innerHTML = "";
    var summary = data.summary || {}, projection = data.projection || {};
    addStat(stats, "File", info.filename);
    addStat(stats, "Status", info.status);
    addStat(stats, "Features", fmt(info.feature_count, 0));
    addStat(stats, "Native CRS", info.crs || "unknown");
    addStat(stats, "Measured in", projection.projected_crs ? projection.projected_crs + " (UTM " + projection.utm_zone + projection.hemisphere + ")" : "—");
    addStat(stats, "Total area", fmt(summary.total_area_m2, 2), "m²");
    addStat(stats, "Total length", fmt(summary.total_length_m, 2), "m");
    addStat(stats, "Polygons / Lines / Points", [summary.polygon_count, summary.line_count, summary.point_count].map(function (n) { return fmt(n, 0); }).join(" / "));
    if (summary.unsupported_count || summary.invalid_count) {
      addStat(stats, "Skipped", fmt(summary.unsupported_count, 0) + " unsupported, " + fmt(summary.invalid_count, 0) + " invalid");
    }
    (info.warnings || []).concat(projection.warnings || []).forEach(function (text) {
      var n = document.createElement("div"); n.className = "note"; n.textContent = text; notes.appendChild(n);
    });
  }

  function previewProperties(properties) {
    var keys = Object.keys(properties || {});
    if (!keys.length) { return "—"; }
    var parts = keys.slice(0, 3).map(function (key) {
      var value = properties[key];
      return key + ": " + (value === null || value === undefined ? "null" : String(value).slice(0, 40));
    });
    if (keys.length > 3) { parts.push("+" + (keys.length - 3) + " more"); }
    return parts.join(" · ");
  }

  function renderRows(data) {
    var body = $("#rows"); body.innerHTML = "";
    data.features.forEach(function (feature) {
      var tr = document.createElement("tr");
      function cell(text, cls) {
        var td = document.createElement("td"); if (cls) { td.className = cls; } td.textContent = text; tr.appendChild(td); return td;
      }
      cell(String(feature.feature_index));
      cell(feature.geometry_type || "—");
      cell(previewProperties(feature.properties));
      cell(fmt(feature.measurement.area_m2, 2), "num");
      cell(fmt(feature.measurement.length_m, 2), "num");
      var statusCell = document.createElement("td");
      var badge = document.createElement("span");
      badge.className = "badge " + feature.measurement.status;
      badge.textContent = feature.measurement.status.replace("_", " ");
      if (feature.measurement.note) { badge.title = feature.measurement.note; }
      statusCell.appendChild(badge); tr.appendChild(statusCell);
      body.appendChild(tr);
    });
    var shown = data.features.length;
    $("#table-caption").textContent = "Showing " + fmt(shown, 0) + " of " + fmt(data.total_features, 0) +
      " features. Hover a status badge for details. Full data: GET /api/files/" + data.file_id + "/measurements/";
  }

  /* ---------- upload flow ---------- */
  var busy = false;

  async function handleFile(file) {
    if (!file || busy) { return; }
    var extension = file.name.split(".").pop().toLowerCase();
    if (extension !== "zip" && extension !== "kml") {
      var message = "Unsupported file type. Choose a .zip (Shapefile) or .kml file.";
      setState("error", message); log("error", "Rejected " + file.name, message); return;
    }
    busy = true;
    setState("busy", "Uploading and measuring " + file.name + "…");
    try {
      var form = new FormData(); form.append("file", file);
      var upload = await request("POST", "/api/files/", { body: form });
      if (!upload.ok) { setState("error", describeError(upload.body, upload.status)); return; }

      var id = upload.body.id;
      var info = await request("GET", "/api/files/" + id + "/");
      var measurements = await request("GET", "/api/files/" + id + "/measurements/?limit=500&include_geometry=false");
      if (!info.ok || !measurements.ok) {
        setState("error", describeError(measurements.body, measurements.status)); return;
      }
      renderSummary(info.body, measurements.body);
      renderRows(measurements.body);
      $("#results").classList.remove("hidden");
      $("#measurements").classList.remove("hidden");
      setState("success", "Done: " + fmt(info.body.feature_count, 0) + " features measured.");
      $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      setState("error", "Network error: " + error.message);
      log("error", "Network error", String(error));
    } finally {
      busy = false;
      fileInput.value = "";
    }
  }

  /* ---------- dropzone events ---------- */
  dropzone.addEventListener("click", function () { if (!busy) { fileInput.click(); } });
  dropzone.addEventListener("keydown", function (event) {
    if ((event.key === "Enter" || event.key === " ") && !busy) { event.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener("change", function () { handleFile(fileInput.files[0]); });
  ["dragenter", "dragover"].forEach(function (name) {
    dropzone.addEventListener(name, function (event) {
      event.preventDefault(); if (!busy) { dropzone.classList.add("dragover"); }
    });
  });
  ["dragleave", "drop"].forEach(function (name) {
    dropzone.addEventListener(name, function (event) {
      event.preventDefault(); dropzone.classList.remove("dragover");
    });
  });
  dropzone.addEventListener("drop", function (event) {
    var files = event.dataTransfer && event.dataTransfer.files;
    if (files && files.length) { handleFile(files[0]); }
  });
  window.addEventListener("dragover", function (event) { event.preventDefault(); });
  window.addEventListener("drop", function (event) { event.preventDefault(); });

  log("info", "Console ready", "Waiting for a file upload.");
})();
</script>
</body>
</html>
"""


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Create database tables on start-up."""
    init_db()
    yield


app = FastAPI(
    title="Geospatial File Measurement API",
    description=(
        "Upload a zipped Shapefile or a KML file, extract its features and get true metric "
        "measurements. Geometries are reprojected to the best-fit UTM zone before measuring."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(files_router)


@app.exception_handler(RequestValidationError)
async def explain_missing_upload_field(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Explain the multipart field required by the upload endpoint."""
    missing_file = any(
        error.get("loc") == ("body", "file") and error.get("type") == "missing"
        for error in exc.errors()
    )
    if request.method == "POST" and request.url.path == "/api/files/" and missing_file:
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "message": (
                        "Missing required multipart field 'file'. Send the upload as "
                        "multipart/form-data with the field name 'file'."
                    ),
                    "errors": exc.errors(),
                }
            },
        )
    return await request_validation_exception_handler(request, exc)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    """Interactive single-page dashboard."""
    return HTMLResponse(INDEX_HTML)


@app.get("/health", tags=["meta"], summary="Liveness probe")
def health() -> dict[str, str]:
    return {"status": "ok"}
