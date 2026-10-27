const PLASMA = ["#0d0887", "#5c01a6", "#9c179e", "#cc4778", "#ed7953", "#fdb42f", "#f0f921"];
const DIVERGING = ["#2166ac", "#67a9cf", "#d1e5f0", "#f7f7f7", "#fddbc7", "#ef8a62", "#b2182b"];
const SAT_COLORS = { s5p: "#5cc28a", iss: "#9cc7ff" };
const TZ = "Europe/Istanbul";

const SECTORS = {
  "1A1": "Enerji (elektrik / ısı)", "1B1a": "Kömür madenciliği", "1B2": "Petrol ve gaz",
  "4B": "Hayvancılık (gübre)", "6A": "Katı atık (çöplük)", "6B": "Atık su", other: "Diğer",
};
// Bulut görüntüleri yalnızca bu yakınlaştırmadan itibaren ve sadece görünen alandakiler için yüklenir
const PLUME_IMG_ZOOM = 8;

const state = { days: [], dayIdx: -1, mode: "day", view: "abs", data: null, passes: [], selPass: null, plumes: [] };
const $ = (id) => document.getElementById(id);

const fmtDay = (d) => new Date(d + "T12:00:00Z").toLocaleDateString("tr-TR", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
const fmtTime = (iso) => new Date(iso).toLocaleString("tr-TR", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: TZ });
const fmtNum = (v, d = 0) => v == null ? "–" : v.toLocaleString("tr-TR", { maximumFractionDigits: d, minimumFractionDigits: d, useGrouping: false });
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

// ---- Harita ----------------------------------------------------------------

const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: {
      base: {
        type: "raster", tileSize: 256, maxzoom: 16,
        tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"],
        attribution: "Harita © Esri, HERE, Garmin, © OpenStreetMap katkıcıları",
      },
      labels: {
        type: "raster", tileSize: 256, maxzoom: 16,
        tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}"],
      },
    },
    layers: [{ id: "base", type: "raster", source: "base" }, { id: "labels", type: "raster", source: "labels" }],
  },
  bounds: [[26, 35.8], [44.8, 42.1]],
  fitBoundsOptions: { padding: mapPadding() },
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

// Paneli haritanın üstünde boş alan sayarak Türkiye'yi görünür kısma ortalar
function mapPadding() {
  return window.innerWidth < 720
    ? { top: 20, right: 20, left: 20, bottom: window.innerHeight * 0.48 + 10 }
    : { top: 30, right: 60, bottom: 30, left: 370 };
}

const empty = () => ({ type: "FeatureCollection", features: [] });

// Tarih çizgisini (±180°) geçen çizgileri parçalara böler
function splitLine(coords) {
  const parts = [[]];
  coords.forEach((c, k) => {
    if (k && Math.abs(c[0] - coords[k - 1][0]) > 180) parts.push([]);
    parts[parts.length - 1].push(c);
  });
  return { type: "MultiLineString", coordinates: parts.filter((p) => p.length > 1) };
}

map.on("load", async () => {
  map.addSource("cells", { type: "geojson", data: empty() });
  map.addSource("turkey", { type: "geojson", data: "turkey.geojson" });
  map.addSource("plumes", { type: "geojson", data: empty() });
  map.addSource("pass-track", { type: "geojson", data: empty() });
  map.addSource("sat-ahead", { type: "geojson", data: empty() });

  map.addLayer({ id: "cells", type: "fill", source: "cells", paint: { "fill-color": "#000", "fill-opacity": 0.78 } });
  map.addLayer({ id: "turkey", type: "line", source: "turkey", paint: { "line-color": "#d7dde5", "line-width": 1, "line-opacity": 0.7 } });
  map.addLayer({
    id: "sat-ahead", type: "line", source: "sat-ahead",
    paint: { "line-color": ["get", "color"], "line-width": 1, "line-opacity": 0.45, "line-dasharray": [2, 3] },
  });
  map.addLayer({
    id: "pass-track", type: "line", source: "pass-track",
    paint: { "line-color": ["get", "color"], "line-width": 3, "line-opacity": 0.9 },
  });
  map.addLayer({
    id: "plumes", type: "circle", source: "plumes",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["coalesce", ["get", "emission"], 300], 100, 5, 1000, 8, 10000, 14],
      "circle-color": "rgba(255,255,255,0.1)",
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": 2,
      // Bulut görüntüsü altta görünür hale gelince işaret soluklaşsın
      "circle-stroke-opacity": ["interpolate", ["linear"], ["zoom"], PLUME_IMG_ZOOM, 1, PLUME_IMG_ZOOM + 3, 0.35],
    },
  });

  map.moveLayer("labels", "sat-ahead"); // yer adları metan katmanının üstünde kalsın

  map.on("click", "cells", (e) => {
    if (map.queryRenderedFeatures(e.point, { layers: ["plumes"] }).length) return;
    const p = e.features[0].properties;
    const v = state.view === "anom" ? `${p.v > 0 ? "+" : ""}${fmtNum(p.v, 1)} ppb (medyana göre)` : `${fmtNum(p.v, 1)} ppb`;
    new maplibregl.Popup().setLngLat(e.lngLat).setHTML(
      `<div class="popup"><b>${v}</b><table>
        <tr><td>Mutlak</td><td>${fmtNum(p.abs, 1)} ppb</td></tr>
        <tr><td>Piksel sayısı</td><td>${p.n}</td></tr>
        <tr><td>Konum</td><td>${fmtNum(p.lat, 2)}°K, ${fmtNum(p.lon, 2)}°D</td></tr></table></div>`).addTo(map);
  });
  map.on("click", "plumes", (e) => showPlume(e.features[0], e.lngLat));
  for (const id of ["cells", "plumes"]) {
    map.on("mouseenter", id, () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", id, () => (map.getCanvas().style.cursor = ""));
  }

  init();
});

// ---- TROPOMI katmanı ---------------------------------------------------------

function range() {
  const end = state.days[state.dayIdx].day;
  if (state.mode === "day") return [end, end];
  const d = new Date(end + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() - (Number(state.mode) - 1));
  return [d.toISOString().slice(0, 10), end];
}

async function loadTropomi() {
  if (state.dayIdx < 0) return;
  const [start, end] = range();
  const day = state.days[state.dayIdx];
  $("daylabel").innerHTML = state.mode === "day"
    ? `${fmtDay(end)}<small>${day.source === "OFFL" ? "kesin ürün" : day.source === "NRTI" ? "hızlı ürün (NRT)" : "veri yok"}</small>`
    : `${fmtDay(start)} – ${fmtDay(end)}<small>${state.mode} günlük ortalama</small>`;
  $("prev").disabled = state.dayIdx <= 0;
  $("next").disabled = state.dayIdx >= state.days.length - 1;
  state.data = await api(`/api/tropomi?start=${start}&end=${end}`);
  renderCells();
  renderSpark();
}

function renderCells() {
  const d = state.data;
  const st = d.stats;
  const anom = state.view === "anom";
  let lo, hi, colors;
  if (!st) {
    lo = 1850; hi = 1950; colors = PLASMA;
  } else if (anom) {
    const m = Math.max(10, Math.ceil(Math.max(st.median - st.p02, st.p98 - st.median) / 5) * 5);
    lo = -m; hi = m; colors = DIVERGING;
  } else {
    lo = Math.floor(st.p02); hi = Math.ceil(st.p98); colors = PLASMA;
    if (hi - lo < 10) { lo -= 5; hi += 5; }
  }
  const g = d.grid;
  const features = d.cells.map(([j, i, v, n]) => {
    const x = d.lon0 + i * g, y = d.lat0 + j * g;
    return {
      type: "Feature",
      properties: { v: anom ? v - st.median : v, abs: v, n, lon: x + g / 2, lat: y + g / 2 },
      geometry: { type: "Polygon", coordinates: [[[x, y], [x + g, y], [x + g, y + g], [x, y + g], [x, y]]] },
    };
  });
  map.getSource("cells").setData({ type: "FeatureCollection", features });

  const stops = colors.flatMap((c, k) => [lo + ((hi - lo) * k) / (colors.length - 1), c]);
  map.setPaintProperty("cells", "fill-color", ["interpolate", ["linear"], ["get", "v"], ...stops]);
  $("legend-bar").style.background = `linear-gradient(90deg, ${colors.join(",")})`;
  const sign = (v) => (anom && v > 0 ? "+" : "") + fmtNum(v);
  $("lg-min").textContent = sign(lo);
  $("lg-mid").textContent = anom ? "0 ppb" : `${fmtNum((lo + hi) / 2)} ppb`;
  $("lg-max").textContent = sign(hi);

  $("st-mean").textContent = st ? `${fmtNum(st.mean)} ppb` : "–";
  $("st-max").textContent = st ? `${fmtNum(st.max)} ppb` : "–";
  $("st-cov").textContent = st ? `%${fmtNum(st.coverage * 100)}` : "%0";
}

function renderSpark() {
  const svg = $("spark");
  const pts = state.days.map((d, k) => ({ k, v: d.mean })).filter((p) => p.v != null);
  if (pts.length < 2) { svg.innerHTML = ""; return; }
  const vs = pts.map((p) => p.v);
  const lo = Math.min(...vs) - 2, hi = Math.max(...vs) + 2;
  const n = state.days.length;
  const X = (k) => 6 + (k / Math.max(1, n - 1)) * 288;
  const Y = (v) => 50 - ((v - lo) / (hi - lo)) * 44;
  let h = "";
  if (state.mode !== "day") {
    const [start] = range();
    const k0 = Math.max(0, state.days.findIndex((d) => d.day >= start));
    h += `<rect class="span" x="${X(k0)}" y="0" width="${Math.max(1, X(state.dayIdx) - X(k0))}" height="56"/>`;
  }
  h += `<polyline class="line" points="${pts.map((p) => `${X(p.k)},${Y(p.v)}`).join(" ")}"/>`;
  for (const p of pts) {
    h += `<circle class="pt${p.k === state.dayIdx ? " sel" : ""}" cx="${X(p.k)}" cy="${Y(p.v)}" r="${p.k === state.dayIdx ? 3.5 : 2}"/>`;
    h += `<rect class="hit" data-k="${p.k}" x="${X(p.k) - 288 / n / 2}" y="0" width="${288 / n}" height="56"><title>${fmtDay(state.days[p.k].day)}: ${fmtNum(p.v, 1)} ppb</title></rect>`;
  }
  svg.innerHTML = h;
}

// Aylarca veride her günün tıklama alanı birkaç piksele iniyor; x konumuna en yakın verili gün seçilir
$("spark").addEventListener("click", (e) => {
  const n = state.days.length;
  if (!n) return;
  const box = $("spark").getBoundingClientRect();
  const x = ((e.clientX - box.left) / box.width) * 300;
  const target = ((x - 6) / 288) * Math.max(1, n - 1);
  let best = -1;
  state.days.forEach((d, k) => {
    if (d.mean != null && (best < 0 || Math.abs(k - target) < Math.abs(best - target))) best = k;
  });
  if (best >= 0) { state.dayIdx = best; loadTropomi(); }
});
$("prev").onclick = () => { state.dayIdx--; loadTropomi(); };
$("next").onclick = () => { state.dayIdx++; loadTropomi(); };

function bindSeg(id, key, cb) {
  $(id).addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    [...$(id).children].forEach((x) => x.classList.toggle("on", x === b));
    state[key] = b.dataset[key];
    cb();
  });
}
bindSeg("mode", "mode", loadTropomi);
bindSeg("view", "view", () => state.data && renderCells());

// ---- Carbon Mapper bulutları -----------------------------------------------------

function showPlume(f, lngLat) {
  // Haritadan gelen özelliklerde iç içe değerler JSON metnine dönüşüyor, asıl kaydı kullan
  const p = state.plumes.find((x) => x.properties.id === f.properties.id)?.properties ?? f.properties;
  const [lon, lat] = f.geometry.coordinates;
  const rows = [
    ["Emisyon", p.emission != null ? `${fmtNum(p.emission)} ± ${fmtNum(p.uncertainty)} kg/sa` : "yayımlanmadı"],
    ["Sektör", esc(SECTORS[p.sector] ?? p.sector ?? "–")],
    ["Gözlem", fmtTime(p.observed)],
    ["Uydu", esc(p.platform ?? "–")],
    ["Rüzgâr", p.wind_speed != null ? `${fmtNum(p.wind_speed, 1)} m/s` : "–"],
  ];
  const link = `https://data.carbonmapper.org/?plume_id=${encodeURIComponent(p.id)}#12/${lat.toFixed(4)}/${lon.toFixed(4)}`;
  new maplibregl.Popup().setLngLat(lngLat).setHTML(
    `<div class="popup"><b>Metan bulutu</b><br><small class="muted">${esc(p.id)}</small>
      <table>${rows.map(([a, b]) => `<tr><td>${a}</td><td>${b}</td></tr>`).join("")}</table>
      <a href="${link}" target="_blank" rel="noopener">Carbon Mapper'da aç ↗</a></div>`
  ).addTo(map);
}

const plumeImages = new Set();
function addPlumeImages() {
  if (!$("show-plumes").checked || map.getZoom() < PLUME_IMG_ZOOM) return;
  const v = map.getBounds();
  for (const { properties: p } of state.plumes) {
    const b = p.bounds; // [batı, güney, doğu, kuzey]
    if (!p.png || !b || plumeImages.has(p.id)) continue;
    if (b[2] < v.getWest() || b[0] > v.getEast() || b[3] < v.getSouth() || b[1] > v.getNorth()) continue;
    const id = `plume-img-${p.id}`;
    map.addSource(id, { type: "image", url: p.png, coordinates: [[b[0], b[3]], [b[2], b[3]], [b[2], b[1]], [b[0], b[1]]] });
    map.addLayer({ id, type: "raster", source: id, minzoom: PLUME_IMG_ZOOM, paint: { "raster-fade-duration": 0 } }, "plumes");
    plumeImages.add(p.id);
  }
}
map.on("moveend", addPlumeImages);

async function loadPlumes() {
  const gj = await api("/api/plumes");
  state.plumes = gj.features;
  map.getSource("plumes").setData(gj);
  const n = gj.features.length;
  const last = n ? fmtDay(gj.features[0].properties.observed.slice(0, 10)) : null;
  $("plume-count").textContent = n ? `(${n}, son: ${last})` : "(henüz yok)";
  addPlumeImages();
}
$("show-plumes").onchange = (e) => {
  const vis = e.target.checked ? "visible" : "none";
  map.setLayoutProperty("plumes", "visibility", vis);
  plumeImages.forEach((id) => map.setLayoutProperty(`plume-img-${id}`, "visibility", vis));
  addPlumeImages();
};

// ---- Geçişler ve uydu konumları ------------------------------------------------

function countdown(iso) {
  const m = Math.round((new Date(iso) - Date.now()) / 60000);
  if (m <= 0) return "şu an";
  const h = Math.floor(m / 60);
  return h ? `${h} sa ${m % 60} dk sonra` : `${m} dk sonra`;
}

function renderPasses() {
  const list = state.passes.slice(0, 8);
  $("passes").innerHTML = list.length ? list.map((p, k) => {
    const measures = p.sat === "s5p"
      ? (p.daylight ? `<span class="badge day">gündüz · CH₄ ölçülür</span>` : `<span class="badge night">gece · CH₄ ölçülmez</span>`)
      : (p.daylight ? `<span class="badge day">gündüz · hedefli çekim olabilir</span>` : `<span class="badge night">gece</span>`);
    return `<li class="pass${state.selPass === k ? " sel" : ""}" data-k="${k}">
      <span class="t">${fmtTime(p.closest)}</span><span class="cd">${countdown(p.start)}</span>
      <span class="meta">${p.name} ${measures}</span></li>`;
  }).join("") : `<li class="muted">72 saat içinde geçiş yok</li>`;
}

$("passes").addEventListener("click", (e) => {
  const li = e.target.closest("li.pass");
  if (!li) return;
  const k = Number(li.dataset.k);
  state.selPass = state.selPass === k ? null : k;
  const p = state.passes[k];
  map.getSource("pass-track").setData(state.selPass == null ? empty() : {
    type: "FeatureCollection",
    features: [{ type: "Feature", properties: { color: SAT_COLORS[p.sat] }, geometry: splitLine(p.track) }],
  });
  if (state.selPass != null) map.easeTo({ center: [p.closest_lon, p.closest_lat], zoom: Math.min(map.getZoom(), 5) });
  renderPasses();
});

async function loadPasses() {
  try { state.passes = await api("/api/passes"); } catch { state.passes = []; }
  state.selPass = null;
  renderPasses();
}

const satMarkers = {};
async function loadPositions() {
  if (!$("show-sats").checked) return;
  const { satellites } = await api("/api/positions");
  const feats = [];
  for (const [key, s] of Object.entries(satellites)) {
    if (!satMarkers[key]) {
      const el = document.createElement("div");
      el.className = "sat-marker";
      el.innerHTML = `<i style="background:${SAT_COLORS[key]}"></i>${s.name}`;
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

// ---- Durum ---------------------------------------------------------------------

async function loadStatus() {
  const s = await api("/api/status");
  const b = $("banner");
  if (s.auth === "missing") {
    b.hidden = false;
    b.textContent = "NASA Earthdata kimlik bilgileri tanımlı değil; TROPOMI verisi indirilemiyor. Sunucuda EARTHDATA_USERNAME ve EARTHDATA_PASSWORD ayarlayın.";
  } else if (!state.days.length) {
    b.hidden = false;
    b.textContent = "İlk veriler indiriliyor. Bu birkaç dakika sürebilir, sayfa kendini yenileyecek.";
  } else b.hidden = true;
  $("status").textContent = s.tropomi_finished
    ? `Son güncelleme: ${fmtTime(s.tropomi_finished)} · ${s.granules} dosya işlendi`
    : "Henüz güncelleme yapılmadı";
}

async function loadDays(keepSelection) {
  const prev = state.days[state.dayIdx]?.day;
  state.days = await api("/api/days");
  if (!state.days.length) { state.dayIdx = -1; return; }
  let idx = keepSelection && prev ? state.days.findIndex((d) => d.day === prev) : -1;
  if (idx < 0) {
    // En son, ülkenin en az %5'inin bulutsuz ölçüldüğü gün
    idx = state.days.length - 1;
    for (let k = state.days.length - 1; k >= 0; k--) if (state.days[k].coverage >= 0.05) { idx = k; break; }
  }
  state.dayIdx = idx;
}

async function init() {
  await loadDays(false);
  await Promise.all([loadTropomi(), loadPlumes(), loadPasses(), loadPositions(), loadStatus()]);
  setInterval(loadPositions, 15000);
  setInterval(renderPasses, 60000);
  setInterval(loadPasses, 15 * 60000);
  setInterval(async () => {
    const had = state.days.length;
    await loadDays(true);
    if (state.days.length !== had) loadTropomi();
    loadStatus();
  }, 5 * 60000);
}
