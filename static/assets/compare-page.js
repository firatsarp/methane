/* Karşılaştırma: iki harita, her birinde yalnızca seçilen tek bulut. Karşılaştırılabilirlik kararı /api/comparisons'tan
   gelir; tarayıcı karar üretmez. Uyumsuz niceliklerde ortak renk ölçeği kullanılmaz (sağlayıcı PNG'leri ayrı ölçekli). */
(async function () {
  "use strict";
  const F = window.Fmt;
  const { esc, fmtNum, fmtLocal, fmtUtc, getJSON } = F;
  const $ = (id) => document.getElementById(id);
  const q = new URLSearchParams(location.search);
  const BASE = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}";
  const MODE = {
    near_time_independent: "Yakın zamanlı bağımsız karşılaştırma",
    historical_change: "Tarihsel değişim (aynı seri)",
    context_only: "Yalnızca bağlam — sayısal fark üretilmedi",
  };

  const geo = await window.TurkeyGeo.load();
  function makeMap(id) {
    const map = new maplibregl.Map({ container: id, attributionControl: { compact: true }, maxBounds: geo.maxBounds,
      style: { version: 8, sources: { base: { type: "raster", tiles: [BASE], tileSize: 256, attribution: "Esri, HERE, Garmin, © OpenStreetMap" } },
        layers: [{ id: "base", type: "raster", source: "base" }] }, bounds: geo.bounds });
    map.on("load", () => {
      map.addSource("mask", { type: "geojson", data: geo.mask });
      map.addSource("pt", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "anchor", type: "background", layout: { visibility: "none" } });
      map.addLayer({ id: "mask", type: "fill", source: "mask", paint: { "fill-color": "#e9edf1", "fill-opacity": 1 } });
      map.addLayer({ id: "pt", type: "circle", source: "pt", paint: { "circle-radius": 6, "circle-color": "#d95c36", "circle-stroke-color": "#fff", "circle-stroke-width": 2 } });
    });
    return map;
  }
  const views = {
    a: { map: makeMap("map-a"), leg: $("leg-a"), info: $("info-a") },
    b: { map: makeMap("map-b"), leg: $("leg-b"), info: $("info-b") },
  };
  await Promise.all(Object.values(views).map((v) => new Promise((r) => (v.map.loaded() ? r() : v.map.once("load", r)))));
  for (const v of Object.values(views)) {
    v.plume = window.PlumeLayer.createPlumeLayer(v.map, { before: "mask", onState: (s) => {
      v.leg.textContent = { loading: "Görüntü yükleniyor…", ready: "Sağlayıcı artış görüntüsü; sayısal lejant yok, renkten değer okunmaz.",
        no_raster: s.reason, error: s.reason, none: "" }[s.status] || "";
    } });
  }
  // Kadraj eşleme (isteğe bağlı); döngüye girmemek için kaynak harita işaretlenir
  let syncing = false;
  for (const [k, v] of Object.entries(views)) {
    v.map.on("move", () => {
      if (!$("sync").checked || syncing) return;
      syncing = true;
      const other = views[k === "a" ? "b" : "a"].map;
      other.jumpTo({ center: v.map.getCenter(), zoom: v.map.getZoom() });
      syncing = false;
    });
  }

  const label = (o) => `${fmtLocal(o.observedAt) || "zaman yok"} · ${o.sensor || "?"} · ${o.provider}${o.measurement?.value != null ? ` · ${fmtNum(o.measurement.value)} ${o.measurement.unit}` : ""}`;
  const all = (await getJSON("/api/observations?limit=2000&scope=tr")).items;
  const byId = new Map(all.map((o) => [o.id, o]));
  const optionList = (list, sel) => list.map((o) => `<option value="${esc(o.id)}"${o.id === sel ? " selected" : ""}>${esc(label(o))}</option>`).join("");
  $("sel-a").innerHTML = '<option value="">Seçin</option>' + optionList(all, q.get("a"));

  async function fillB() {
    const a = $("sel-a").value;
    if (!a) { $("sel-b").innerHTML = '<option value="">Önce A seçin</option>'; return; }
    const r = await getJSON(`/api/observations/${encodeURIComponent(a)}/comparisons`);
    const cands = r.items.map((c) => byId.get(c.observation.id) || c.observation);
    const pick = q.get("b") && cands.some((c) => c.id === q.get("b")) ? q.get("b") : cands[0]?.id;
    $("sel-b").innerHTML = cands.length ? optionList(cands, pick)
      : '<option value="">Aynı yer veya kaynakta başka gözlem yok</option>';
  }

  function infoHtml(o, w) {
    const m = o.measurement;
    const u = m?.uncertainty;
    const p = w?.primary;
    const wind = p?.wind10Interpolated || p?.wind10;
    return `<dl class="kv">
      <dt>Gözlem</dt><dd>${esc(fmtLocal(o.observedAt) || "–")}<small>${esc(fmtUtc(o.observedAt) || "")}</small></dd>
      <dt>Sensör / sağlayıcı</dt><dd>${esc(o.sensor || "–")}<small>${esc(o.provider)} · ${esc(o.methodVersion || "")}</small></dd>
      <dt>Çözünürlük</dt><dd>${o.gsdM ? `${fmtNum(o.gsdM)} m` : "Kayıtta yok"}</dd>
      <dt>Tahmin</dt><dd>${m?.value != null ? `${fmtNum(m.value)} ${esc(m.unit)}` : "Yok (sıfır değildir)"}${u ? `<small>± ${fmtNum(u.value)} · ${u.kind === "1-sigma" ? "1-sigma (belgeli)" : "istatistiksel tanımı belgelenmemiş"}</small>` : ""}</dd>
      <dt>Kalite</dt><dd>${esc(o.qualityNote || "Sağlayıcı kalite alanı boş")}</dd>
      <dt>Gözlem anı rüzgârı</dt><dd>${wind ? `${fmtNum(wind.speed, 1)} m/s${wind.from != null ? `, ${fmtNum(wind.from)}°'den (taşınma ${fmtNum(wind.to)}°)` : ", yön tanımsız"}<small>${esc(p.accessPath)} · gözlemden ${p.timeOffsetMinutes} dk</small>` : "Gözlem anına ait yeniden analiz yok"}</dd>
      <dt>Sınır tabakası</dt><dd>${p?.variables?.boundary_layer_height?.value != null ? `${fmtNum(p.variables.boundary_layer_height.value)} m` : "–"}</dd>
    </dl>`;
  }

  async function show(slot, o) {
    const v = views[slot];
    v.map.getSource("pt").setData({ type: "FeatureCollection", features: [{ type: "Feature", geometry: o.geometry, properties: {} }] });
    v.map.jumpTo({ center: o.geometry.coordinates, zoom: 12 });
    v.plume.show(o);
    v.info.innerHTML = '<div class="status-box">Meteoroloji yükleniyor…</div>';
    let w = null;
    try { w = await getJSON(`/api/weather/observation/${encodeURIComponent(o.id)}`); } catch (e) { w = null; }
    v.info.innerHTML = infoHtml(o, w);
  }

  function decisionHtml(d) {
    const num = d.numeric ? `<p class="big">Fark (B − A): <b>${F.signed(d.numeric.difference, 0)} ${esc(d.numeric.unit)}</b>${d.numeric.percent != null ? ` · ${F.signed(d.numeric.percent, 0)} %` : ""}</p>` : "";
    const comb = d.combined ? `<p>Birleşik 1-sigma: ${fmtNum(d.combined.sigma)} ${esc(d.combined.unit)} · z = ${fmtNum(d.combined.z, 2)}</p>` : "";
    return `<h2>${esc(MODE[d.mode] || d.mode)}</h2>
      <p class="hint">Zaman farkı ${d.timeDifferenceMinutes == null ? "bilinmiyor" : d.timeDifferenceMinutes < 1440 ? `${fmtNum(d.timeDifferenceMinutes)} dk` : `${fmtNum(d.timeDifferenceMinutes / 1440, 1)} gün`} · mesafe ${fmtNum(d.distanceM)} m · bağımsızlık: ${d.independent === true ? "farklı çekimler" : d.independent === false ? "aynı çekim/kayıt — bağımsız değil" : "değerlendirilmedi"}</p>
      ${num}${comb}
      <ul>${d.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}${d.limitations.map((r) => `<li class="lim">${esc(r)}</li>`).join("")}</ul>
      <p class="hint">Yöntem ${esc(d.method)} · ${esc(d.policyVersion)}. "Uyuşuyor/çelişiyor" hükmü üretilmez.</p>`;
  }

  async function update() {
    const a = $("sel-a").value, b = $("sel-b").value;
    const url = new URL(location.href);
    url.searchParams.set("a", a || "");
    url.searchParams.set("b", b || "");
    history.replaceState(null, "", url);
    if (!a || !b) { views.b.plume.clear(); return; }
    try {
      const r = await getJSON(`/api/comparisons?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
      $("decision").innerHTML = decisionHtml(r.decision);
      show("a", r.a);
      show("b", r.b);
    } catch (e) {
      $("decision").innerHTML = `<div class="status-box">Karşılaştırma yapılamadı: ${esc(e.message)}</div>`;
    }
  }
  $("sel-a").onchange = async () => { q.delete("b"); await fillB(); update(); };
  $("sel-b").onchange = update;
  await fillB();
  update();
})();
