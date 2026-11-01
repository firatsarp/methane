/* Harita sayfası: TROPOMI katmanı, gözlem filtreleri/listesi, detay çekmecesi, izleme bildirimleri, veri durumu. */
(function () {
  "use strict";
  const F = window.Fmt;
  const { esc, fmtNum, fmtLocal, fmtUtc, fmtShort, fmtDay, getJSON, latestOnly } = F;
  const $ = (id) => document.getElementById(id);

  const CONTENT_URL = "/content/metan-icerik.2026-11-01-general-3.json";
  const MAP_STATE_KEY = "metan.mapState";
  const PLASMA = ["#0d0887", "#5c01a6", "#9c179e", "#cc4778", "#ed7953", "#fdb42f", "#f0f921"];
  const DIVERGING = ["#2166ac", "#67a9cf", "#d1e5f0", "#f7f7f7", "#fddbc7", "#ef8a62", "#b2182b"];
  const SAT_COLORS = { s5p: "#5cc28a", iss: "#9cc7ff" };
  const SECTORS = {
    "1A1": "Enerji (elektrik / ısı)", "1B1a": "Kömür madenciliği", "1B2": "Petrol ve gaz", "4B": "Hayvancılık (gübre)",
    "6A": "Katı atık (depolama sahası)", "6B": "Atık su", unknown: "Sektör belirtilmemiş",
  };
  const PRODUCT = { OFFL: "Çevrimdışı işlenmiş ürün (OFFL)", NRTI: "Hızlı işlenmiş ürün (NRTI)" };
  const PLUME_IMG_ZOOM = 8;
  const PAGE = 30;
  const sectorName = (c) => SECTORS[c] ?? (c ? `IPCC ${c}` : SECTORS.unknown);

  // ---- Durum ve URL ----------------------------------------------------------------
  const state = {
    days: [], dayIdx: -1, mode: "day", view: "abs", data: null, passes: [], selPass: null,
    items: [], facets: null, total: 0, shown: PAGE, selected: null, sources: [], content: null,
  };
  const filterIds = { sector: "f-sector", sensor: "f-sensor", days: "f-period", sort: "f-sort" };

  function readUrl() {
    const q = new URLSearchParams(location.search);
    return {
      sector: q.get("sector") || "", sensor: q.get("sensor") || "", days: q.get("days") || "",
      sort: q.get("sort") || "observed", em: q.get("em") === "1", sel: q.get("sel"),
      day: q.get("day"), mode: q.get("mode") || "day", view: q.get("view") || "abs",
      tropomi: q.get("tropomi") !== "0", sources: q.get("sources") === "1",
      map: q.get("map"), src: q.get("src"),
    };
  }
  const initial = readUrl();

  function writeUrl() {
    const q = new URLSearchParams();
    for (const [k, id] of Object.entries(filterIds)) {
      const v = $(id).value;
      if (v && !(k === "sort" && v === "observed")) q.set(k, v);
    }
    if ($("f-emission").checked) q.set("em", "1");
    if (state.selected) q.set("sel", state.selected);
    const day = state.days[state.dayIdx]?.day;
    if (day) q.set("day", day);
    if (state.mode !== "day") q.set("mode", state.mode);
    if (state.view !== "abs") q.set("view", state.view);
    if (!$("show-tropomi").checked) q.set("tropomi", "0");
    if ($("show-sources").checked) q.set("sources", "1");
    if (window.mapReady) {
      const c = map.getCenter();
      q.set("map", `${map.getZoom().toFixed(2)}/${c.lat.toFixed(4)}/${c.lng.toFixed(4)}`);
    }
    const s = q.toString() ? `?${q}` : "";
    history.replaceState(null, "", location.pathname + s);
    try { sessionStorage.setItem(MAP_STATE_KEY, s); } catch (e) { /* oturum deposu kapalıysa yalnızca URL kullanılır */ }
  }

  // ---- Harita ----------------------------------------------------------------------
  const startView = (() => {
    const m = (initial.map || "").split("/").map(Number);
    return m.length === 3 && m.every(Number.isFinite) ? { zoom: m[0], center: [m[2], m[1]] } : null;
  })();
  const map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      sources: {
        base: { type: "raster", tileSize: 256, maxzoom: 16, attribution: "Harita © Esri, HERE, Garmin, © OpenStreetMap katkıcıları",
          tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"] },
        labels: { type: "raster", tileSize: 256, maxzoom: 16,
          tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}"] },
      },
      layers: [{ id: "base", type: "raster", source: "base" }, { id: "labels", type: "raster", source: "labels" }],
    },
    ...(startView || { bounds: [[26, 35.8], [44.8, 42.1]], fitBoundsOptions: { padding: 30 } }),
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
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
    for (const id of ["cells", "obs", "footprints", "sources", "pass-track", "sat-ahead", "selected"]) map.addSource(id, { type: "geojson", data: empty() });
    map.addSource("turkey", { type: "geojson", data: "/turkey.geojson" });
    map.addLayer({ id: "cells", type: "fill", source: "cells", paint: { "fill-color": "#000", "fill-opacity": 0.75 } });
    map.addLayer({ id: "turkey", type: "line", source: "turkey", paint: { "line-color": "#d7dde5", "line-width": 1, "line-opacity": 0.6 } });
    map.addLayer({ id: "sat-ahead", type: "line", source: "sat-ahead", paint: { "line-color": ["get", "color"], "line-width": 1, "line-opacity": 0.45, "line-dasharray": [2, 3] } });
    map.addLayer({ id: "pass-track", type: "line", source: "pass-track", paint: { "line-color": ["get", "color"], "line-width": 3, "line-opacity": 0.9 } });
    // EMIT bulut sınırı: sağlayıcının incelenmiş bulut kompleksi poligonu
    map.addLayer({ id: "footprints", type: "line", source: "footprints", minzoom: 9, paint: { "line-color": "#9cc7ff", "line-width": 1.5 } });
    map.addLayer({ id: "sources", type: "circle", source: "sources", paint: {
      "circle-radius": 9, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#c79bf2", "circle-stroke-width": 2 } });
    // Tespit noktası: sabit boyut; daire bulutun sınırı veya büyüklüğü değildir
    map.addLayer({ id: "obs", type: "circle", source: "obs", paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 4, 10, 6],
      "circle-color": ["case", ["get", "hasEmission"], "#ed7953", "#8b95a3"],
      "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.5,
      "circle-stroke-opacity": ["interpolate", ["linear"], ["zoom"], PLUME_IMG_ZOOM, 1, PLUME_IMG_ZOOM + 3, 0.5] } });
    map.addLayer({ id: "selected", type: "circle", source: "selected", paint: {
      "circle-radius": 11, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#ffffff", "circle-stroke-width": 3 } });
    map.moveLayer("labels", "sat-ahead");

    map.on("click", "cells", (e) => {
      if (map.queryRenderedFeatures(e.point, { layers: ["obs", "sources"] }).length) return;
      const p = e.features[0].properties;
      const v = state.view === "anom" ? `${F.signed(p.v, 1)} ppb (referansa göre)` : `${fmtNum(p.v, 1)} ppb`;
      new maplibregl.Popup().setLngLat(e.lngLat).setHTML(`<div class="popup"><b>${esc(v)}</b><table>
        <tr><td>Mutlak</td><td>${fmtNum(p.abs, 1)} ppb</td></tr><tr><td>Piksel sayısı</td><td>${p.n}</td></tr>
        <tr><td>Hücre merkezi</td><td>${fmtNum(p.lat, 2)}° K, ${fmtNum(p.lon, 2)}° D</td></tr></table></div>`).addTo(map);
    });
    map.on("click", "obs", (e) => openDetail(e.features[0].properties.id, { focus: true }));
    map.on("click", "sources", (e) => openSource(e.features[0].properties.id));
    for (const id of ["cells", "obs", "sources"]) {
      map.on("mouseenter", id, () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", id, () => (map.getCanvas().style.cursor = ""));
    }
    map.on("moveend", () => { addPlumeImages(); writeUrl(); });
    window.mapReady = true;
    init();
  });

  // ---- TROPOMI ----------------------------------------------------------------------
  function range() {
    const end = state.days[state.dayIdx].day;
    if (state.mode === "day") return [end, end];
    const d = new Date(end + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() - (Number(state.mode) - 1));
    return [d.toISOString().slice(0, 10), end];
  }

  const tropomiReq = latestOnly();
  async function loadTropomi() {
    if (state.dayIdx < 0) return;
    const [start, end] = range();
    const day = state.days[state.dayIdx];
    $("daylabel").innerHTML = state.mode === "day"
      ? `${fmtDay(end)}<small>${esc(PRODUCT[day.source] || "Bu gün için ölçüm yok")}</small>`
      : `${fmtDay(start)} – ${fmtDay(end)}<small>${state.mode} günlük ortalama · her hücre o dönemdeki geçerli piksellerin ortalaması</small>`;
    $("prev").disabled = state.dayIdx <= 0;
    $("next").disabled = state.dayIdx >= state.days.length - 1;
    const req = tropomiReq();
    try {
      const data = await getJSON(`/api/tropomi?start=${start}&end=${end}`, { signal: req.signal });
      if (!req.current()) return;
      state.data = data;
      renderCells();
    } catch (e) {
      if (!e.aborted) $("tropomi-meta").textContent = `TROPOMI verisi alınamadı: ${e.message}. Haritadaki son çizim korunuyor.`;
    }
    renderSpark();
    writeUrl();
  }

  function renderCells() {
    const d = state.data, st = d.stats, anom = state.view === "anom";
    let lo, hi, colors;
    if (!st) { lo = 1850; hi = 1950; colors = PLASMA; }
    else if (anom) {
      const m = Math.max(10, Math.ceil(Math.max(st.median - st.p02, st.p98 - st.median) / 5) * 5);
      lo = -m; hi = m; colors = DIVERGING;
    } else {
      lo = Math.floor(st.p02); hi = Math.ceil(st.p98); colors = PLASMA;
      if (hi - lo < 10) { lo -= 5; hi += 5; }
    }
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
    $("lg-mid").textContent = anom ? "0 ppb" : `${fmtNum((lo + hi) / 2)} ppb`;
    $("lg-max").textContent = sign(hi);
    $("st-mean").textContent = st ? `${fmtNum(st.mean)} ppb` : "–";
    $("st-max").textContent = st ? `${fmtNum(st.max)} ppb` : "–";
    $("st-cov").textContent = st ? `%${fmtNum(st.coverage * 100)}` : "%0";
    const ref = anom && st ? ` Anomali referansı: seçilen dönemde Türkiye kara hücrelerinin medyanı (${fmtNum(st.median, 1)} ppb).` : "";
    $("tropomi-meta").textContent = `Ürün: Sentinel-5P TROPOMI CH₄ L2 (S5P_L2__CH4____HiR, sürüm 2), değişken methane_mixing_ratio_bias_corrected, kalite filtresi qa_value > 0,5. `
      + `Gösterim 0,1° ızgara (yaklaşık 9 × 11 km); TROPOMI pikseli yaklaşık 5,5 × 7 km'dir, yakınlaştırmak çözünürlüğü artırmaz. `
      + `Kapsama: geçerli ölçüm içeren kara hücrelerinin oranı.${ref} Sütun ortalamasıdır; yer seviyesi konsantrasyonu veya tesis emisyonu değildir.`;
  }

  function renderSpark() {
    const svg = $("spark");
    const n = state.days.length;
    if (n < 2) { svg.innerHTML = ""; return; }
    const pts = state.days.map((d, k) => ({ k, v: d.mean, c: d.coverage })).filter((p) => p.v != null);
    const vs = pts.map((p) => p.v);
    const lo = Math.min(...vs) - 2, hi = Math.max(...vs) + 2;
    const X = (k) => 6 + (k / Math.max(1, n - 1)) * 288;
    const Y = (v) => 46 - ((v - lo) / (hi - lo)) * 40;
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
    const set = (val) => [...$(id).children].forEach((x) => {
      const on = x.dataset[key] === val;
      x.classList.toggle("on", on);
      x.setAttribute("aria-pressed", String(on));
    });
    $(id).addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      state[key] = b.dataset[key];
      set(state[key]);
      cb();
    });
    return set;
  }
  const setMode = bindSeg("mode", "mode", loadTropomi);
  const setView = bindSeg("view", "view", () => { if (state.data) renderCells(); writeUrl(); });
  $("show-tropomi").onchange = (e) => { map.setLayoutProperty("cells", "visibility", e.target.checked ? "visible" : "none"); writeUrl(); };

  async function loadDays(keep) {
    const prev = keep ? state.days[state.dayIdx]?.day : initial.day;
    state.days = await getJSON("/api/days");
    if (!state.days.length) { state.dayIdx = -1; $("daylabel").textContent = "Henüz işlenmiş TROPOMI günü yok"; return; }
    let idx = prev ? state.days.findIndex((d) => d.day === prev) : -1;
    if (idx < 0) {
      idx = state.days.length - 1;
      for (let k = state.days.length - 1; k >= 0; k--) if (state.days[k].coverage >= 0.05) { idx = k; break; }
    }
    state.dayIdx = idx;
  }

  // ---- Gözlemler ---------------------------------------------------------------------
  const emissionText = (e) => (e && F.num(e.value)
    ? `${fmtNum(e.value)} kg CH₄/sa` + (e.uncertainty && F.num(e.uncertainty.value) ? ` ± ${fmtNum(e.uncertainty.value)}` : "")
    : "Tahmin yok");

  function fillSelect(id, values, current) {
    const sel = $(id);
    sel.length = 1;
    for (const [v, label] of values) sel.add(new Option(label, v));
    sel.value = values.some(([v]) => v === current) ? current : "";
  }

  function query() {
    const q = new URLSearchParams({ limit: "2000" });
    if ($("f-sector").value) q.set("sector", $("f-sector").value);
    if ($("f-sensor").value) q.set("sensor", $("f-sensor").value);
    if ($("f-period").value) q.set("days", $("f-period").value);
    q.set("sort", $("f-sort").value);
    if ($("f-emission").checked) q.set("emission_only", "true");
    return q;
  }

  const obsReq = latestOnly();
  async function loadObservations() {
    const req = obsReq();
    $("obs-summary").textContent = "Yükleniyor…";
    try {
      const r = await getJSON(`/api/observations?${query()}`, { signal: req.signal });
      if (!req.current()) return;
      state.items = r.items;
      state.total = r.total;
      if (!state.facets) {
        state.facets = r.facets;
        fillSelect("f-sector", r.facets.sectors.map((s) => [s, sectorName(s)]).sort((a, b) => a[1].localeCompare(b[1], "tr")), initial.sector);
        fillSelect("f-sensor", r.facets.sensors.map((s) => [s, s]), initial.sensor);
        if ($("f-sector").value !== initial.sector || $("f-sensor").value !== initial.sensor) return loadObservations();
      }
      state.shown = PAGE;
      renderObservations();
    } catch (e) {
      if (e.aborted) return;
      $("obs-summary").textContent = `Gözlemler alınamadı: ${e.message}.`;
      if (state.items.length) $("obs-summary").textContent += " Son alınan liste gösteriliyor.";
    }
    writeUrl();
  }

  function renderObservations() {
    const list = state.items;
    const withE = list.filter((o) => o.emission).length;
    const period = $("f-period").value;
    $("obs-summary").textContent = `${list.length} / ${state.total} gözlem · ${withE} kayıtta emisyon tahmini`;
    $("tab-count").textContent = `(${list.length})`;
    const rng = state.facets?.observedRange;
    const span = rng ? `${new Date(rng[0]).getUTCFullYear()}–${new Date(rng[1]).getUTCFullYear()}` : "–";
    const tro = state.days[state.dayIdx]?.day;
    $("obs-range").textContent = (period ? `Bulut gözlemleri: son ${period} gün (gözlem zamanına göre)` : `Bulut gözlemleri: tüm dönem (${span})`)
      + (tro ? ` · TROPOMI katmanı: ${fmtDay(tro)}${state.mode !== "day" ? ` ile biten ${state.mode} gün` : ""}` : "");
    map.getSource("obs").setData({ type: "FeatureCollection", features: list.map((o) => ({
      type: "Feature", geometry: o.geometry, properties: { id: o.id, hasEmission: !!o.emission } })) });
    map.getSource("footprints").setData({ type: "FeatureCollection", features: list.filter((o) => o.footprint).map((o) => ({
      type: "Feature", geometry: o.footprint, properties: { id: o.id } })) });
    syncPlumeImages();
    renderList();
  }

  function renderList() {
    const items = state.items.slice(0, state.shown);
    $("obs-list").innerHTML = items.length ? items.map((o) => {
      const when = o.observedAt ? fmtShort(o.observedAt) : "Gözlem zamanı doğrulanmadı";
      const paths = o.accessPaths.length > 1 ? `<span class="tag">${o.accessPaths.length} erişim yolu</span>` : "";
      return `<li><button class="obs-item${state.selected === o.id ? " sel" : ""}" data-id="${esc(o.id)}">
        <span class="oi-top"><span class="oi-date">${esc(when)}</span><span class="oi-emi">${esc(emissionText(o.emission))}</span></span>
        <span class="oi-meta">${esc(o.sensor ?? "Sensör belirtilmemiş")} · ${esc(o.provider)} · ${esc(sectorName(o.sectorCode))}${paths}</span>
      </button></li>`;
    }).join("") : `<li class="muted">Bu filtrelerle gözlem yok. Bu, metan olmadığı anlamına gelmez.</li>`;
    $("obs-more").hidden = state.items.length <= state.shown;
  }

  $("obs-list").addEventListener("click", (e) => {
    const b = e.target.closest("button.obs-item");
    if (b) openDetail(b.dataset.id, { fly: true, opener: b, focus: true });
  });
  $("obs-more").onclick = () => { state.shown += PAGE; renderList(); };
  for (const id of ["f-sector", "f-sensor", "f-period", "f-sort", "f-emission"]) $(id).addEventListener("change", loadObservations);
  $("show-plumes").onchange = (e) => {
    for (const id of ["obs", "footprints"]) map.setLayoutProperty(id, "visibility", e.target.checked ? "visible" : "none");
    syncPlumeImages();
  };

  // CSV: listede ve haritada görünen filtrelenmiş kümenin aynısı
  $("download").onclick = () => {
    const cols = ["id", "provider", "sensor", "observedAt", "publishedAt", "ingestedAt", "lat", "lon", "sectorCode",
      "emission_kg_h", "uncertainty_kg_h", "processingVersion", "sourceId", "accessPaths", "sourceUrl"];
    const cell = (v) => (v == null ? "" : /[",\n;]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
    const rows = state.items.map((o) => [o.id, o.provider, o.sensor, o.observedAt, o.publishedAt, o.ingestedAt, o.lat, o.lon,
      o.sectorCode, o.emission?.value, o.emission?.uncertainty?.value, o.processingVersion, o.sourceId,
      o.accessPaths.map((a) => `${a.provider}:${a.id}`).join(" | "), o.sourceUrl].map(cell).join(","));
    const blob = new Blob(["﻿" + [cols.join(","), ...rows].join("\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `metan-gozlemleri-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };

  // Sağlayıcı artış görüntüleri yalnızca yakınlaştırınca ve görünen alandakiler için yüklenir
  const plumeImages = new Set();
  function syncPlumeImages() {
    const on = $("show-plumes").checked;
    const visible = new Set(state.items.map((o) => o.id));
    plumeImages.forEach((id) => map.setLayoutProperty(`img-${id}`, "visibility", on && visible.has(id) ? "visible" : "none"));
    addPlumeImages();
  }
  function addPlumeImages() {
    if (!$("show-plumes").checked || map.getZoom() < PLUME_IMG_ZOOM) return;
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

  // ---- Kaynak özetleri -----------------------------------------------------------------
  async function loadSources() {
    try {
      const r = await getJSON("/api/source-summaries");
      state.sources = r.items;
      map.getSource("sources").setData({ type: "FeatureCollection", features: r.items.map((s) => ({
        type: "Feature", geometry: { type: "Point", coordinates: [s.lon, s.lat] }, properties: { id: s.id } })) });
    } catch (e) { state.sources = []; }
  }
  const setSourcesVisible = (on) => map.setLayoutProperty("sources", "visibility", on ? "visible" : "none");
  $("show-sources").onchange = (e) => { setSourcesVisible(e.target.checked); writeUrl(); };

  // ---- Detay çekmecesi -------------------------------------------------------------------
  const detailReq = latestOnly();
  let opener = null;

  function showDrawer() {
    const d = $("detail");
    if (d.hidden) { d.hidden = false; requestAnimationFrame(() => map.resize()); }
    d.scrollTop = 0;
  }

  function kv(rows) {
    return `<dl class="kv">${rows.filter(Boolean).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
  }
  const timeCell = (iso) => (iso ? `${esc(fmtLocal(iso))}<small>${esc(fmtUtc(iso))}</small>` : F.notProvided);
  const uncertaintyText = (e) => {
    const u = e?.uncertainty;
    if (u && F.num(u.lower) && F.num(u.upper)) return `${fmtNum(u.lower)}–${fmtNum(u.upper)} kg CH₄/sa · ${esc(u.label || "istatistiksel tanımı kayıtta belirtilmemiş")}`;
    if (u && F.num(u.value)) return `± ${fmtNum(u.value)} kg CH₄/sa · ${esc(u.label || "sağlayıcının simetrik belirsizliği; istatistiksel tanımı kayıtta belirtilmemiş")}`;
    return F.notProvided;
  };

  async function openDetail(id, { fly = false, opener: op = null, focus = false } = {}) {
    const req = detailReq();
    state.selected = id;
    opener = op || document.activeElement;
    renderList();
    $("detail-kind").textContent = "Metan bulutu gözlemi";
    $("detail-title").textContent = "Yükleniyor…";
    $("detail-sub").textContent = "";
    $("detail-body").innerHTML = "";
    showDrawer();
    if (matchMedia("(max-width: 860px)").matches) setTab("map");
    let r;
    try {
      r = await getJSON(`/api/observations/${encodeURIComponent(id)}`, { signal: req.signal });
    } catch (e) {
      if (!e.aborted && req.current()) $("detail-title").textContent = `Gözlem açılamadı: ${e.message}`;
      return;
    }
    if (!req.current()) return;
    const o = r.observation;
    const [lon, lat] = o.geometry.coordinates;
    map.getSource("selected").setData({ type: "FeatureCollection", features: [{ type: "Feature", geometry: o.geometry, properties: {} }] });
    if (fly) map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), PLUME_IMG_ZOOM) });
    $("detail-title").textContent = `${o.sensor ?? "Sensör belirtilmemiş"} · ${o.observedAt ? new Date(o.observedAt).getUTCFullYear() : "yıl doğrulanmadı"}`;
    $("detail-sub").textContent = `Özgün kayıt kimliği: ${o.providerId || o.id} · ${o.locationLabel}`;
    $("detail-body").innerHTML = detailHtml(o, r.sourceSummary, r.relatedObservations);
    if (focus) $("detail-title").focus();
    writeUrl();
    loadWeather(o, req);
    loadSurface(o, req);
  }

  function detailHtml(o, src, related) {
    const e = o.emission;
    const alt = (o.alternateEstimates || []).map((a) => `<li>${esc(a.provider)} (${esc(a.id)}): ${a.emission ? esc(emissionText(a.emission)) : "emisyon tahmini yayımlanmamış"}${a.maxEnhancement?.value != null ? ` · en yüksek artış ${fmtNum(a.maxEnhancement.value)} ${esc(a.maxEnhancement.unit)}` : ""}${a.processingVersion ? ` · ${esc(a.processingVersion)}` : ""}</li>`).join("");
    const access = o.accessPaths.map((a) => `<li><a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.provider)} — ${esc(a.id)} ↗</a>${a.kind ? ` <small class="muted">${esc(a.kind)}</small>` : ""}</li>`).join("");
    const attribution = src
      ? `Carbon Mapper bu bulutu ${esc(String(src.clusterRadiusM ?? "?"))} m kümeleme yarıçaplı kaynak kaydına bağlıyor (${esc(src.providerId)}). Bu sağlayıcı kümelemesidir; tesis doğrulaması değildir.`
      : "Bu kayıt için sağlayıcı kaynak kümelemesi veya doğrulanmış tesis eşleştirmesi yok.";
    return `
      <section class="dsec"><h3>Zaman</h3>${kv([
        ["Gözlem", timeCell(o.observedAt)],
        ["Yayımlanma", timeCell(o.publishedAt)],
        ["Sisteme alınma", timeCell(o.ingestedAt)],
      ])}<p class="note">Gözlem yılı sağlayıcının tarih alanından alınır; kayıt kimliğinden türetilmez.</p></section>
      <section class="dsec"><h3>Emisyon tahmini (bu gözlem)</h3>
        ${e ? `<p class="big">${fmtNum(e.value)} kg CH₄/saat</p>${kv([["Belirsizlik", uncertaintyText(e)], ["Sağlayıcı", esc(o.provider)]])}`
          : `<div class="status-box">Sağlayıcı bu gözlem için emisyon tahmini yayımlamadı. Bu, sıfır emisyon anlamına gelmez.</div>`}
        ${alt ? `<p class="note">Aynı gözlemin diğer erişim yolundaki tahmin (bağımsız doğrulama değildir):</p><ul class="note">${alt}</ul>` : ""}
        ${o.mergeNote ? `<p class="note">${esc(o.mergeNote)}</p>` : ""}
      </section>
      <section class="dsec"><h3>Kayıt</h3>${kv([
        ["Sensör / platform", `${esc(o.sensor ?? "–")}${o.platform ? ` <small>${esc(o.platform)}</small>` : ""}`],
        ["Sağlayıcı", esc(o.provider)],
        ["Sektör", esc(sectorName(o.sectorCode))],
        ["İşlem sürümü", esc(o.processingVersion || F.notProvided)],
        o.gsdM ? ["Yer örneklemesi", `${fmtNum(o.gsdM)} m`] : null,
        ["Kalite", esc(o.qualityNote || "Sağlayıcı kalite alanı boş")],
        ["Kaynak eşleştirmesi", esc(attribution)],
        ["Görüntü", o.raster ? "Sağlayıcının artış görüntüsü haritada katman olarak (yakınlaştırın). Renk ölçeğinin sayısal lejantı API yanıtında yok." : o.footprint ? "NASA EMIT bulut sınırı haritada çizgi olarak (yakınlaştırın)." : "Bu kayıtta görüntü yok; haritadaki işaret yalnızca tespit noktasıdır."],
      ])}</section>
      <section class="dsec" id="d-weather"><h3>Gözlem anına ait meteoroloji</h3><div class="status-box">Yükleniyor…</div></section>
      ${src ? sourceHtml(src, related) : ""}
      <section class="dsec" id="d-surface"><h3>Yüzey görüntüsü</h3><div class="status-box">Yükleniyor…</div></section>
      <section class="dsec understand"><h3>Bu ölçümü anlamak</h3>${understandHtml()}</section>
      <section class="dsec"><h3>Özgün kaydı aç</h3><ul class="related">${access}</ul>
        <div class="actions"><button class="btn" type="button" id="d-zoom">Haritada yakınlaştır</button>
        <a class="btn" href="/hava-ve-iklim.html?obs=${encodeURIComponent(o.id)}">Hava ve İklim'de aç</a></div></section>`;
  }

  function sourceHtml(src, related) {
    const dm = src.detectedMean, pw = src.persistenceWeighted;
    const rel = related.length
      ? `<ul class="related">${related.map((x) => `<li><button class="obs-item" data-open="${esc(x.id)}"><span class="oi-top"><span class="oi-date">${esc(fmtShort(x.observedAt) || "zaman yok")}</span><span class="oi-emi">${esc(emissionText(x.emission))}</span></span><span class="oi-meta">${esc(x.sensor ?? "")}</span></button></li>`).join("")}</ul>`
      : `<p class="note">Bu kaynak kaydında listede gösterilebilen başka gözlem yok.</p>`;
    return `<section class="dsec"><h3>Kaynak özeti (Carbon Mapper) — tek bulut değildir</h3>${kv([
      ["Kaynak kaydı", esc(src.providerId)],
      ["Dönem", `${esc(src.firstObserved || "?")} – ${esc(src.lastObserved || "?")}`],
      ["Gözlem / tespit tarihi", `${fmtNum(src.observationDateCount)} / ${fmtNum(src.detectionDateCount)} <small>Sağlayıcının saydığı uygun gözlem ve tespit tarihleri</small>`],
      ["Tespitlerin ortalaması", dm ? `${fmtNum(dm.value)} kg/sa${dm.uncertainty ? ` ± ${fmtNum(dm.uncertainty.value)}` : ""}` : F.notProvided],
      ["Süreklilikle ağırlıklı ortalama", pw ? `${fmtNum(pw.value)} kg/sa <small>Sağlayıcı alanı: tespit ortalaması × (tespit tarihi / gözlem tarihi)</small>` : F.notProvided],
    ])}<p class="note">Kaynak ortalaması bir dönemi özetler; bu bulutun kendi tahmini yukarıdadır. Aynı kaynağın diğer kayıtları:</p>${rel}</section>`;
  }

  function understandHtml() {
    const g = state.content?.glossary;
    if (!g) return `<p><a href="/veriyi-anlamak.html">Veriyi Anlamak</a> sayfasında gözlem zamanı, birimler ve belirsizlik açıklanır.</p>`;
    const pick = ["emisyon", "belirsizlik", "gecikme", "ruzgar"].map((id) => g.find((x) => x.id === id)).filter(Boolean);
    return pick.map((x) => `<p><b>${esc(x.title)}.</b> ${esc(x.text)}</p>`).join("")
      + `<p><a href="/uydular-nasil-olcer.html#kavramlar">Tüm kavramlar</a> · <a href="/veriyi-anlamak.html">Veriyi Anlamak</a></p>`;
  }

  $("detail-body").addEventListener("click", (e) => {
    const b = e.target.closest("[data-open]");
    if (b) { openDetail(b.dataset.open, { fly: true, focus: true }); return; }
    if (e.target.id === "d-zoom") {
      const it = state.items.find((x) => x.id === state.selected);
      const c = it?.geometry?.coordinates;
      if (c) map.flyTo({ center: c, zoom: Math.max(map.getZoom(), PLUME_IMG_ZOOM + 3) });
    }
  });

  async function loadWeather(o, req) {
    const box = () => document.getElementById("d-weather");
    try {
      const w = await getJSON(`/api/weather/observation/${encodeURIComponent(o.id)}`, { signal: req.signal });
      if (!req.current() || !box()) return;
      box().innerHTML = `<h3>Gözlem anına ait meteoroloji</h3>${weatherHtml(w)}`;
    } catch (e) {
      if (!e.aborted && req.current() && box()) box().innerHTML = `<h3>Gözlem anına ait meteoroloji</h3><div class="status-box">Meteoroloji alınamadı: ${esc(e.message)}. Diğer bilgiler etkilenmedi.</div>`;
    }
  }

  function weatherHtml(w) {
    const p = w.primary;
    const job = w.cdsJob;
    const jobLine = job ? `<p class="note">CDS ERA5 işi: ${esc(F.STATUS_TEXT[job.state] || job.state)}${job.remoteId ? ` · iş kimliği ${esc(job.remoteId)}` : ""}${job.errorClass ? ` · ${esc(F.ERROR_CLASS[job.errorClass] || job.errorClass)}` : ""}</p>` : "";
    if (!p) {
      const msg = w.status === "empty"
        ? "Gözlem anına ait meteoroloji henüz yok. ERA5 yeniden analizi birkaç gün gecikmeyle yayımlanır; güncel tahmin bu gözleme bağlanmaz."
        : w.status === "unavailable" ? `Meteoroloji şu an alınamıyor (${esc(F.ERROR_CLASS[w.error?.errorClass] || "bilinmeyen neden")}).` : esc(w.message || F.STATUS_TEXT[w.status] || w.status);
      return `<div class="status-box">${msg}</div>${jobLine}`;
    }
    const v = p.variables || {};
    const val = (k, d = 1) => (v[k] && F.num(v[k].value) ? `${fmtNum(v[k].value, d)} ${esc(v[k].unit || "")}` : "–");
    const temp = v.temperature_2m ? val("temperature_2m") : val("t2m");
    const rh = v.relative_humidity_2m ? val("relative_humidity_2m", 0) : val("rh", 0);
    const pres = v.surface_pressure ? val("surface_pressure", 0) : val("sp", 0);
    const cloud = v.cloud_cover ? val("cloud_cover", 0) : val("tcc", 0);
    const prec = v.precipitation ? val("precipitation") : val("tp");
    const blh = v.boundary_layer_height ? val("boundary_layer_height", 0) : val("blh", 0);
    const off = p.timeOffsetMinutes;
    return `
      <div class="wind-row">${F.windArrow(p.wind10?.to)}<div><b>10 m rüzgâr</b><br>${esc(F.windText(p.wind10))}</div></div>
      ${p.wind100 ? `<div class="wind-row">${F.windArrow(p.wind100?.to)}<div><b>100 m rüzgâr</b><br>${esc(F.windText(p.wind100))}</div></div>` : ""}
      <dl class="wx-main">
        <div><dt>Sıcaklık (2 m)</dt><dd>${temp}</dd></div>
        <div><dt>Bulutluluk</dt><dd>${cloud}</dd></div>
        <div><dt>Yağış (önceki 1 sa)</dt><dd>${prec}</dd></div>
        <div><dt>Bağıl nem</dt><dd>${rh}</dd></div>
        <div><dt>Yüzey basıncı</dt><dd>${pres}</dd></div>
        <div><dt>Sınır tabakası</dt><dd>${blh}</dd></div>
      </dl>
      ${kv([
        ["Meteoroloji saati", `${esc(fmtLocal(p.validAt) || "–")}<small>${esc(fmtUtc(p.validAt) || "")} · gözlemden ${F.num(off) ? `${off > 0 ? "+" : ""}${off} dk` : "?"}</small>`],
        ["Kaynak yolu", `${esc(p.accessPath)} <small>${esc(p.dataset)} · ${esc(p.kind === "reanalysis" ? "yeniden analiz" : p.kind)} · ${esc(p.temporalResolution)} · ${esc(p.spatialResolution)}</small>`],
        ["Izgara noktası", `${fmtNum(p.resolvedGridPoint?.lat, 2)}° K, ${fmtNum(p.resolvedGridPoint?.lon, 2)}° D`],
        ["Alınma", esc(fmtLocal(p.fetchedAt) || "–")],
      ])}
      <p class="note">Ok, rüzgârın taşıdığı yönü gösterir (yukarı = kuzey). Yön derecesi rüzgârın geldiği yöndür. Rüzgâr bilgisi kaynak ataması veya yeni emisyon hesabı için kullanılmaz.${p.attribution ? ` ${esc(p.attribution)}.` : ""}</p>
      ${jobLine}`;
  }

  async function loadSurface(o, req) {
    const box = () => document.getElementById("d-surface");
    try {
      const s = await getJSON(`/api/imagery/${encodeURIComponent(o.id)}`, { signal: req.signal });
      if (!req.current() || !box()) return;
      let html;
      if (s.status === "ready") {
        html = `<figure class="surface"><img src="${esc(s.imageUrl)}" alt="Sentinel-2 L2A doğal renkli yüzey görüntüsü, sahne tarihi ${esc(s.scene.datetime)}" loading="lazy">
          <figcaption class="note">Yüzey görüntüsü · ${esc(s.collection)} · sahne ${esc(fmtLocal(s.scene.datetime))} · bulut %${fmtNum(s.scene.cloudCover)} · ${esc(s.note)} Kaynak: ${esc(s.provider)}.</figcaption></figure>`;
      } else if (s.status === "not_configured") {
        html = `<div class="status-box">Yüzey görüntüsü erişimi sunucuda tanımlı değil (Copernicus Data Space).</div>`;
      } else if (s.status === "empty") {
        html = `<div class="status-box">${esc(s.message)}</div>`;
      } else if (s.status === "queued") {
        html = `<div class="status-box">Yüzey görüntüsü hazırlanıyor (iş: ${esc(s.job?.state || "kuyrukta")}). Birkaç dakika sonra yeniden açın.</div>`;
      } else {
        html = `<div class="status-box">Yüzey görüntüsü alınamadı (${esc(F.ERROR_CLASS[s.errorClass] || s.errorClass || "bilinmeyen neden")}).</div>`;
      }
      box().innerHTML = `<h3>Yüzey görüntüsü</h3>${html}`;
    } catch (e) {
      if (!e.aborted && req.current() && box()) box().innerHTML = `<h3>Yüzey görüntüsü</h3><div class="status-box">Alınamadı: ${esc(e.message)}</div>`;
    }
  }

  async function openSource(id) {
    const req = detailReq();
    state.selected = null;
    renderList();
    $("detail-kind").textContent = "Kaynak özeti (Carbon Mapper)";
    $("detail-title").textContent = "Yükleniyor…";
    $("detail-sub").textContent = "";
    $("detail-body").innerHTML = "";
    showDrawer();
    try {
      const r = await getJSON(`/api/source-summaries/${encodeURIComponent(id)}`, { signal: req.signal });
      if (!req.current()) return;
      const s = r.source;
      $("detail-title").textContent = `${sectorName(s.sectorCode)} · ${s.locationLabel}`;
      $("detail-sub").textContent = `Sağlayıcı kaynak kaydı: ${s.providerId}`;
      $("detail-body").innerHTML = sourceHtml(s, r.observations)
        + `<section class="dsec"><p class="note">Koordinat sağlayıcının küme merkezidir; tesis adı veya sınırı atanmamıştır.</p>
           <div class="actions"><a class="btn" href="${esc(s.sourceUrl)}" target="_blank" rel="noopener">Carbon Mapper'da aç ↗</a>
           <a class="btn" href="/hava-ve-iklim.html?lat=${s.lat.toFixed(4)}&lon=${s.lon.toFixed(4)}">Bu konumun hava ve iklimi</a></div></section>`;
      $("detail-title").focus();
    } catch (e) {
      if (!e.aborted && req.current()) $("detail-title").textContent = `Kaynak özeti açılamadı: ${e.message}`;
    }
  }

  function closeDetail() {
    if ($("detail").hidden) return;
    detailReq();
    $("detail").hidden = true;
    state.selected = null;
    map.getSource("selected").setData(empty());
    requestAnimationFrame(() => map.resize());
    renderList();
    writeUrl();
    const back = opener?.dataset?.id && document.querySelector(`.obs-item[data-id="${CSS.escape(opener.dataset.id)}"]`);
    (back || opener)?.focus?.();
  }
  $("detail-close").onclick = closeDetail;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !document.querySelector(".mobile-menu[open]")) closeDetail(); });

  // ---- Telefon sekmeleri --------------------------------------------------------------
  function setTab(which) {
    $("app").dataset.view = which;
    $("tab-map").setAttribute("aria-selected", String(which === "map"));
    $("tab-list").setAttribute("aria-selected", String(which === "list"));
    if (which === "map") requestAnimationFrame(() => map.resize());
  }
  $("tab-map").onclick = () => setTab("map");
  $("tab-list").onclick = () => setTab("list");
  setTab("map");

  // ---- Geçişler ve uydu konumları ------------------------------------------------------
  function countdown(iso) {
    const m = Math.round((new Date(iso) - Date.now()) / 60000);
    if (m <= 0) return "şu an";
    const h = Math.floor(m / 60);
    return h ? `${h} sa ${m % 60} dk sonra` : `${m} dk sonra`;
  }
  function renderPasses() {
    const list = state.passes.slice(0, 8);
    $("passes").innerHTML = list.length ? list.map((p, k) => {
      const light = p.daylight
        ? `<span class="badge day">gündüz · ışık koşulu uygun gözlem fırsatı</span>`
        : `<span class="badge night">gece · optik ölçüm için ışık yok</span>`;
      return `<li class="pass${state.selPass === k ? " sel" : ""}" data-k="${k}" tabindex="0" role="button">
        <span class="t">${esc(F.fmtShort(p.closest))}</span><span class="cd">${countdown(p.start)}</span>
        <span class="meta">${esc(p.name)} ${light}</span></li>`;
    }).join("") : `<li class="muted">72 saat içinde hesaplanan geçiş yok.</li>`;
  }
  function selectPass(li) {
    const k = Number(li.dataset.k);
    state.selPass = state.selPass === k ? null : k;
    const p = state.passes[k];
    map.getSource("pass-track").setData(state.selPass == null ? empty() : { type: "FeatureCollection",
      features: [{ type: "Feature", properties: { color: SAT_COLORS[p.sat] }, geometry: splitLine(p.track) }] });
    if (state.selPass != null) map.easeTo({ center: [p.closest_lon, p.closest_lat], zoom: Math.min(map.getZoom(), 5) });
    renderPasses();
  }
  $("passes").addEventListener("click", (e) => { const li = e.target.closest("li.pass"); if (li) selectPass(li); });
  $("passes").addEventListener("keydown", (e) => { const li = e.target.closest("li.pass"); if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); selectPass(li); } });
  async function loadPasses() {
    try { state.passes = await getJSON("/api/passes"); } catch (e) { state.passes = []; }
    state.selPass = null;
    renderPasses();
  }
  const satMarkers = {};
  async function loadPositions() {
    if (!$("show-sats").checked) return;
    let r;
    try { r = await getJSON("/api/positions"); } catch (e) { return; }
    const feats = [];
    for (const [key, s] of Object.entries(r.satellites)) {
      if (!satMarkers[key]) {
        const el = document.createElement("div");
        el.className = "sat-marker";
        el.innerHTML = `<i style="background:${SAT_COLORS[key]}"></i>${esc(s.name)}`;
        satMarkers[key] = new maplibregl.Marker({ element: el, anchor: "left", offset: [-7, 0] }).setLngLat([s.lon, s.lat]).addTo(map);
      }
      satMarkers[key].setLngLat([s.lon, s.lat]);
      feats.push({ type: "Feature", properties: { color: SAT_COLORS[key] }, geometry: splitLine(s.ahead) });
    }
    map.getSource("sat-ahead").setData({ type: "FeatureCollection", features: feats });
  }
  $("show-sats").onchange = (e) => {
    const on = e.target.checked;
    Object.values(satMarkers).forEach((m) => (m.getElement().style.display = on ? "" : "none"));
    map.setLayoutProperty("sat-ahead", "visibility", on ? "visible" : "none");
    if (on) loadPositions();
  };

  // ---- Veri durumu ve izleme bildirimleri ---------------------------------------------------
  const SOURCE_STATE = {
    ok: ["çalışıyor", "ok"], error: ["hata", "err"], pending: ["hazır, henüz denenmedi", "idle"],
    not_configured: ["erişim tanımlı değil", "idle"],
  };
  const TOKEN_TEXT = { unset: "Token tanımlı değil; herkese açık katalog kullanılıyor.", set: "Token tanımlı; henüz denenmedi.",
    accepted: "Token kabul edildi.", rejected: "Token reddedildi; herkese açık katalog kullanılıyor." };
  const t = (iso) => (iso ? fmtShort(iso) : "–");

  function renderSources(list) {
    $("sources").innerHTML = list.map((s) => {
      const [label, cls] = SOURCE_STATE[s.state] ?? [s.state, "idle"];
      const lines = [];
      if (s.state === "not_configured") lines.push("Sunucuda erişim bilgisi yok; bu kaynak kullanılmıyor.");
      else {
        lines.push(`Son kontrol: ${t(s.last_check)} · son başarılı alma: ${t(s.last_ok)}`);
        if (s.observations === false) lines.push("Yörünge verisidir; ölçüm değildir.");
        else if (s.id === "tropomi") lines.push(`Son gözlem günü: ${s.last_observation ? fmtDay(s.last_observation) : "–"}`);
        else if (s.last_observation) lines.push(`Son gözlem: ${t(s.last_observation)}`);
        if (s.records != null) lines.push(`${s.records} kayıt${s.duplicates ? ` · ${s.duplicates} tekrar atıldı` : ""}${s.invalid ? ` · ${s.invalid} geçersiz` : ""}`);
        if (s.collection) lines.push(`Ürün: ${s.collection}`);
        if (s.id === "carbonmapper" && TOKEN_TEXT[s.token]) lines.push(TOKEN_TEXT[s.token]);
        if (s.state === "error") lines.push(`Son hata: ${F.ERROR_CLASS[s.error_class] || s.error_class || "belirtilmedi"}`);
      }
      if (s.schedule) lines.push(`Güncelleme: ${s.schedule}`);
      return `<li><div class="src-head"><span class="src-name">${esc(s.name)}</span><span class="state ${cls}">${esc(label)}</span></div>
        ${lines.map((l) => `<span class="src-line">${esc(l)}</span>`).join("")}</li>`;
    }).join("");
  }

  function eventHtml(a) {
    const btn = a.plumeId ? `<button class="linkish" data-open="${esc(a.plumeId)}">Kaydı aç</button>` : "";
    const lag = a.publicationLagDays != null ? ` (gözlemden ${fmtNum(a.publicationLagDays, 1)} gün sonra)` : "";
    switch (a.kind) {
      case "new_observation":
      case "past_observation_published":
        return `<li class="info"><b>${a.kind === "new_observation" ? "Yeni gözlem" : "Geçmiş gözlem yeni yayımlandı"}</b>
          <span>${esc(a.sensor ?? "Sensör belirtilmemiş")} · gözlem ${esc(t(a.observedAt))} · yayımlama ${esc(t(a.publishedAt))}${esc(lag)} · sisteme alınma ${esc(t(a.ingestedAt))} · emisyon tahmini: ${esc(emissionText(a.emission))}</span>
          <small>Dayanak: ${esc(a.rule)} Yeni alınan kayıt, salımın bugün başladığını göstermez.</small>${btn}</li>`;
      case "repeated_detection":
        return `<li class="info"><b>Tekrarlanan tespit</b><span>Kaynak kaydında ${a.plumeCount} bulut · ${fmtNum(a.observationDateCount)} gözlem tarihinde ${fmtNum(a.detectionDateCount)} tespit tarihi (sağlayıcı sayımı).</span><small>Dayanak: ${esc(a.rule)}</small>${btn}</li>`;
      case "record_updated":
        return `<li class="info"><b>Kayıt güncellendi</b><span>${esc(a.plumeId)} · sağlayıcı değişikliği ${esc(t(a.modifiedAt))} · sürüm ${esc(a.previousVersion || "?")} → ${esc(a.version || "?")} · emisyon ${a.previousEmission != null ? fmtNum(a.previousEmission) : "–"} → ${a.emission != null ? fmtNum(a.emission) : "–"} kg/sa</span><small>Dayanak: ${esc(a.rule)}</small>${btn}</li>`;
      case "regional_enhancement": {
        const r = a.rule, b = a.block;
        const days = a.days.map((d) => `${fmtDay(d.day)}: ${F.signed(d.anomalyPpb, 1)} ppb (eşik ${F.signed(d.limitPpb, 1)}, kapsama %${fmtNum(d.coverage * 100)})`).join("; ");
        return `<li class="review"><b>İncelenecek bölgesel artış (TROPOMI)</b><span>Blok ${fmtNum(b.south, 1)}–${fmtNum(b.north, 1)}° K, ${fmtNum(b.west, 1)}–${fmtNum(b.east, 1)}° D · ${esc(days)}</span>
          <small>Kural ${esc(r.id)}: 0,5° blok ortalaması, referans "${esc(r.reference)}"; blok anomalisi günün blok anomalileri medyanından ${fmtNum(r.madFactor, 1)} sağlam sapma (1,4826·MAD) yukarıda; blok kara hücrelerinin en az %${fmtNum(r.minBlockCoverage * 100)}'i ölçülmüş; qa_value > ${fmtNum(r.qaMin, 1)}; ${r.windowDays} gün içinde en az ${r.minDays} gün. Doğrulanmış tesis kaçağı değildir.</small></li>`;
      }
      case "source_error":
        return `<li class="warn"><b>Veri güncellenemedi: ${esc(a.name)}</b><span>Kontrol ${esc(t(a.lastCheck))} · neden: ${esc(F.ERROR_CLASS[a.errorClass] || a.errorClass)} · son başarılı alma ${esc(t(a.lastOk))}</span><small>${esc(a.rule)}</small></li>`;
      case "stale":
        return `<li class="warn"><b>Veri güncel değil: ${esc(a.name)}</b><span>Son başarılı alma: ${esc(t(a.lastOk))}</span><small>${esc(a.rule)}</small></li>`;
      case "token_rejected":
        return `<li class="warn"><b>${esc(a.name)}: token reddedildi</b><small>${esc(a.rule)}</small></li>`;
      default:
        return "";
    }
  }

  async function loadEvents() {
    try {
      const [st, ev] = await Promise.all([getJSON("/api/data-status"), getJSON("/api/monitoring-events")]);
      renderSources(st.sources);
      $("events-count").textContent = ev.items.length || "";
      $("events").innerHTML = ev.items.length ? ev.items.map(eventHtml).join("")
        : `<li class="muted">Son ${ev.windowDays} günde bildirim yok. Bu, yeni salım olmadığını göstermez; yalnızca sisteme yeni kayıt alınmadığını, kaynaklarda hata görülmediğini ve bölgesel kuralın sağlanmadığını belirtir.</li>`;
    } catch (e) {
      $("events").innerHTML = `<li class="muted">Bildirimler alınamadı: ${esc(e.message)}</li>`;
    }
  }
  $("events").addEventListener("click", (e) => { const b = e.target.closest("[data-open]"); if (b) openDetail(b.dataset.open, { fly: true, focus: true }); });

  async function loadStatus() {
    try {
      const s = await getJSON("/api/status");
      $("status").textContent = s.tropomi_finished ? `TROPOMI son güncelleme: ${fmtShort(s.tropomi_finished)} · ${s.granules} dosya işlendi` : "TROPOMI henüz güncellenmedi";
    } catch (e) { $("status").textContent = `Durum alınamadı: ${e.message}`; }
  }

  async function loadContent() {
    try { state.content = await getJSON(CONTENT_URL); } catch (e) { state.content = null; }
  }

  // ---- Başlangıç -------------------------------------------------------------------------
  async function init() {
    $("f-period").value = initial.days;
    $("f-sort").value = ["observed", "published", "emission"].includes(initial.sort) ? initial.sort : "observed";
    $("f-emission").checked = initial.em;
    $("show-tropomi").checked = initial.tropomi;
    $("show-sources").checked = initial.sources;
    map.setLayoutProperty("cells", "visibility", initial.tropomi ? "visible" : "none");
    setSourcesVisible(initial.sources);
    state.mode = ["day", "7", "30", "90"].includes(initial.mode) ? initial.mode : "day";
    state.view = initial.view === "anom" ? "anom" : "abs";
    setMode(state.mode);
    setView(state.view);
    loadContent();
    try { await loadDays(false); } catch (e) { $("daylabel").textContent = `TROPOMI günleri alınamadı: ${e.message}`; }
    await Promise.all([loadTropomi(), loadObservations(), loadSources(), loadPasses(), loadPositions(), loadStatus(), loadEvents()]);
    if (initial.sel) openDetail(initial.sel, { fly: !startView });
    else if (initial.src) {
      openSource(initial.src);
      const s = state.sources.find((x) => x.id === initial.src);
      if (s && !startView) map.flyTo({ center: [s.lon, s.lat], zoom: 11 });
    }
    setInterval(loadPositions, 15000);
    setInterval(renderPasses, 60000);
    setInterval(loadPasses, 15 * 60000);
    setInterval(async () => {
      const had = state.days.length;
      try { await loadDays(true); } catch (e) { return; }
      if (state.days.length !== had) loadTropomi();
      loadStatus();
      loadEvents();
    }, 5 * 60000);
  }
})();
