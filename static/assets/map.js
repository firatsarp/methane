/* Harita sayfası. Kapsam yalnızca Türkiye: kamera sınırı, ülke dışı maske, sunucuda scope=tr kayıtları.
   Harita, liste, sayaç ve CSV aynı sunucu filtresinden gelen kayıt kümesini kullanır. */
(async function () {
  "use strict";
  const F = window.Fmt;
  const { esc, fmtNum, fmtLocal, fmtUtc, fmtShort, fmtDay, getJSON, latestOnly } = F;
  const $ = (id) => document.getElementById(id);

  const CONTENT_URL = "/content/metan-icerik.2026-11-01-general-3.json";
  const MAP_STATE_KEY = "metan.mapState";
  const PLASMA = ["#0d0887", "#5c01a6", "#9c179e", "#cc4778", "#ed7953", "#fdb42f", "#f0f921"];
  const DIVERGING = ["#2166ac", "#67a9cf", "#d1e5f0", "#f7f7f7", "#fddbc7", "#ef8a62", "#b2182b"];
  const SAT_COLORS = { s5p: "#1d6b3e", iss: "#2f5f93" };
  const SECTORS = {
    "1A1": "Enerji (elektrik / ısı)", "1B1a": "Kömür madenciliği", "1B2": "Petrol ve gaz", "4B": "Hayvancılık (gübre)",
    "6A": "Katı atık (depolama sahası)", "6B": "Atık su", unknown: "Sektör belirtilmemiş",
  };
  const PRODUCT = { OFFL: "Çevrimdışı işlenmiş ürün (OFFL)", NRTI: "Hızlı işlenmiş ürün (NRTI)" };
  const BASEMAPS = {
    light: ["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
      "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}"],
    dark: ["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
      "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}"],
  };
  const MASK_COLOR = { light: "#e9edf1", dark: "#1d2733" };
  const PLUME_IMG_ZOOM = 9;
  const CLUSTER_MAX_ZOOM = 9;
  const PAGE = 40;
  const MOBILE = () => matchMedia("(max-width: 860px)").matches;
  const sectorName = (c) => SECTORS[c] ?? (c ? `IPCC ${c}` : SECTORS.unknown);

  // ---- Durum ve URL -----------------------------------------------------------------
  const readUrl = () => {
    const q = new URLSearchParams(location.search);
    return {
      sector: q.get("sector") || "", sensor: q.get("sensor") || "", days: q.get("days") || "",
      sort: q.get("sort") || "observed", em: q.get("em") === "1", sel: q.get("sel"), src: q.get("src"),
      tab: q.get("tab") === "src" ? "src" : "obs", day: q.get("day"), mode: q.get("mode") || "day",
      view: q.get("view") === "anom" ? "anom" : "abs", tropomi: q.get("tropomi") === "1",
      op: Number(q.get("op")) || 55, base: q.get("base") === "dark" ? "dark" : "light", sats: q.get("sats") === "1",
      results: q.get("results") !== "0", map: q.get("map"),
    };
  };
  const initial = readUrl();
  const state = {
    filters: { sector: initial.sector, sensor: initial.sensor, days: initial.days, em: initial.em },
    sort: ["observed", "published", "emission"].includes(initial.sort) ? initial.sort : "observed",
    tab: initial.tab, items: [], all: [], sources: [], facets: null, total: 0, shown: PAGE,
    selected: null, selectedSource: null, resultsOpen: initial.results,
    days: [], dayIdx: -1, mode: ["day", "7", "30", "90"].includes(initial.mode) ? initial.mode : "day", view: initial.view,
    data: null, passes: [], selPass: null, content: null, base: initial.base, scroll: 0,
  };

  function urlParams() {
    const q = new URLSearchParams();
    const f = state.filters;
    if (f.sector) q.set("sector", f.sector);
    if (f.sensor) q.set("sensor", f.sensor);
    if (f.days) q.set("days", f.days);
    if (f.em) q.set("em", "1");
    if (state.sort !== "observed") q.set("sort", state.sort);
    if (state.tab === "src") q.set("tab", "src");
    if (state.selected) q.set("sel", state.selected);
    if (state.selectedSource) q.set("src", state.selectedSource);
    if ($("show-tropomi").checked) {
      q.set("tropomi", "1");
      const day = state.days[state.dayIdx]?.day;
      if (day) q.set("day", day);
      if (state.mode !== "day") q.set("mode", state.mode);
      if (state.view !== "abs") q.set("view", state.view);
      q.set("op", $("t-opacity").value);
    }
    if (state.base !== "light") q.set("base", state.base);
    if ($("show-sats").checked) q.set("sats", "1");
    if (!state.resultsOpen) q.set("results", "0");
    if (mapReady) {
      const c = map.getCenter();
      q.set("map", `${map.getZoom().toFixed(2)}/${c.lat.toFixed(4)}/${c.lng.toFixed(4)}`);
    }
    return q.toString() ? `?${q}` : "";
  }
  function writeUrl(push = false) {
    const s = urlParams();
    if (s === location.search) return;
    history[push ? "pushState" : "replaceState"](null, "", location.pathname + s);
    try { sessionStorage.setItem(MAP_STATE_KEY, s); } catch (e) { /* oturum deposu kapalıysa yalnızca URL kullanılır */ }
  }

  // ---- Türkiye sınırı ve harita --------------------------------------------------------
  let geo;
  try { geo = await window.TurkeyGeo.load("/turkey.geojson"); }
  catch (e) { $("cards").innerHTML = `<p class="empty">Harita sınırı yüklenemedi: ${esc(e.message)}. Sayfayı yenileyin.</p>`; return; }

  let mapReady = false;
  function viewPadding() {
    if (MOBILE()) {
      const sheet = $("results").hidden ? 0 : $("results").getBoundingClientRect().height || 112;
      return { top: 70, left: 16, right: 16, bottom: Math.min(sheet, window.innerHeight * 0.5) + 16 };
    }
    const panel = !$("detail").hidden ? $("detail").offsetWidth : state.resultsOpen ? $("results").offsetWidth : 0;
    return { top: 84, left: window.innerWidth < 900 ? 24 : 72, right: panel ? panel + 32 : 24, bottom: 48 };
  }
  const startCam = (() => {
    const m = (initial.map || "").split("/").map(Number);
    if (m.length !== 3 || !m.every(Number.isFinite)) return null;
    // Eski kamera Türkiye kapsamı dışındaysa kullanılmaz; açılış ülke görünümüne döner
    const [[w, s], [e, n]] = geo.maxBounds;
    return m[2] >= w && m[2] <= e && m[1] >= s && m[1] <= n ? { zoom: m[0], center: [m[2], m[1]] } : null;
  })();

  const map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      sources: {
        "base-light": { type: "raster", tileSize: 256, maxzoom: 16, tiles: [BASEMAPS.light[0]], attribution: "Harita © Esri, HERE, Garmin, © OpenStreetMap katkıcıları" },
        "labels-light": { type: "raster", tileSize: 256, maxzoom: 16, tiles: [BASEMAPS.light[1]] },
        "base-dark": { type: "raster", tileSize: 256, maxzoom: 16, tiles: [BASEMAPS.dark[0]] },
        "labels-dark": { type: "raster", tileSize: 256, maxzoom: 16, tiles: [BASEMAPS.dark[1]] },
      },
      layers: [
        { id: "base-light", type: "raster", source: "base-light" },
        { id: "base-dark", type: "raster", source: "base-dark", layout: { visibility: "none" } },
      ],
    },
    // Tanımsız center/zoom anahtarı verilirse kamera NaN olur; yalnızca geçerli olan seçenek eklenir
    ...(startCam ? { center: startCam.center, zoom: startCam.zoom } : { bounds: geo.bounds, fitBoundsOptions: { padding: viewPadding() } }),
    maxBounds: geo.maxBounds,
    attributionControl: { compact: false },
    dragRotate: false, pitchWithRotate: false, touchPitch: false,
  });
  map.touchZoomRotate.disableRotation();

  // En fazla uzaklaşma: ülkenin tamamı görünür alana sığacak kadar; ekran/panel değişince yeniden hesaplanır
  function updateMinZoom() {
    const cam = map.cameraForBounds(geo.bounds, { padding: viewPadding() });
    if (cam) map.setMinZoom(Math.max(3, cam.zoom - 0.6));
  }
  function goHome(animate = true) {
    map.fitBounds(geo.bounds, { padding: viewPadding(), duration: animate ? 600 : 0 });
  }
  const empty = () => ({ type: "FeatureCollection", features: [] });
  function splitLine(coords) {
    const parts = [[]];
    coords.forEach((c, k) => {
      if (k && Math.abs(c[0] - coords[k - 1][0]) > 180) parts.push([]);
      parts[parts.length - 1].push(c);
    });
    return { type: "MultiLineString", coordinates: parts.filter((p) => p.length > 1) };
  }

  map.on("load", () => {
    for (const id of ["cells", "obs", "footprints", "sources", "pass-track", "sat-ahead", "selected"]) {
      map.addSource(id, id === "obs"
        ? { type: "geojson", data: empty(), cluster: true, clusterMaxZoom: CLUSTER_MAX_ZOOM, clusterRadius: 44 }
        : { type: "geojson", data: empty() });
    }
    map.addSource("mask", { type: "geojson", data: geo.mask });
    map.addSource("turkey", { type: "geojson", data: geo.outline });
    // Sıra (alttan üste): temel harita, ülke dışına taşabilecek veri katmanları, yer adları, MASKE, sınır, Türkiye içi işaretçiler
    map.addLayer({ id: "cells", type: "fill", source: "cells", layout: { visibility: "none" }, paint: { "fill-color": "#000", "fill-opacity": 0.45 } });
    map.addLayer({ id: "footprints", type: "line", source: "footprints", minzoom: PLUME_IMG_ZOOM, paint: { "line-color": "#2f5f93", "line-width": 1.5 } });
    map.addLayer({ id: "sat-ahead", type: "line", source: "sat-ahead", layout: { visibility: "none" }, paint: { "line-color": ["get", "color"], "line-width": 1.2, "line-dasharray": [2, 3] } });
    map.addLayer({ id: "pass-track", type: "line", source: "pass-track", paint: { "line-color": ["get", "color"], "line-width": 3 } });
    map.addLayer({ id: "labels-light", type: "raster", source: "labels-light" });
    map.addLayer({ id: "labels-dark", type: "raster", source: "labels-dark", layout: { visibility: "none" } });
    map.addLayer({ id: "mask", type: "fill", source: "mask", paint: { "fill-color": MASK_COLOR.light, "fill-opacity": 1 } });
    map.addLayer({ id: "turkey", type: "line", source: "turkey", paint: { "line-color": "#7d8d9b", "line-width": 1.2 } });
    map.addLayer({ id: "sources", type: "circle", source: "sources", layout: { visibility: "none" }, paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 6, 10, 9], "circle-color": "#ffffff",
      "circle-stroke-color": "#6b4fa0", "circle-stroke-width": 3 } });
    map.addLayer({ id: "obs-points", type: "circle", source: "obs", filter: ["!", ["has", "point_count"]], paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 5, 12, 7],
      "circle-color": ["case", ["get", "hasEmission"], "#d95c36", "#8696a4"],
      "circle-stroke-color": "#ffffff", "circle-stroke-width": 2, "circle-opacity": 1, "circle-stroke-opacity": 1 } });
    map.addLayer({ id: "selected", type: "circle", source: "selected", paint: {
      "circle-radius": 12, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#172b3a", "circle-stroke-width": 3 } });

    map.on("click", "cells", (e) => {
      if (map.queryRenderedFeatures(e.point, { layers: ["obs-points", "sources"] }).length) return;
      if (!geo.contains(e.lngLat.lng, e.lngLat.lat)) return;
      const p = e.features[0].properties;
      const v = state.view === "anom" ? `${F.signed(p.v, 1)} ppb (referansa göre)` : `${fmtNum(p.v, 1)} ppb`;
      new maplibregl.Popup().setLngLat(e.lngLat).setHTML(`<div class="popup"><b>${esc(v)}</b><table>
        <tr><td>Mutlak</td><td>${fmtNum(p.abs, 1)} ppb</td></tr><tr><td>Piksel sayısı</td><td>${p.n}</td></tr>
        <tr><td>Hücre merkezi</td><td>${fmtNum(p.lat, 2)}° K, ${fmtNum(p.lon, 2)}° D</td></tr></table></div>`).addTo(map);
    });
    map.on("click", "obs-points", (e) => openDetail(e.features[0].properties.id, { fly: false, push: true }));
    map.on("click", "sources", (e) => openSource(e.features[0].properties.id, { push: true }));
    for (const id of ["cells", "obs-points", "sources"]) {
      map.on("mouseenter", id, () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", id, () => (map.getCanvas().style.cursor = ""));
    }
    map.on("moveend", () => { addPlumeImages(); writeUrl(); });
    map.on("render", () => { if (map.isSourceLoaded("obs")) updateClusterMarkers(); });
    mapReady = true;
    updateMinZoom();
    init();
  });

  // Panel veya pencere boyutu değişince harita ve kamera sınırları güncellenir
  let resizeTimer;
  new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { map.resize(); if (mapReady) updateMinZoom(); }, 80);
  }).observe($("app"));

  // ---- Kümeler: gerçek kayıt sayısıyla HTML işaretçi (dış yazı tipi sunucusu gerekmez) -------
  const clusterMarkers = new Map();
  function updateClusterMarkers() {
    const visible = new Set();
    if (state.tab === "obs" && $("show-plumes").checked) {
      for (const f of map.querySourceFeatures("obs")) {
        const p = f.properties;
        if (!p.cluster) continue;
        const id = p.cluster_id;
        if (visible.has(id)) continue;
        visible.add(id);
        let m = clusterMarkers.get(id);
        if (!m) {
          const el = document.createElement("button");
          el.type = "button";
          const n = p.point_count;
          const size = n < 10 ? 34 : n < 50 ? 40 : 48;
          el.style.cssText = `width:${size}px;height:${size}px;border-radius:50%;border:3px solid #fff;background:#172b3a;color:#fff;font:600 13px/1 'Segoe UI',system-ui,sans-serif;cursor:pointer;box-shadow:0 2px 6px rgba(23,43,58,.35)`;
          el.textContent = n;
          el.setAttribute("aria-label", `${n} gözlem; yakınlaştır`);
          el.addEventListener("click", async (ev) => {
            ev.stopPropagation();
            const z = await map.getSource("obs").getClusterExpansionZoom(id);
            map.easeTo({ center: f.geometry.coordinates, zoom: Math.min(z + 0.2, 14) });
          });
          m = new maplibregl.Marker({ element: el }).setLngLat(f.geometry.coordinates);
          clusterMarkers.set(id, m);
        }
        if (!m._map) m.addTo(map);
      }
    }
    for (const [id, m] of clusterMarkers) if (!visible.has(id)) { m.remove(); clusterMarkers.delete(id); }
  }

  // ---- Temel harita ---------------------------------------------------------------------
  function setBase(name) {
    state.base = name === "dark" ? "dark" : "light";
    for (const b of ["light", "dark"]) {
      const v = b === state.base ? "visible" : "none";
      map.setLayoutProperty(`base-${b}`, "visibility", v);
      map.setLayoutProperty(`labels-${b}`, "visibility", v);
    }
    // Maske her temel haritada aynı katmandır; yalnızca rengi temaya uyar
    map.setPaintProperty("mask", "fill-color", MASK_COLOR[state.base]);
    [...$("basemap").children].forEach((b) => { const on = b.dataset.base === state.base; b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on)); });
    document.body.style.setProperty("--mask", MASK_COLOR[state.base]);
    writeUrl();
  }
  $("basemap").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setBase(b.dataset.base); });

  // ---- Panel yönetimi (tek seferde bir açılır panel; Escape ile kapanır, odak geri döner) ---
  const popovers = { filters: "filters-btn", "info-pop": "info-btn", layers: "layers-btn", "status-panel": "status-btn" };
  const openerOf = {};
  function openPopover(id) {
    for (const other of Object.keys(popovers)) if (other !== id) closePopover(other, false);
    $(id).hidden = false;
    $(popovers[id]).setAttribute("aria-expanded", "true");
    openerOf[id] = $(popovers[id]);
    const focusable = $(id).querySelector("select, input, button:not([data-close]), a");
    (focusable || $(id).querySelector("[data-close]"))?.focus();
  }
  function closePopover(id, restore = true) {
    if ($(id).hidden) return;
    $(id).hidden = true;
    $(popovers[id]).setAttribute("aria-expanded", "false");
    if (restore) openerOf[id]?.focus();
  }
  for (const [id, btn] of Object.entries(popovers)) {
    $(btn).addEventListener("click", () => ($(id).hidden ? openPopover(id) : closePopover(id)));
  }
  document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => closePopover(b.dataset.close)));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (document.querySelector(".mobile-menu[open]")) return;
    if (!$("q-results").hidden) { hideSearch(); return; }
    const open = Object.keys(popovers).find((id) => !$(id).hidden);
    if (open) { closePopover(open); return; }
    if (!$("detail").hidden) closeDetail({ push: true });
  });

  // ---- Filtreler --------------------------------------------------------------------------
  function fillSelect(id, values, current) {
    const sel = $(id);
    sel.length = 1;
    for (const [v, label] of values) sel.add(new Option(label, v));
    sel.value = values.some(([v]) => v === current) ? current : "";
  }
  function formToState() {
    state.filters = { sector: $("f-sector").value, sensor: $("f-sensor").value, days: $("f-period").value, em: $("f-emission").checked };
  }
  function stateToForm() {
    $("f-sector").value = state.filters.sector;
    $("f-sensor").value = state.filters.sensor;
    $("f-period").value = state.filters.days;
    $("f-emission").checked = state.filters.em;
    $("f-sort").value = state.sort;
  }
  const PERIOD = { 30: "Son 30 gün", 90: "Son 90 gün", 365: "Son 1 yıl" };
  function renderChips() {
    const f = state.filters;
    const chips = [];
    if (f.sector) chips.push(["sector", sectorName(f.sector)]);
    if (f.sensor) chips.push(["sensor", f.sensor]);
    if (f.days) chips.push(["days", PERIOD[f.days] || `Son ${f.days} gün`]);
    if (f.em) chips.push(["em", "Emisyon tahmini olanlar"]);
    $("chips").innerHTML = chips.map(([k, l]) => `<span class="chip">${esc(l)}<button type="button" data-chip="${k}" aria-label="${esc(l)} filtresini kaldır">×</button></span>`).join("");
    $("f-count").hidden = !chips.length;
    $("f-count").textContent = chips.length || "";
  }
  $("chips").addEventListener("click", (e) => {
    const b = e.target.closest("[data-chip]");
    if (!b) return;
    const k = b.dataset.chip;
    state.filters[k] = k === "em" ? false : "";
    stateToForm();
    applyFilters();
  });
  $("f-apply").onclick = () => { formToState(); closePopover("filters"); applyFilters(); };
  $("f-clear").onclick = () => { state.filters = { sector: "", sensor: "", days: "", em: false }; stateToForm(); applyFilters(); };
  $("f-sort").onchange = () => { state.sort = $("f-sort").value; loadObservations(); };
  function applyFilters() { renderChips(); loadObservations(); }

  // ---- Gözlemler ve kaynak özetleri --------------------------------------------------------
  const emissionText = (e) => (e && F.num(e.value)
    ? `${fmtNum(e.value)} kg CH₄/sa` + (e.uncertainty && F.num(e.uncertainty.value) ? ` ± ${fmtNum(e.uncertainty.value)}` : "")
    : "Tahmin yok");
  function query() {
    const q = new URLSearchParams({ limit: "2000", scope: "tr", sort: state.sort });
    const f = state.filters;
    if (f.sector) q.set("sector", f.sector);
    if (f.sensor) q.set("sensor", f.sensor);
    if (f.days) q.set("days", f.days);
    if (f.em) q.set("emission_only", "true");
    return q;
  }
  const obsReq = latestOnly();
  async function loadObservations() {
    const req = obsReq();
    $("cards").setAttribute("aria-busy", "true");
    if (state.tab === "obs") $("cards").innerHTML = '<div class="skeleton"></div>'.repeat(5);
    try {
      const r = await getJSON(`/api/observations?${query()}`, { signal: req.signal });
      if (!req.current()) return;
      state.items = r.items;
      state.total = r.total;
      state.shown = PAGE;
      if (!state.facets) {
        state.facets = r.facets;
        fillSelect("f-sector", r.facets.sectors.map((s) => [s, sectorName(s)]).sort((a, b) => a[1].localeCompare(b[1], "tr")), state.filters.sector);
        fillSelect("f-sensor", r.facets.sensors.map((s) => [s, s]), state.filters.sensor);
      }
      renderObservationsLayer();
      renderResults();
    } catch (e) {
      if (e.aborted) return;
      $("cards").innerHTML = `<div class="empty">Gözlemler alınamadı: ${esc(e.message)}.<br><button class="btn-secondary" type="button" id="retry">Yeniden dene</button></div>`;
      $("retry").onclick = loadObservations;
    } finally {
      $("cards").setAttribute("aria-busy", "false");
    }
    writeUrl();
  }
  async function loadSources() {
    try {
      const r = await getJSON("/api/source-summaries?scope=tr");
      state.sources = r.items;
      map.getSource("sources").setData({ type: "FeatureCollection", features: r.items.map((s) => ({
        type: "Feature", geometry: { type: "Point", coordinates: [s.lon, s.lat] }, properties: { id: s.id } })) });
    } catch (e) { state.sources = []; }
    if (state.tab === "src") renderResults();
    $("tab-src-n").textContent = `(${visibleSources().length})`;
  }
  const visibleSources = () => state.sources.filter((s) => !state.filters.sector || s.sectorCode === state.filters.sector);
  async function loadAllForSearch() {
    try { state.all = (await getJSON("/api/observations?limit=2000&scope=tr")).items; } catch (e) { state.all = []; }
  }

  function renderObservationsLayer() {
    const on = $("show-plumes").checked && state.tab === "obs";
    map.getSource("obs").setData({ type: "FeatureCollection", features: state.items.map((o) => ({
      type: "Feature", geometry: o.geometry, properties: { id: o.id, hasEmission: !!o.emission } })) });
    map.getSource("footprints").setData({ type: "FeatureCollection", features: state.items.filter((o) => o.footprint).map((o) => ({
      type: "Feature", geometry: o.footprint, properties: { id: o.id } })) });
    for (const id of ["obs-points", "footprints"]) map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
    map.setLayoutProperty("sources", "visibility", state.tab === "src" ? "visible" : "none");
    $("legend-obs").hidden = state.tab !== "obs" || !$("show-plumes").checked;
    $("legend-src").hidden = state.tab !== "src";
    highlightSelection();
    syncPlumeImages();
    updateClusterMarkers();
  }

  function thumbHtml(t, alt) {
    return t?.url
      ? `<span class="thumb"><img src="${esc(t.url)}" alt="${esc(alt)}" loading="lazy" decoding="async" title="${esc(t.kind)}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'noimg',textContent:'Görüntü yüklenemedi'}))"></span>`
      : `<span class="thumb"><span class="noimg">Görüntü yok</span></span>`;
  }
  function obsCard(o) {
    const when = o.observedAt ? fmtShort(o.observedAt) : "Gözlem zamanı doğrulanmadı";
    const num = o.emission && F.num(o.emission.value)
      ? `<b>${fmtNum(o.emission.value)}</b><span>kg CH₄/sa${o.emission.uncertainty && F.num(o.emission.uncertainty.value) ? ` ± ${fmtNum(o.emission.uncertainty.value)}` : ""}</span>`
      : `<span class="none">Emisyon tahmini yok</span>`;
    return `<button class="card${state.selected === o.id ? " sel" : ""}" type="button" role="listitem" data-id="${esc(o.id)}">
      ${thumbHtml(o.thumbnail, `${o.thumbnail?.kind || "Görüntü"} · ${o.locationLabel}`)}
      <span class="card-body"><span class="c-title">${esc(o.locationLabel)}</span>
        <span class="c-meta">${esc(when)} · ${esc(o.sensor ?? "Sensör belirtilmemiş")}</span>
        <span class="c-meta">${esc(o.provider)}${o.accessPaths.length > 1 ? " · 2 erişim yolu" : ""}</span>
        <span class="c-num">${num}</span>
        <span class="tag">${esc(sectorName(o.sectorCode))}</span></span></button>`;
  }
  function sourceCard(s) {
    const m = s.detectedMean;
    const num = m ? `<b>${fmtNum(m.value)}</b><span>kg CH₄/sa · tespitlerin ortalaması</span>` : `<span class="none">Ortalama yayımlanmamış</span>`;
    return `<button class="card${state.selectedSource === s.id ? " sel" : ""}" type="button" role="listitem" data-src="${esc(s.id)}">
      ${thumbHtml(s.thumbnail, `Kaynağın en son gözlem görüntüsü · ${s.locationLabel}`)}
      <span class="card-body"><span class="c-title">${esc(s.locationLabel)}</span>
        <span class="c-meta">Dönem: ${esc(s.firstObserved || "?")} – ${esc(s.lastObserved || "?")}</span>
        <span class="c-meta">${fmtNum(s.linkedObservations)} bağlı gözlem · ${fmtNum(s.detectionDateCount)} / ${fmtNum(s.observationDateCount)} tespit/gözlem tarihi</span>
        <span class="c-num">${num}</span>
        <span class="tag brand">Kaynak özeti</span> <span class="tag">${esc(sectorName(s.sectorCode))}</span></span></button>`;
  }

  function renderResults() {
    const obs = state.tab === "obs";
    const list = obs ? state.items : visibleSources();
    $("tab-obs-n").textContent = `(${state.items.length})`;
    $("tab-src-n").textContent = `(${visibleSources().length})`;
    $("r-count").textContent = fmtNum(list.length);
    $("r-noun").textContent = obs ? "gözlem" : "kaynak özeti";
    $("fab-count").textContent = `(${state.items.length})`;
    $("f-sort").disabled = !obs;
    const rng = state.facets?.observedRange;
    const span = rng ? `${new Date(rng[0]).getUTCFullYear()}–${new Date(rng[1]).getUTCFullYear()}` : "–";
    $("r-range").textContent = obs
      ? (state.filters.days ? `Bulut gözlemleri: son ${state.filters.days} gün (gözlem zamanına göre)` : `Bulut gözlemleri: tüm dönem (${span}) · ${state.total} kayıttan`)
      : "Sağlayıcının bulutları kümeleyerek oluşturduğu özetler; tek bulut değildir.";
    if (!list.length) {
      const any = Object.values(state.filters).some(Boolean);
      $("cards").innerHTML = `<div class="empty">Bu seçimle kayıt yok. Bu, metan olmadığı anlamına gelmez.${any ? '<br><button class="btn-secondary" type="button" id="clear-empty">Filtreleri temizle</button>' : ""}</div>`;
      $("clear-empty")?.addEventListener("click", () => $("f-clear").click());
      return;
    }
    const shown = list.slice(0, state.shown);
    $("cards").innerHTML = shown.map(obs ? obsCard : sourceCard).join("")
      + (list.length > state.shown ? `<button class="btn-secondary more" type="button" id="more">Daha fazla göster (${list.length - state.shown})</button>` : "");
    $("more")?.addEventListener("click", () => { state.shown += PAGE; renderResults(); });
  }

  $("cards").addEventListener("click", (e) => {
    const c = e.target.closest(".card");
    if (!c) return;
    state.scroll = $("cards").scrollTop;
    if (c.dataset.id) openDetail(c.dataset.id, { fly: true, push: true, opener: c });
    else if (c.dataset.src) openSource(c.dataset.src, { fly: true, push: true, opener: c });
  });

  function setTab(tab) {
    state.tab = tab;
    $("tab-obs").setAttribute("aria-selected", String(tab === "obs"));
    $("tab-src").setAttribute("aria-selected", String(tab === "src"));
    $("tab-obs").tabIndex = tab === "obs" ? 0 : -1;
    $("tab-src").tabIndex = tab === "src" ? 0 : -1;
    state.shown = PAGE;
    renderObservationsLayer();
    renderResults();
    writeUrl();
  }
  $("tab-obs").onclick = () => setTab("obs");
  $("tab-src").onclick = () => setTab("src");
  document.querySelector(".results .tabs").addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") { setTab(state.tab === "obs" ? "src" : "obs"); $(state.tab === "obs" ? "tab-obs" : "tab-src").focus(); }
  });

  // CSV: listede ve haritada görünen kümenin aynısı (Türkiye kapsamı)
  $("download").onclick = () => {
    const cell = (v) => (v == null ? "" : /[",\n;]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
    let cols, rows;
    if (state.tab === "obs") {
      cols = ["id", "provider", "sensor", "observedAt", "publishedAt", "ingestedAt", "lat", "lon", "sectorCode",
        "emission_kg_h", "uncertainty_kg_h", "processingVersion", "sourceId", "accessPaths", "sourceUrl"];
      rows = state.items.map((o) => [o.id, o.provider, o.sensor, o.observedAt, o.publishedAt, o.ingestedAt, o.lat, o.lon,
        o.sectorCode, o.emission?.value, o.emission?.uncertainty?.value, o.processingVersion, o.sourceId,
        o.accessPaths.map((a) => `${a.provider}:${a.id}`).join(" | "), o.sourceUrl]);
    } else {
      cols = ["id", "lat", "lon", "sectorCode", "firstObserved", "lastObserved", "linkedObservations", "detectionDateCount",
        "observationDateCount", "detectedMean_kg_h", "persistenceWeighted_kg_h", "sourceUrl"];
      rows = visibleSources().map((s) => [s.providerId, s.lat, s.lon, s.sectorCode, s.firstObserved, s.lastObserved, s.linkedObservations,
        s.detectionDateCount, s.observationDateCount, s.detectedMean?.value, s.persistenceWeighted?.value, s.sourceUrl]);
    }
    const blob = new Blob(["﻿" + [cols.join(","), ...rows.map((r) => r.map(cell).join(","))].join("\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `metan-${state.tab === "obs" ? "gozlemleri" : "kaynak-ozetleri"}-turkiye-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };

  // Sağlayıcı bulut görüntüleri yalnızca yakınlaştırınca ve görünen alandakiler için yüklenir (maskenin altında)
  const plumeImages = new Set();
  function syncPlumeImages() {
    const on = $("show-plumes").checked && state.tab === "obs";
    const visible = new Set(state.items.map((o) => o.id));
    plumeImages.forEach((id) => map.setLayoutProperty(`img-${id}`, "visibility", on && visible.has(id) ? "visible" : "none"));
    addPlumeImages();
  }
  function addPlumeImages() {
    if (!$("show-plumes").checked || state.tab !== "obs" || map.getZoom() < PLUME_IMG_ZOOM) return;
    const v = map.getBounds();
    for (const o of state.items) {
      const b = o.raster?.bounds;
      if (!o.raster?.png || !b || plumeImages.has(o.id)) continue;
      if (b[2] < v.getWest() || b[0] > v.getEast() || b[3] < v.getSouth() || b[1] > v.getNorth()) continue;
      const id = `img-${o.id}`;
      map.addSource(id, { type: "image", url: o.raster.png, coordinates: [[b[0], b[3]], [b[2], b[3]], [b[2], b[1]], [b[0], b[1]]] });
      map.addLayer({ id, type: "raster", source: id, minzoom: PLUME_IMG_ZOOM, paint: { "raster-fade-duration": 0 } }, "footprints");
      plumeImages.add(o.id);
    }
  }
  $("show-plumes").onchange = () => { renderObservationsLayer(); writeUrl(); };

  function highlightSelection() {
    const sel = state.selected;
    const op = sel ? ["case", ["==", ["get", "id"], sel], 1, 0.35] : 1;
    map.setPaintProperty("obs-points", "circle-opacity", op);
    map.setPaintProperty("obs-points", "circle-stroke-opacity", op);
  }

  // ---- Arama: katalog kayıtları ve koordinat (yer adı araması yok) ---------------------------
  let searchHits = [], searchIdx = -1;
  function parseCoord(s) {
    const m = s.trim().replace(/°/g, "").match(/^(-?\d{1,2}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)$/);
    if (!m) return null;
    const a = Number(m[1]), b = Number(m[2]);
    return Number.isFinite(a) && Number.isFinite(b) ? { lat: a, lon: b } : null;
  }
  const norm = (s) => String(s || "").toLocaleLowerCase("tr").replace(/[,°]/g, ".");
  function runSearch() {
    const q = $("q").value.trim();
    const box = $("q-results");
    searchHits = []; searchIdx = -1;
    if (q.length < 2) { hideSearch(); return; }
    const html = [];
    // Koordinat girişi "41,21 28,15" ya da "41.21, 28.15" (enlem, boylam)
    const c = parseCoord(q.replace(/(\d),(\d)/g, "$1.$2"));
    if (c) {
      if (geo.contains(c.lon, c.lat)) {
        searchHits.push({ kind: "coord", ...c });
        html.push(`<div class="q-group">Koordinat</div><button class="q-opt" role="option" data-k="0" type="button">${fmtNum(c.lat, 4)}° K, ${fmtNum(c.lon, 4)}° D<small>Bu noktaya git</small></button>`);
      } else {
        html.push('<div class="q-msg">Bu koordinat Türkiye dışında. Türkiye içinden bir konum seçin.</div>');
      }
    }
    const nq = norm(q);
    const obs = state.all.filter((o) => [o.id, o.providerId, o.locationLabel, o.sensor].some((v) => norm(v).includes(nq))).slice(0, 6);
    if (obs.length) {
      html.push('<div class="q-group">Gözlem kayıtları</div>');
      for (const o of obs) {
        searchHits.push({ kind: "obs", id: o.id });
        html.push(`<button class="q-opt" role="option" data-k="${searchHits.length - 1}" type="button">${esc(o.locationLabel)} · ${esc(fmtShort(o.observedAt) || "")}<small>${esc(o.sensor || "")} · ${esc(o.providerId || o.id)}</small></button>`);
      }
    }
    const srcs = state.sources.filter((s) => [s.providerId, s.locationLabel, sectorName(s.sectorCode)].some((v) => norm(v).includes(nq))).slice(0, 4);
    if (srcs.length) {
      html.push('<div class="q-group">Kaynak özetleri</div>');
      for (const s of srcs) {
        searchHits.push({ kind: "src", id: s.id });
        html.push(`<button class="q-opt" role="option" data-k="${searchHits.length - 1}" type="button">${esc(s.locationLabel)}<small>${esc(sectorName(s.sectorCode))} · ${esc(s.providerId)}</small></button>`);
      }
    }
    if (!html.length) html.push('<div class="q-msg">Sonuç yok. Yer adı araması bulunmuyor; kayıt kimliği, sensör, kaynak veya koordinat (ör. 41,21 28,15) deneyin.</div>');
    box.innerHTML = html.join("");
    box.hidden = false;
    $("q").setAttribute("aria-expanded", "true");
  }
  function hideSearch() { $("q-results").hidden = true; $("q").setAttribute("aria-expanded", "false"); searchIdx = -1; }
  function pickSearch(k) {
    const h = searchHits[k];
    if (!h) return;
    hideSearch();
    if (h.kind === "coord") {
      map.flyTo({ center: [h.lon, h.lat], zoom: Math.max(map.getZoom(), 10) });
      new maplibregl.Popup().setLngLat([h.lon, h.lat]).setHTML(`<div class="popup"><b>Seçilen nokta</b><br>${fmtNum(h.lat, 4)}° K, ${fmtNum(h.lon, 4)}° D<br><a href="/hava-ve-iklim.html?lat=${h.lat.toFixed(4)}&lon=${h.lon.toFixed(4)}">Bu noktanın hava ve iklimi</a></div>`).addTo(map);
    } else if (h.kind === "obs") openDetail(h.id, { fly: true, push: true });
    else openSource(h.id, { fly: true, push: true });
  }
  $("q").addEventListener("input", runSearch);
  $("q").addEventListener("focus", () => { if ($("q").value.trim().length >= 2) runSearch(); });
  $("q").addEventListener("keydown", (e) => {
    const opts = [...$("q-results").querySelectorAll(".q-opt")];
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!opts.length) return;
      e.preventDefault();
      searchIdx = (searchIdx + (e.key === "ArrowDown" ? 1 : -1) + opts.length) % opts.length;
      opts.forEach((o, i) => o.setAttribute("aria-selected", String(i === searchIdx)));
      opts[searchIdx].scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (opts.length) pickSearch(searchIdx >= 0 ? Number(opts[searchIdx].dataset.k) : 0);
    }
  });
  $("q-results").addEventListener("click", (e) => { const b = e.target.closest(".q-opt"); if (b) pickSearch(Number(b.dataset.k)); });
  document.addEventListener("click", (e) => { if (!e.target.closest(".search")) hideSearch(); });

  // ---- Sonuçlar paneli / alt panel --------------------------------------------------------
  function setResultsOpen(open) {
    state.resultsOpen = open;
    $("results").hidden = !open || !$("detail").hidden;
    $("results-open").hidden = open || !$("detail").hidden;
    writeUrl();
  }
  $("results-close").onclick = () => { setResultsOpen(false); $("results-open").focus(); };
  $("results-open").onclick = () => { setResultsOpen(true); $("results-close").focus(); };
  const SHEETS = ["peek", "half", "full"];
  const setSheet = (s) => { $("app").dataset.sheet = s; };
  $("sheet-up").onclick = (e) => { e.stopPropagation(); setSheet(SHEETS[Math.min(2, SHEETS.indexOf($("app").dataset.sheet) + 1)]); };
  $("sheet-down").onclick = (e) => { e.stopPropagation(); setSheet(SHEETS[Math.max(0, SHEETS.indexOf($("app").dataset.sheet) - 1)]); };
  document.querySelector(".results-head").addEventListener("click", (e) => {
    if (!MOBILE() || e.target.closest("button, select, label")) return;
    setSheet($("app").dataset.sheet === "peek" ? "half" : "peek");
  });

  // ---- Ayrıntı ---------------------------------------------------------------------------
  const detailReq = latestOnly();
  let detailOpener = null;
  function showDetail() {
    $("detail").hidden = false;
    $("results").hidden = true;
    $("results-open").hidden = true;
    $("detail-body").scrollTop = 0;
  }
  function closeDetail({ push = false } = {}) {
    if ($("detail").hidden) return;
    detailReq();
    $("detail").hidden = true;
    state.selected = null;
    state.selectedSource = null;
    map.getSource("selected").setData(empty());
    highlightSelection();
    setResultsOpen(state.resultsOpen || MOBILE());
    renderResults();
    $("cards").scrollTop = state.scroll;
    writeUrl(push);
    const ds = detailOpener?.dataset || {};
    const back = (ds.id && document.querySelector(`.card[data-id="${CSS.escape(ds.id)}"]`)) || (ds.src && document.querySelector(`.card[data-src="${CSS.escape(ds.src)}"]`));
    (back || ($("results").hidden ? $("results-open") : $("cards")))?.focus?.();
  }
  $("detail-close").onclick = () => closeDetail({ push: true });
  $("detail-back").onclick = () => closeDetail({ push: true });

  const kv = (rows) => `<dl class="kv">${rows.filter(Boolean).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
  const timeCell = (iso) => (iso ? `${esc(fmtLocal(iso))}<small>${esc(fmtUtc(iso))}</small>` : F.notProvided);
  const uncertaintyText = (e) => {
    const u = e?.uncertainty;
    if (u && F.num(u.lower) && F.num(u.upper)) return `${fmtNum(u.lower)}–${fmtNum(u.upper)} kg CH₄/sa · ${esc(u.label || "istatistiksel tanımı kayıtta belirtilmemiş")}`;
    if (u && F.num(u.value)) return `± ${fmtNum(u.value)} kg CH₄/sa · ${esc(u.label || "sağlayıcının simetrik belirsizliği; istatistiksel tanımı kayıtta belirtilmemiş")}`;
    return "Sağlayıcı belirsizlik vermemiş";
  };
  function flyTo(lon, lat) {
    if (!geo.contains(lon, lat)) return;
    map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), PLUME_IMG_ZOOM + 1), padding: viewPadding() });
  }
  const miniCard = (x) => `<button class="card" type="button" data-open="${esc(x.id)}">${thumbHtml(x.thumbnail, x.thumbnail?.kind || "Görüntü")}<span class="card-body"><span class="c-title">${esc(fmtShort(x.observedAt) || "zaman yok")}</span><span class="c-meta">${esc(x.sensor ?? "")}</span><span class="c-num">${x.emission ? `<b>${fmtNum(x.emission.value)}</b><span>kg CH₄/sa</span>` : '<span class="none">Tahmin yok</span>'}</span></span></button>`;

  async function openDetail(id, { fly = false, push = false, opener = null } = {}) {
    const req = detailReq();
    state.selected = id;
    state.selectedSource = null;
    detailOpener = opener || document.activeElement;
    for (const k of Object.keys(popovers)) closePopover(k, false);
    showDetail();
    $("detail-body").innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
    let r;
    try { r = await getJSON(`/api/observations/${encodeURIComponent(id)}`, { signal: req.signal }); }
    catch (e) {
      if (!e.aborted && req.current()) $("detail-body").innerHTML = `<div class="empty">Gözlem açılamadı: ${esc(e.message)}</div>`;
      return;
    }
    if (!req.current()) return;
    const o = r.observation;
    const [lon, lat] = o.geometry.coordinates;
    map.getSource("selected").setData({ type: "FeatureCollection", features: [{ type: "Feature", geometry: o.geometry, properties: {} }] });
    highlightSelection();
    if (fly) flyTo(lon, lat);
    $("detail-body").innerHTML = detailHtml(o, r.sourceSummary, r.relatedObservations);
    $("d-title").focus({ preventScroll: true });
    writeUrl(push);
    bindDetailTabs();
    loadWeather(o, req);
    loadSurface(o, req);
  }

  function detailHtml(o, src, related) {
    const e = o.emission;
    const img = o.thumbnail?.url
      ? `<figure class="d-img"><img src="${esc(o.thumbnail.url)}" alt="${esc(o.thumbnail.kind)}" loading="lazy"><figcaption>${esc(o.thumbnail.kind)}. Renk ölçeğinin sayısal lejantı sağlayıcı yanıtında yok.${o.raster ? " Haritada yakınlaştırınca aynı ürünün artış görüntüsü konumunda açılır." : ""}</figcaption></figure>`
      : `<div class="status-box d-img">Bu kayıtta yayımlanmış görüntü yok. Haritadaki işaret yalnızca tespit noktasıdır.</div>`;
    const alt = (o.alternateEstimates || []).map((a) => `<li>${esc(a.provider)} (${esc(a.id)}): ${a.emission ? esc(emissionText(a.emission)) : "emisyon tahmini yayımlanmamış"}${a.maxEnhancement?.value != null ? ` · en yüksek artış ${fmtNum(a.maxEnhancement.value)} ${esc(a.maxEnhancement.unit)}` : ""}</li>`).join("");
    const access = o.accessPaths.map((a) => `<a class="btn-secondary small" href="${esc(a.url)}" target="_blank" rel="noopener">Özgün kaydı aç · ${esc(a.provider)} ↗</a>`).join("");
    const attribution = src
      ? `Sağlayıcı bu bulutu ${esc(String(src.clusterRadiusM ?? "?"))} m kümeleme yarıçaplı bir kaynak kaydına bağlıyor. Tesis doğrulaması değildir.`
      : "Sağlayıcı kaynak kümelemesi veya doğrulanmış tesis eşleştirmesi yok.";
    const rel = related.length ? `<div class="related">${related.map(miniCard).join("")}</div>` : '<p class="note">Bu kayda bağlı başka gözlem yok.</p>';
    return `
      <h2 class="d-place" id="d-title" tabindex="-1">${esc(o.locationLabel)}</h2>
      <p class="d-sub">${esc(fmtLocal(o.observedAt) || "Gözlem zamanı doğrulanmadı")} · ${esc(o.sensor ?? "Sensör belirtilmemiş")}<br>${esc(o.provider)}</p>
      ${e && F.num(e.value) ? `<p class="d-num"><b>${fmtNum(e.value)}</b><span>kg CH₄/saat · bu gözlemin emisyon tahmini</span></p><p class="d-unc">${uncertaintyText(e)}</p>`
        : '<div class="status-box" style="margin-top:12px">Sağlayıcı bu gözlem için emisyon tahmini yayımlamadı. Bu, sıfır emisyon anlamına gelmez.</div>'}
      ${img}
      <div class="tabs d-tabs" role="tablist" aria-label="Gözlem ayrıntısı">
        <button role="tab" id="dt-obs" aria-selected="true" aria-controls="dp-obs">Gözlem</button>
        <button role="tab" id="dt-wx" aria-selected="false" aria-controls="dp-wx" tabindex="-1">Meteoroloji</button>
        <button role="tab" id="dt-hist" aria-selected="false" aria-controls="dp-hist" tabindex="-1">Geçmiş</button>
      </div>
      <div role="tabpanel" id="dp-obs" aria-labelledby="dt-obs">
        <section class="dsec"><h3>Zaman</h3>${kv([["Gözlem", timeCell(o.observedAt)], ["Yayımlanma", timeCell(o.publishedAt)], ["Sisteme alınma", timeCell(o.ingestedAt)]])}
          <p class="note">Gözlem yılı sağlayıcının tarih alanından alınır; kayıt kimliğinden türetilmez.</p></section>
        <section class="dsec"><h3>Kayıt ve yöntem</h3>${kv([
          ["Özgün kimlik", esc(o.providerId || o.id)],
          ["Sensör / platform", `${esc(o.sensor ?? "–")}${o.platform ? ` <small>${esc(o.platform)}</small>` : ""}`],
          ["Sektör", esc(sectorName(o.sectorCode))],
          ["İşlem sürümü", esc(o.processingVersion || F.notProvided)],
          o.gsdM ? ["Yer örneklemesi", `${fmtNum(o.gsdM)} m`] : null,
          ["Kalite", esc(o.qualityNote || "Sağlayıcı kalite alanı boş")],
          ["Kaynak eşleştirmesi", esc(attribution)],
        ])}
        ${alt ? `<p class="note">Aynı gözlemin diğer erişim yolundaki tahmin (bağımsız doğrulama değildir):</p><ul class="note">${alt}</ul>` : ""}</section>
        <section class="dsec" id="d-surface"><h3>Yüzey görüntüsü</h3><div class="status-box">Yükleniyor…</div></section>
        <section class="dsec"><h3>Bu ölçümü anlamak</h3>${understandHtml()}</section>
      </div>
      <div role="tabpanel" id="dp-wx" aria-labelledby="dt-wx" hidden>
        <section class="dsec" id="d-weather"><div class="status-box">Yükleniyor…</div></section>
      </div>
      <div role="tabpanel" id="dp-hist" aria-labelledby="dt-hist" hidden>
        ${src ? sourceBlock(src) : '<p class="note">Bu gözlem bir kaynak özetine bağlı değil.</p>'}
        <section class="dsec"><h3>Aynı kaynağın diğer kayıtları</h3>${rel}</section>
      </div>
      <div class="actions">${access}
        <a class="btn-secondary small" href="/hava-ve-iklim.html?obs=${encodeURIComponent(o.id)}">Hava ve İklim'de aç</a></div>`;
  }
  function sourceBlock(src) {
    const dm = src.detectedMean, pw = src.persistenceWeighted;
    return `<section class="dsec"><h3>Kaynak özeti (sağlayıcı) — tek bulut değildir</h3>${kv([
      ["Kaynak kaydı", esc(src.providerId)],
      ["Dönem", `${esc(src.firstObserved || "?")} – ${esc(src.lastObserved || "?")}`],
      ["Gözlem / tespit tarihi", `${fmtNum(src.observationDateCount)} / ${fmtNum(src.detectionDateCount)} <small>Sağlayıcının saydığı uygun gözlem ve tespit tarihleri</small>`],
      ["Tespitlerin ortalaması", dm ? `${fmtNum(dm.value)} kg/sa${dm.uncertainty ? ` ± ${fmtNum(dm.uncertainty.value)}` : ""}` : F.notProvided],
      ["Süreklilikle ağırlıklı ortalama", pw ? `${fmtNum(pw.value)} kg/sa <small>Sağlayıcı alanı: tespit ortalaması × (tespit tarihi / gözlem tarihi)</small>` : F.notProvided],
    ])}<p class="note">Kaynak ortalaması bir dönemi özetler; tek bir buluta atanmaz.</p></section>`;
  }
  function understandHtml() {
    const g = state.content?.glossary;
    const pick = g ? ["emisyon", "belirsizlik", "gecikme", "ruzgar"].map((id) => g.find((x) => x.id === id)).filter(Boolean) : [];
    return pick.map((x) => `<p class="note"><b>${esc(x.title)}.</b> ${esc(x.text)}</p>`).join("")
      + '<p class="note"><a href="/yuksek-degerleri-yorumlamak.html">Yüksek değerleri yorumlama rehberi</a></p>';
  }
  function bindDetailTabs() {
    const tabs = ["obs", "wx", "hist"];
    const set = (t, focus) => {
      for (const k of tabs) {
        const on = k === t;
        $(`dt-${k}`).setAttribute("aria-selected", String(on));
        $(`dt-${k}`).tabIndex = on ? 0 : -1;
        $(`dp-${k}`).hidden = !on;
      }
      if (focus) $(`dt-${t}`).focus();
    };
    for (const k of tabs) $(`dt-${k}`).onclick = () => set(k);
    document.querySelector(".d-tabs").addEventListener("keydown", (e) => {
      const i = tabs.findIndex((k) => $(`dt-${k}`).getAttribute("aria-selected") === "true");
      if (e.key === "ArrowRight") set(tabs[(i + 1) % 3], true);
      if (e.key === "ArrowLeft") set(tabs[(i + 2) % 3], true);
    });
  }
  $("detail-body").addEventListener("click", (e) => {
    const b = e.target.closest("[data-open]");
    if (b) openDetail(b.dataset.open, { fly: true, push: true });
  });

  async function loadWeather(o, req) {
    const box = () => document.getElementById("d-weather");
    try {
      const w = await getJSON(`/api/weather/observation/${encodeURIComponent(o.id)}`, { signal: req.signal });
      if (req.current() && box()) box().innerHTML = weatherHtml(w);
    } catch (e) {
      if (!e.aborted && req.current() && box()) box().innerHTML = `<div class="status-box">Meteoroloji alınamadı: ${esc(e.message)}. Diğer bilgiler etkilenmedi.</div>`;
    }
  }
  function weatherHtml(w) {
    const p = w.primary, job = w.cdsJob;
    const jobLine = job ? `<p class="note">CDS ERA5 işi: ${esc(F.STATUS_TEXT[job.state] || job.state)}${job.errorClass ? ` · ${esc(F.ERROR_CLASS[job.errorClass] || job.errorClass)}` : ""}</p>` : "";
    if (!p) {
      const msg = w.status === "empty" ? "Gözlem anına ait meteoroloji henüz yok. ERA5 yeniden analizi birkaç gün gecikmeyle yayımlanır; güncel tahmin bu gözleme bağlanmaz."
        : w.status === "unavailable" ? `Meteoroloji şu an alınamıyor (${esc(F.ERROR_CLASS[w.error?.errorClass] || "bilinmeyen neden")}).` : esc(w.message || F.STATUS_TEXT[w.status] || w.status);
      return `<div class="status-box">${msg}</div>${jobLine}`;
    }
    const v = p.variables || {};
    const val = (keys, d = 1) => { for (const k of keys) if (v[k] && F.num(v[k].value)) return `${fmtNum(v[k].value, d)} ${esc(v[k].unit || "")}`; return "–"; };
    const off = p.timeOffsetMinutes;
    return `
      <div class="wind-row">${F.windArrow(p.wind10?.to)}<div><b>10 m rüzgâr</b><br>${esc(F.windText(p.wind10))}</div></div>
      ${p.wind100 ? `<div class="wind-row">${F.windArrow(p.wind100?.to)}<div><b>100 m rüzgâr</b><br>${esc(F.windText(p.wind100))}</div></div>` : ""}
      <dl class="wx-main">
        <div><dt>Sıcaklık (2 m)</dt><dd>${val(["temperature_2m", "t2m"])}</dd></div>
        <div><dt>Bulutluluk</dt><dd>${val(["cloud_cover", "tcc"], 0)}</dd></div>
        <div><dt>Yağış (önceki 1 sa)</dt><dd>${val(["precipitation", "tp"])}</dd></div>
        <div><dt>Bağıl nem</dt><dd>${val(["relative_humidity_2m", "rh"], 0)}</dd></div>
        <div><dt>Yüzey basıncı</dt><dd>${val(["surface_pressure", "sp"], 0)}</dd></div>
        <div><dt>Sınır tabakası</dt><dd>${val(["boundary_layer_height", "blh"], 0)}</dd></div>
      </dl>
      ${kv([
        ["Meteoroloji saati", `${esc(fmtLocal(p.validAt) || "–")}<small>${esc(fmtUtc(p.validAt) || "")} · gözlemden ${F.num(off) ? `${off > 0 ? "+" : ""}${off} dk` : "?"}</small>`],
        ["Kaynak yolu", `${esc(p.accessPath)} <small>${esc(p.dataset)} · yeniden analiz · ${esc(p.temporalResolution)} · ${esc(p.spatialResolution)}</small>`],
      ])}
      <p class="note">Ok rüzgârın taşıdığı yönü gösterir (yukarı = kuzey); derece rüzgârın geldiği yöndür.${p.attribution ? ` ${esc(p.attribution)}.` : ""}</p>${jobLine}`;
  }
  async function loadSurface(o, req) {
    const box = () => document.getElementById("d-surface");
    try {
      const s = await getJSON(`/api/imagery/${encodeURIComponent(o.id)}`, { signal: req.signal });
      if (!req.current() || !box()) return;
      let html;
      if (s.status === "ready") html = `<figure class="d-img"><img src="${esc(s.imageUrl)}" alt="Sentinel-2 L2A doğal renkli yüzey görüntüsü" loading="lazy"><figcaption>Yüzey görüntüsü · ${esc(s.collection)} · sahne ${esc(fmtLocal(s.scene.datetime))} · bulut %${fmtNum(s.scene.cloudCover)} · ${esc(s.note)}</figcaption></figure>`;
      else if (s.status === "not_configured") html = '<div class="status-box">Yüzey görüntüsü erişimi henüz bağlanmamış.</div>';
      else if (s.status === "empty") html = `<div class="status-box">${esc(s.message)}</div>`;
      else if (s.status === "queued") html = '<div class="status-box">Yüzey görüntüsü hazırlanıyor. Birkaç dakika sonra yeniden açın.</div>';
      else html = `<div class="status-box">Yüzey görüntüsü alınamadı (${esc(F.ERROR_CLASS[s.errorClass] || s.errorClass || "bilinmeyen neden")}).</div>`;
      box().innerHTML = `<h3>Yüzey görüntüsü</h3>${html}`;
    } catch (e) {
      if (!e.aborted && req.current() && box()) box().innerHTML = `<h3>Yüzey görüntüsü</h3><div class="status-box">Alınamadı: ${esc(e.message)}</div>`;
    }
  }

  async function openSource(id, { fly = false, push = false, opener = null } = {}) {
    const req = detailReq();
    state.selected = null;
    state.selectedSource = id;
    detailOpener = opener || document.activeElement;
    for (const k of Object.keys(popovers)) closePopover(k, false);
    showDetail();
    $("detail-body").innerHTML = '<div class="skeleton"></div>';
    try {
      const r = await getJSON(`/api/source-summaries/${encodeURIComponent(id)}`, { signal: req.signal });
      if (!req.current()) return;
      const s = r.source;
      if (fly) flyTo(s.lon, s.lat);
      map.getSource("selected").setData({ type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Point", coordinates: [s.lon, s.lat] }, properties: {} }] });
      highlightSelection();
      // Kaynağa bağlı gözlemlerden yalnızca Türkiye kapsamındakiler listelenir
      const inScope = new Set(state.all.map((x) => x.id));
      const plumes = r.observations.filter((x) => inScope.has(x.id));
      $("detail-body").innerHTML = `
        <h2 class="d-place" id="d-title" tabindex="-1">${esc(s.locationLabel)}</h2>
        <p class="d-sub">Kaynak özeti · ${esc(sectorName(s.sectorCode))}<br>Carbon Mapper</p>
        ${s.detectedMean ? `<p class="d-num"><b>${fmtNum(s.detectedMean.value)}</b><span>kg CH₄/saat · tespitlerin ortalaması</span></p>` : ""}
        ${s.thumbnail ? `<figure class="d-img"><img src="${esc(s.thumbnail.url)}" alt="${esc(s.thumbnail.kind)}" loading="lazy"><figcaption>En son bağlı gözlemin görüntüsü · ${esc(s.thumbnail.kind)}</figcaption></figure>` : ""}
        ${sourceBlock(s)}
        <section class="dsec"><h3>Bağlı gözlemler (${plumes.length})</h3><div class="related">${plumes.map(miniCard).join("")}</div></section>
        <p class="note">Koordinat sağlayıcının küme merkezidir; tesis adı veya sınırı atanmamıştır.</p>
        <div class="actions"><a class="btn-secondary small" href="${esc(s.sourceUrl)}" target="_blank" rel="noopener">Özgün kaydı aç · Carbon Mapper ↗</a>
          <a class="btn-secondary small" href="/hava-ve-iklim.html?lat=${s.lat.toFixed(4)}&lon=${s.lon.toFixed(4)}">Bu konumun hava ve iklimi</a></div>`;
      $("d-title").focus({ preventScroll: true });
      writeUrl(push);
    } catch (e) {
      if (!e.aborted && req.current()) $("detail-body").innerHTML = `<div class="empty">Kaynak özeti açılamadı: ${esc(e.message)}</div>`;
    }
  }

  // ---- İleri/geri: seçili kayıt URL'den eşitlenir ----------------------------------------
  window.addEventListener("popstate", () => {
    const u = readUrl();
    if (u.sel && u.sel !== state.selected) openDetail(u.sel, { fly: true });
    else if (u.src && u.src !== state.selectedSource) openSource(u.src, { fly: true });
    else if (!u.sel && !u.src) closeDetail();
  });

  // ---- Harita araçları -----------------------------------------------------------------------
  $("zoom-in").onclick = () => map.zoomIn();
  $("zoom-out").onclick = () => map.zoomOut();
  $("home").onclick = () => goHome(true);

  // ---- TROPOMI -------------------------------------------------------------------------------
  function range() {
    const end = state.days[state.dayIdx].day;
    if (state.mode === "day") return [end, end];
    const d = new Date(end + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() - (Number(state.mode) - 1));
    return [d.toISOString().slice(0, 10), end];
  }
  const tropomiReq = latestOnly();
  async function loadTropomi() {
    if (state.dayIdx < 0 || !$("show-tropomi").checked) { updateCaption(); return; }
    const [start, end] = range();
    const day = state.days[state.dayIdx];
    $("daylabel").innerHTML = state.mode === "day"
      ? `${fmtDay(end)}<small>${esc(PRODUCT[day.source] || "Bu gün için ölçüm yok")}</small>`
      : `${fmtDay(start)} – ${fmtDay(end)}<small>${state.mode} günlük ortalama</small>`;
    $("prev").disabled = state.dayIdx <= 0;
    $("next").disabled = state.dayIdx >= state.days.length - 1;
    const req = tropomiReq();
    try {
      const data = await getJSON(`/api/tropomi?start=${start}&end=${end}&scope=tr`, { signal: req.signal });
      if (!req.current()) return;
      state.data = data;
      renderCells();
    } catch (e) {
      if (!e.aborted) $("tropomi-meta").textContent = `TROPOMI verisi alınamadı: ${e.message}.`;
    }
    renderSpark();
    updateCaption();
    writeUrl();
  }
  function updateCaption() {
    const on = $("show-tropomi").checked && state.dayIdx >= 0;
    $("tropomi-caption").hidden = !on;
    $("legend-tropomi").hidden = !on;
    if (!on) return;
    const [start, end] = range();
    const day = state.days[state.dayIdx];
    $("tropomi-caption").textContent = state.mode === "day"
      ? `TROPOMI XCH₄ · ${fmtDay(end)} · ${day.source || "ölçüm yok"}`
      : `TROPOMI XCH₄ · ${fmtDay(start)} – ${fmtDay(end)} ortalaması`;
    $("lg-mode").textContent = state.view === "anom" ? "· anomali (ppb)" : "· ppb";
  }
  function renderCells() {
    const d = state.data, st = d.stats, anom = state.view === "anom";
    let lo, hi, colors;
    if (!st) { lo = 1850; hi = 1950; colors = PLASMA; }
    else if (anom) { const m = Math.max(10, Math.ceil(Math.max(st.median - st.p02, st.p98 - st.median) / 5) * 5); lo = -m; hi = m; colors = DIVERGING; }
    else { lo = Math.floor(st.p02); hi = Math.ceil(st.p98); colors = PLASMA; if (hi - lo < 10) { lo -= 5; hi += 5; } }
    const g = d.grid;
    const features = d.cells.map(([j, i, v, n]) => {
      const x = d.lon0 + i * g, y = d.lat0 + j * g;
      return { type: "Feature", properties: { v: anom ? v - st.median : v, abs: v, n, lon: x + g / 2, lat: y + g / 2 },
        geometry: { type: "Polygon", coordinates: [[[x, y], [x + g, y], [x + g, y + g], [x, y + g], [x, y]]] } };
    });
    map.getSource("cells").setData({ type: "FeatureCollection", features });
    const stops = colors.flatMap((c, k) => [lo + ((hi - lo) * k) / (colors.length - 1), c]);
    map.setPaintProperty("cells", "fill-color", ["interpolate", ["linear"], ["get", "v"], ...stops]);
    $("legend-bar").style.background = `linear-gradient(90deg, ${colors.join(",")})`;
    const sign = (v) => (anom ? F.signed(v, 0) : fmtNum(v));
    $("lg-min").textContent = sign(lo);
    $("lg-mid").textContent = anom ? "0" : fmtNum((lo + hi) / 2);
    $("lg-max").textContent = sign(hi);
    $("st-mean").textContent = st ? `${fmtNum(st.mean)} ppb` : "–";
    $("st-max").textContent = st ? `${fmtNum(st.max)} ppb` : "–";
    $("st-cov").textContent = st ? `%${fmtNum(st.coverage * 100)}` : "%0";
    const ref = anom && st ? ` Anomali referansı: seçilen dönemde Türkiye kara hücrelerinin medyanı (${fmtNum(st.median, 1)} ppb).` : "";
    $("tropomi-meta").textContent = `Ürün: Sentinel-5P TROPOMI CH₄ L2 (S5P_L2__CH4____HiR, sürüm 2), methane_mixing_ratio_bias_corrected, qa_value > 0,5. `
      + `0,1° ızgara; TROPOMI pikseli yaklaşık 5,5 × 7 km, yakınlaştırmak çözünürlüğü artırmaz. Yalnızca merkezi Türkiye içinde kalan hücreler gösterilir; sınırda ekran görüntüsü maskeyle kırpılır, değerler değişmez.${ref} Sütun ortalamasıdır; tesis emisyonu değildir.`;
  }
  function renderSpark() {
    const svg = $("spark"), n = state.days.length;
    if (n < 2) { svg.innerHTML = ""; return; }
    const pts = state.days.map((d, k) => ({ k, v: d.mean })).filter((p) => p.v != null);
    const vs = pts.map((p) => p.v);
    const lo = Math.min(...vs) - 2, hi = Math.max(...vs) + 2;
    const X = (k) => 6 + (k / Math.max(1, n - 1)) * 288, Y = (v) => 46 - ((v - lo) / (hi - lo)) * 40;
    let h = "";
    state.days.forEach((d, k) => { if (d.coverage > 0) h += `<rect class="cov" x="${X(k) - 1}" y="${70 - d.coverage * 18}" width="2" height="${d.coverage * 18}"/>`; });
    if (state.mode !== "day" && state.dayIdx >= 0) {
      const [start] = range();
      const k0 = Math.max(0, state.days.findIndex((d) => d.day >= start));
      h += `<rect class="span" x="${X(k0)}" y="0" width="${Math.max(1, X(state.dayIdx) - X(k0))}" height="70"/>`;
    }
    h += `<polyline class="line" fill="none" points="${pts.map((p) => `${X(p.k)},${Y(p.v)}`).join(" ")}"/>`;
    const sel = pts.find((p) => p.k === state.dayIdx);
    if (sel) h += `<circle class="pt sel" cx="${X(sel.k)}" cy="${Y(sel.v)}" r="3.5"/>`;
    svg.innerHTML = h;
  }
  $("spark").addEventListener("click", (e) => {
    const n = state.days.length;
    if (!n) return;
    const box = $("spark").getBoundingClientRect();
    const target = ((((e.clientX - box.left) / box.width) * 300 - 6) / 288) * Math.max(1, n - 1);
    let best = -1;
    state.days.forEach((d, k) => { if (d.mean != null && (best < 0 || Math.abs(k - target) < Math.abs(best - target))) best = k; });
    if (best >= 0) { state.dayIdx = best; loadTropomi(); }
  });
  $("prev").onclick = () => { state.dayIdx--; loadTropomi(); };
  $("next").onclick = () => { state.dayIdx++; loadTropomi(); };
  function bindSeg(id, key, cb) {
    const set = (val) => [...$(id).children].forEach((x) => { const on = x.dataset[key] === val; x.classList.toggle("on", on); x.setAttribute("aria-pressed", String(on)); });
    $(id).addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; state[key] = b.dataset[key]; set(state[key]); cb(); });
    return set;
  }
  const setMode = bindSeg("mode", "mode", loadTropomi);
  const setView = bindSeg("view", "view", () => { if (state.data) renderCells(); updateCaption(); writeUrl(); });
  function setOpacity() {
    const v = Number($("t-opacity").value);
    map.setPaintProperty("cells", "fill-opacity", v / 100);
    $("t-op-val").textContent = `%${100 - v} saydam`;
  }
  $("t-opacity").addEventListener("input", () => { setOpacity(); writeUrl(); });
  function setTropomi(on) {
    $("show-tropomi").checked = on;
    $("tropomi-controls").hidden = !on;
    map.setLayoutProperty("cells", "visibility", on ? "visible" : "none");
    if (on && !state.data) loadTropomi(); else updateCaption();
    writeUrl();
  }
  $("show-tropomi").onchange = (e) => setTropomi(e.target.checked);
  async function loadDays() {
    try { state.days = await getJSON("/api/days"); } catch (e) { state.days = []; }
    if (!state.days.length) { state.dayIdx = -1; $("daylabel").textContent = "Henüz işlenmiş TROPOMI günü yok"; return; }
    let idx = initial.day ? state.days.findIndex((d) => d.day === initial.day) : -1;
    if (idx < 0) {
      idx = state.days.length - 1;
      for (let k = state.days.length - 1; k >= 0; k--) if (state.days[k].coverage >= 0.05) { idx = k; break; }
    }
    state.dayIdx = idx;
  }

  // ---- Uydu konumları (yalnızca Türkiye üzerindeyken) ve geçişler ----------------------------
  const satMarkers = {};
  async function loadPositions() {
    const on = $("show-sats").checked;
    map.setLayoutProperty("sat-ahead", "visibility", on ? "visible" : "none");
    if (!on) { Object.values(satMarkers).forEach((m) => m.remove()); return; }
    let r;
    try { r = await getJSON("/api/positions"); } catch (e) { return; }
    const feats = [];
    for (const [key, s] of Object.entries(r.satellites)) {
      if (!satMarkers[key]) {
        const el = document.createElement("div");
        el.className = "sat-marker";
        el.innerHTML = `<i style="background:${SAT_COLORS[key]}"></i>${esc(s.name)}`;
        satMarkers[key] = new maplibregl.Marker({ element: el, anchor: "left", offset: [-7, 0] });
      }
      satMarkers[key].setLngLat([s.lon, s.lat]);
      if (geo.contains(s.lon, s.lat)) satMarkers[key].addTo(map); else satMarkers[key].remove();
      feats.push({ type: "Feature", properties: { color: SAT_COLORS[key] }, geometry: splitLine(s.ahead) });
    }
    map.getSource("sat-ahead").setData({ type: "FeatureCollection", features: feats });
  }
  $("show-sats").onchange = () => { loadPositions(); writeUrl(); };
  function countdown(iso) {
    const m = Math.round((new Date(iso) - Date.now()) / 60000);
    if (m <= 0) return "şu an";
    const h = Math.floor(m / 60);
    return h ? `${h} sa ${m % 60} dk sonra` : `${m} dk sonra`;
  }
  function renderPasses() {
    const list = state.passes.slice(0, 8);
    $("passes").innerHTML = list.length ? list.map((p, k) => {
      const light = p.daylight ? '<span class="badge day">gündüz · ışık koşulu uygun</span>' : '<span class="badge night">gece · optik ölçüm için ışık yok</span>';
      return `<li class="pass${state.selPass === k ? " sel" : ""}" data-k="${k}" tabindex="0" role="button"><span class="t">${esc(fmtShort(p.closest))}</span><span class="cd">${countdown(p.start)}</span><span class="meta">${esc(p.name)} ${light}</span></li>`;
    }).join("") : '<li class="muted">72 saat içinde hesaplanan geçiş yok.</li>';
  }
  function selectPass(li) {
    const k = Number(li.dataset.k);
    state.selPass = state.selPass === k ? null : k;
    const p = state.passes[k];
    map.getSource("pass-track").setData(state.selPass == null ? empty() : { type: "FeatureCollection",
      features: [{ type: "Feature", properties: { color: SAT_COLORS[p.sat] }, geometry: splitLine(p.track) }] });
    renderPasses();
  }
  $("passes").addEventListener("click", (e) => { const li = e.target.closest("li.pass"); if (li) selectPass(li); });
  $("passes").addEventListener("keydown", (e) => { const li = e.target.closest("li.pass"); if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); selectPass(li); } });
  async function loadPasses() { try { state.passes = await getJSON("/api/passes"); } catch (e) { state.passes = []; } renderPasses(); }

  // ---- Durum paneli: bildirimler, gözlem fırsatları, veri durumu ----------------------------
  const ST_TABS = ["events", "passes", "sources"];
  function setStatusTab(t) {
    for (const k of ST_TABS) {
      const on = k === t;
      $(`st-tab-${k}`).setAttribute("aria-selected", String(on));
      $(`st-tab-${k}`).tabIndex = on ? 0 : -1;
      $(`st-${k}`).hidden = !on;
    }
  }
  for (const k of ST_TABS) $(`st-tab-${k}`).onclick = () => setStatusTab(k);
  const SOURCE_STATE = { ok: ["çalışıyor", "ok"], error: ["hata", "err"], pending: ["hazır, henüz denenmedi", "idle"], not_configured: ["bağlanmamış", "idle"] };
  const ts = (iso) => (iso ? fmtShort(iso) : "–");
  function renderSources(list) {
    $("sources").innerHTML = list.map((s) => {
      const [label, cls] = SOURCE_STATE[s.state] ?? [s.state, "idle"];
      const lines = [];
      if (s.state === "not_configured") lines.push("Sunucuda erişim bilgisi yok; bu kaynak kullanılmıyor.");
      else {
        lines.push(`Son başarılı alma: ${ts(s.last_ok)}`);
        if (s.id === "tropomi") lines.push(`Son gözlem günü: ${s.last_observation ? fmtDay(s.last_observation) : "–"}`);
        if (s.records != null) lines.push(`${s.records} kayıt (sağlayıcı kutusu; harita yalnızca Türkiye içini gösterir)`);
        if (s.state === "error") lines.push(`Son hata: ${F.ERROR_CLASS[s.error_class] || s.error_class || "belirtilmedi"}`);
      }
      return `<li><div class="src-head"><span class="src-name">${esc(s.name)}</span><span class="state ${cls}">${esc(label)}</span></div>${lines.map((l) => `<span class="src-line">${esc(l)}</span>`).join("")}</li>`;
    }).join("");
  }
  function eventHtml(a) {
    const btn = a.plumeId ? `<button class="linkish" type="button" data-open="${esc(a.plumeId)}">Kaydı aç</button>` : "";
    switch (a.kind) {
      case "new_observation":
      case "past_observation_published":
        return `<li class="info"><b>${a.kind === "new_observation" ? "Yeni gözlem" : "Geçmiş gözlem yeni yayımlandı"}</b><span>${esc(a.sensor ?? "")} · gözlem ${esc(ts(a.observedAt))} · alınma ${esc(ts(a.ingestedAt))} · ${esc(emissionText(a.emission))}</span><small>${esc(a.rule)}</small>${btn}</li>`;
      case "repeated_detection":
        return `<li class="info"><b>Tekrarlanan tespit</b><span>Kaynak kaydında ${a.plumeCount} bulut.</span><small>${esc(a.rule)}</small>${btn}</li>`;
      case "record_updated":
        return `<li class="info"><b>Kayıt güncellendi</b><span>${esc(a.plumeId)} · ${esc(ts(a.modifiedAt))}</span><small>${esc(a.rule)}</small>${btn}</li>`;
      case "regional_enhancement": {
        const b = a.block;
        return `<li class="review"><b>İncelenecek bölgesel artış (TROPOMI)</b><span>${fmtNum(b.south, 1)}–${fmtNum(b.north, 1)}° K, ${fmtNum(b.west, 1)}–${fmtNum(b.east, 1)}° D · ${a.days.length} gün</span><small>Kural ${esc(a.rule.id)}: ${esc(a.rule.reference)}; ${a.rule.windowDays} günde en az ${a.rule.minDays} gün. Doğrulanmış tesis kaçağı değildir.</small></li>`;
      }
      case "source_error":
        return `<li class="warn"><b>Veri güncellenemedi: ${esc(a.name)}</b><span>Neden: ${esc(F.ERROR_CLASS[a.errorClass] || a.errorClass)} · son başarılı alma ${esc(ts(a.lastOk))}</span></li>`;
      case "stale":
        return `<li class="warn"><b>Veri güncel değil: ${esc(a.name)}</b><span>Son başarılı alma: ${esc(ts(a.lastOk))}</span></li>`;
      default: return "";
    }
  }
  async function loadEvents() {
    try {
      const [st, ev] = await Promise.all([getJSON("/api/data-status"), getJSON("/api/monitoring-events")]);
      renderSources(st.sources);
      $("events-count").textContent = ev.items.length ? `(${ev.items.length})` : "";
      $("events-dot").hidden = !ev.items.length;
      $("st-events").innerHTML = ev.items.length ? ev.items.map(eventHtml).join("")
        : `<li class="muted">Son ${ev.windowDays} günde bildirim yok. Bu, yeni salım olmadığını göstermez.</li>`;
    } catch (e) { $("st-events").innerHTML = `<li class="muted">Bildirimler alınamadı: ${esc(e.message)}</li>`; }
  }
  $("st-events").addEventListener("click", (e) => { const b = e.target.closest("[data-open]"); if (b) openDetail(b.dataset.open, { fly: true, push: true }); });
  async function loadStatus() {
    try {
      const s = await getJSON("/api/status");
      $("status").textContent = s.tropomi_finished ? `TROPOMI son güncelleme: ${fmtShort(s.tropomi_finished)}` : "TROPOMI henüz güncellenmedi";
    } catch (e) { $("status").textContent = ""; }
  }

  // ---- Başlangıç ----------------------------------------------------------------------------
  async function init() {
    stateToForm();
    renderChips();
    setBase(state.base);
    $("t-opacity").value = Math.min(90, Math.max(10, initial.op));
    setOpacity();
    setMode(state.mode);
    setView(state.view);
    $("show-sats").checked = initial.sats;
    $("tab-obs").setAttribute("aria-selected", String(state.tab === "obs"));
    $("tab-src").setAttribute("aria-selected", String(state.tab === "src"));
    setResultsOpen(state.resultsOpen || MOBILE());
    if (!startCam) goHome(false);
    getJSON(CONTENT_URL).then((c) => { state.content = c; }).catch(() => { state.content = null; });
    await loadDays();
    setTropomi(initial.tropomi);
    await Promise.all([loadObservations(), loadSources(), loadAllForSearch(), loadPasses(), loadPositions(), loadStatus(), loadEvents()]);
    if (initial.sel) openDetail(initial.sel, { fly: !startCam });
    else if (initial.src) openSource(initial.src, { fly: !startCam });
    setInterval(loadPositions, 15000);
    setInterval(renderPasses, 60000);
    setInterval(loadPasses, 15 * 60000);
    setInterval(async () => {
      const had = state.days.length;
      await loadDays();
      if (state.days.length !== had) loadTropomi();
      loadStatus();
      loadEvents();
    }, 5 * 60000);
  }
})();
