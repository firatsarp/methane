/* Hava ve İklim: Gözlem Anı (ERA5), Güncel / Tahmin (Open-Meteo), İklim Bağlamı (ERA5 1991–2020). */
(function () {
  "use strict";
  const F = window.Fmt, C = window.Charts;
  const { esc, fmtNum, fmtLocal, fmtUtc, getJSON, latestOnly } = F;
  const $ = (id) => document.getElementById(id);
  const CLIMATE_POLL_MS = 15000;
  const CLIMATE_POLL_MAX = 80; // yaklaşık 20 dakika; sonrasında kullanıcı yeniden açabilir

  const q = new URLSearchParams(location.search);
  const st = { lat: null, lon: null, label: "", obsId: q.get("obs") || "", tab: q.get("tab") || "obs", compare: q.get("compare") || "" };

  function writeUrl() {
    const p = new URLSearchParams();
    if (st.obsId) p.set("obs", st.obsId);
    if (st.lat != null) { p.set("lat", st.lat.toFixed(4)); p.set("lon", st.lon.toFixed(4)); }
    if (st.tab !== "obs") p.set("tab", st.tab);
    if (st.compare) p.set("compare", st.compare);
    history.replaceState(null, "", `${location.pathname}?${p}`);
  }

  // ---- Sekmeler -----------------------------------------------------------------------
  const TABS = { obs: ["tab-obs", "panel-obs"], fc: ["tab-fc", "panel-fc"], clim: ["tab-clim", "panel-clim"] };
  function setTab(name, focus) {
    st.tab = TABS[name] ? name : "obs";
    for (const [k, [tab, panel]] of Object.entries(TABS)) {
      const on = k === st.tab;
      $(tab).setAttribute("aria-selected", String(on));
      $(tab).tabIndex = on ? 0 : -1;
      $(panel).hidden = !on;
    }
    if (focus) $(TABS[st.tab][0]).focus();
    writeUrl();
    load();
  }
  const order = Object.keys(TABS);
  document.querySelector(".tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[role=tab]");
    if (b) setTab(order.find((k) => TABS[k][0] === b.id));
  });
  document.querySelector(".tabs").addEventListener("keydown", (e) => {
    const i = order.indexOf(st.tab);
    if (e.key === "ArrowRight") setTab(order[(i + 1) % order.length], true);
    if (e.key === "ArrowLeft") setTab(order[(i + order.length - 1) % order.length], true);
  });

  // ---- Konum -----------------------------------------------------------------------------
  function setLocation(lat, lon, label) {
    st.lat = lat; st.lon = lon; st.label = label;
    $("loc-text").textContent = `${label}: ${fmtNum(lat, 4)}° K, ${fmtNum(lon, 4)}° D`;
    if (pickMarker) pickMarker.setLngLat([lon, lat]);
    writeUrl();
  }

  let pickMap = null, pickMarker = null;
  $("pick-toggle").onclick = () => {
    const wrap = $("pick-map-wrap");
    wrap.hidden = !wrap.hidden;
    $("pick-toggle").setAttribute("aria-expanded", String(!wrap.hidden));
    if (wrap.hidden || pickMap) { pickMap?.resize(); return; }
    pickMap = new maplibregl.Map({
      container: "pick-map",
      style: { version: 8, sources: { base: { type: "raster", tileSize: 256, attribution: "Harita © Esri, HERE, Garmin, © OpenStreetMap katkıcıları",
        tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"] } },
        layers: [{ id: "base", type: "raster", source: "base" }] },
      center: [st.lon ?? 35, st.lat ?? 39], zoom: st.lat != null ? 6 : 4.5,
    });
    pickMap.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    pickMarker = new maplibregl.Marker({ color: "#ed7953" }).setLngLat([st.lon ?? 35, st.lat ?? 39]).addTo(pickMap);
    pickMap.on("click", (e) => {
      st.obsId = "";
      setLocation(e.lngLat.lat, e.lngLat.lng, "Seçilen nokta");
      cache.fc = cache.clim = null;
      load();
    });
  };

  // ---- Gözlem Anı ---------------------------------------------------------------------------
  const obsReq = latestOnly();
  async function loadObservationList() {
    try {
      const r = await getJSON("/api/observations?limit=300&sort=observed");
      const sel = $("obs-select");
      sel.innerHTML = `<option value="">Bir gözlem seç…</option>` + r.items.map((o) =>
        `<option value="${esc(o.id)}">${esc(F.fmtShort(o.observedAt) || "zaman yok")} · ${esc(o.sensor || "")} · ${esc(o.locationLabel)}</option>`).join("");
      if (st.obsId && !r.items.some((o) => o.id === st.obsId)) sel.add(new Option(st.obsId, st.obsId));
      sel.value = st.obsId;
      return r.items;
    } catch (e) {
      $("obs-select").innerHTML = `<option value="">Gözlem listesi alınamadı</option>`;
      return [];
    }
  }
  $("obs-select").onchange = (e) => { st.obsId = e.target.value; cache.obs = null; if (st.obsId) cache.fc = cache.clim = null; load(); };

  function valueOf(v, keys, d = 1) {
    for (const k of keys) if (v[k] && F.num(v[k].value)) return { text: `${fmtNum(v[k].value, d)} ${v[k].unit || ""}`.trim(), v: v[k] };
    return { text: "–", v: null };
  }

  async function renderObs() {
    const out = $("obs-out");
    if (!st.obsId) { out.innerHTML = `<div class="status-box">Gözlem anının meteorolojisini görmek için bir metan bulutu gözlemi seç. Güncel tahmin eski bir gözleme bağlanmaz.</div>`; return; }
    const req = obsReq();
    out.innerHTML = `<div class="status-box">Yükleniyor…</div>`;
    let obsR, w;
    try {
      [obsR, w] = await Promise.all([
        getJSON(`/api/observations/${encodeURIComponent(st.obsId)}`, { signal: req.signal }),
        getJSON(`/api/weather/observation/${encodeURIComponent(st.obsId)}`, { signal: req.signal, timeout: 30000 }),
      ]);
    } catch (e) {
      if (!e.aborted && req.current()) out.innerHTML = `<div class="status-box warn">Gözlem meteorolojisi alınamadı: ${esc(e.message)}</div>`;
      return;
    }
    if (!req.current()) return;
    const o = obsR.observation;
    if (st.lat == null || st.label !== "Seçilen nokta") setLocation(o.lat, o.lon, "Gözlemin tespit noktası");
    const p = w.primary;
    const head = `<dl class="kv">
      <dt>Gözlem</dt><dd>${esc(o.sensor || "")} · ${esc(o.providerId || o.id)}<small>${esc(o.provider)}</small></dd>
      <dt>Gözlem zamanı</dt><dd>${esc(fmtLocal(o.observedAt) || "Doğrulanmadı")}<small>${esc(fmtUtc(o.observedAt) || "")}</small></dd>
      <dt>Yer</dt><dd>${esc(o.locationLabel)}</dd>
      ${p ? `<dt>Meteoroloji saati</dt><dd>${esc(fmtLocal(p.validAt))}<small>${esc(fmtUtc(p.validAt))} · gözlemden ${p.timeOffsetMinutes > 0 ? "+" : ""}${p.timeOffsetMinutes} dk · en fazla 60 dk tolerans</small></dd>
      <dt>Kaynak yolu</dt><dd>${esc(p.accessPath)}<small>${esc(p.dataset)} · yeniden analiz · ${esc(p.temporalResolution)} · ${esc(p.spatialResolution)} · ızgara ${fmtNum(p.resolvedGridPoint?.lat, 2)}° K, ${fmtNum(p.resolvedGridPoint?.lon, 2)}° D</small></dd>` : ""}
    </dl>`;
    const job = w.cdsJob ? `<p class="hint">CDS ERA5 işi: ${esc(F.STATUS_TEXT[w.cdsJob.state] || w.cdsJob.state)}${w.cdsJob.remoteId ? ` · iş kimliği ${esc(w.cdsJob.remoteId)}` : ""}${w.cdsJob.errorClass ? ` · ${esc(F.ERROR_CLASS[w.cdsJob.errorClass] || w.cdsJob.errorClass)}` : ""}</p>` : "";
    if (!p) {
      const msg = w.status === "empty" ? "Gözlem anına ait meteoroloji henüz yok. ERA5 birkaç gün gecikmeyle yayımlanır; tahmin ayrı sekmededir."
        : `Meteoroloji şu an alınamıyor (${esc(F.ERROR_CLASS[w.error?.errorClass] || "bilinmeyen neden")}).`;
      out.innerHTML = head + `<div class="status-box">${msg}</div>` + job;
      return;
    }
    const v = p.variables || {};
    const t = valueOf(v, ["temperature_2m", "t2m"]);
    const rh = valueOf(v, ["relative_humidity_2m", "rh"], 0);
    const pr = valueOf(v, ["surface_pressure", "sp"], 0);
    const cc = valueOf(v, ["cloud_cover", "tcc"], 0);
    const pc = valueOf(v, ["precipitation", "tp"], 1);
    const blh = valueOf(v, ["boundary_layer_height", "blh"], 0);
    const s = p.series;
    const charts = s ? `<div class="charts">
      <div><p class="chart-title">Rüzgâr hızı (10 m ve 100 m)</p>${C.timeSeries({ times: s.time, title: "Rüzgâr hızı", unit: "m/s", zero: true, marker: o.observedAt, markerLabel: "gözlem",
        series: [{ values: s.wind_speed_10m, label: "10 m" }].concat(s.wind_speed_100m ? [{ values: s.wind_speed_100m, label: "100 m", cls: "alt" }] : []) })}</div>
      <div><p class="chart-title">Sıcaklık (2 m)</p>${C.timeSeries({ times: s.time, title: "Sıcaklık", unit: p.units?.temperature_2m || "°C", marker: o.observedAt, markerLabel: "gözlem",
        series: [{ values: s.temperature_2m, label: "2 m sıcaklık" }] })}</div>
      ${s.precipitation ? `<div><p class="chart-title">Yağış (saatlik toplam)</p>${C.timeSeries({ times: s.time, bars: true, title: "Yağış", unit: "mm", marker: o.observedAt, markerLabel: "gözlem",
        series: [{ values: s.precipitation, label: "önceki 1 saatin toplamı", cls: "bar" }] })}</div>` : ""}
      ${s.boundary_layer_height ? `<div><p class="chart-title">Sınır tabakası yüksekliği</p>${C.timeSeries({ times: s.time, title: "Sınır tabakası", unit: "m", zero: true, marker: o.observedAt, markerLabel: "gözlem",
        series: [{ values: s.boundary_layer_height, label: "sınır tabakası" }] })}</div>` : ""}
    </div>` : "";
    out.innerHTML = head + `
      <div class="grid-main">
        <div class="metric wind">${F.windArrow(p.wind10?.to, 40)}<dl><dt>10 m rüzgâr</dt><dd>${F.num(p.wind10?.speed) ? fmtNum(p.wind10.speed, 1) + " m/s" : "–"}<small>${esc(F.windText(p.wind10))}</small></dd></dl></div>
        <dl class="metric"><dt>Sıcaklık (2 m)</dt><dd>${esc(t.text)}</dd></dl>
        <dl class="metric"><dt>Bulutluluk</dt><dd>${esc(cc.text)}</dd></dl>
        <dl class="metric"><dt>Yağış</dt><dd>${esc(pc.text)}<small>önceki 1 saatin toplamı</small></dd></dl>
        <dl class="metric"><dt>Bağıl nem (2 m)</dt><dd>${esc(rh.text)}</dd></dl>
        <dl class="metric"><dt>Sınır tabakası yüksekliği</dt><dd>${esc(blh.text)}</dd></dl>
      </div>
      <details class="extra"><summary>Ek değerler ve ayrıntılar</summary>
        <dl class="kv">
          <dt>100 m rüzgâr</dt><dd>${esc(F.windText(p.wind100))}</dd>
          <dt>Yüzey basıncı</dt><dd>${esc(pr.text)}</dd>
          <dt>Alınma zamanı</dt><dd>${esc(fmtLocal(p.fetchedAt))}</dd>
          <dt>Eksik değişkenler</dt><dd>${esc((p.missingVariables || []).join(", ") || "yok")}</dd>
          ${(w.alternatives || []).filter((a) => a.status === "ready").map((a) => `<dt>Diğer erişim yolu</dt><dd>${esc(a.accessPath)} · 10 m rüzgâr ${esc(F.windText(a.wind10))}</dd>`).join("")}
        </dl>
      </details>
      <p class="hint">Ok rüzgârın <b>taşıdığı</b> yönü gösterir (yukarı = kuzey); derece rüzgârın <b>geldiği</b> yöndür. ${esc(w.note)} Rüzgâr bilgisi otomatik kaynak ataması veya yeni emisyon hesabı için kullanılmaz.</p>
      ${charts}${job}
      <p class="attrib">${esc(p.attribution || "")} · ERA5: Copernicus Climate Change Service.</p>`;
  }

  // ---- Güncel / Tahmin ---------------------------------------------------------------------------
  const fcReq = latestOnly();
  async function renderForecast() {
    const out = $("fc-out");
    if (st.lat == null) { out.innerHTML = `<div class="status-box">Önce bir konum seç.</div>`; return; }
    const req = fcReq();
    out.innerHTML = `<div class="status-box">Yükleniyor…</div>`;
    let f;
    try { f = await getJSON(`/api/weather/forecast?lat=${st.lat.toFixed(4)}&lon=${st.lon.toFixed(4)}`, { signal: req.signal, timeout: 30000 }); }
    catch (e) { if (!e.aborted && req.current()) out.innerHTML = `<div class="status-box warn">Tahmin alınamadı: ${esc(e.message)}</div>`; return; }
    if (!req.current()) return;
    if (f.status === "unavailable") { out.innerHTML = `<div class="status-box warn">Tahmin şu an alınamıyor (${esc(F.ERROR_CLASS[f.error?.errorClass] || "bilinmeyen neden")}).</div>`; return; }
    const cur = f.current || {}, v = cur.variables || {};
    const val = (k, d = 1) => (v[k] && F.num(v[k].value) ? `${fmtNum(v[k].value, d)} ${v[k].unit}` : "–");
    const h = f.hourly;
    out.innerHTML = `
      ${f.status === "stale" ? `<div class="status-box warn">Sağlayıcıya şu an ulaşılamadı; son alınan veri gösteriliyor (alınma ${esc(fmtLocal(f.fetchedAt))}).</div>` : ""}
      <dl class="kv">
        <dt>Tür</dt><dd>Model tahmini <small>${esc(f.modelNote)}</small></dd>
        <dt>Model</dt><dd>${esc(f.model)} · ${esc(f.dataset)}</dd>
        <dt>Geçerlilik</dt><dd>Şimdi: ${esc(fmtLocal(cur.validAt) || "–")}<small>Sonraki 72 saat, saatlik</small></dd>
        <dt>Model ızgara noktası</dt><dd>${fmtNum(f.resolvedGridPoint?.lat, 3)}° K, ${fmtNum(f.resolvedGridPoint?.lon, 3)}° D · ${fmtNum(f.resolvedGridPoint?.elevationM)} m</dd>
        <dt>Alınma</dt><dd>${esc(fmtLocal(f.fetchedAt))} <small>1 saat sunucu önbelleği</small></dd>
      </dl>
      <div class="grid-main">
        <div class="metric wind">${F.windArrow(cur.wind10?.to, 40)}<dl><dt>10 m rüzgâr (şimdi)</dt><dd>${F.num(cur.wind10?.speed) ? fmtNum(cur.wind10.speed, 1) + " m/s" : "–"}<small>${esc(F.windText(cur.wind10))}</small></dd></dl></div>
        <dl class="metric"><dt>Sıcaklık (2 m)</dt><dd>${esc(val("temperature_2m"))}</dd></dl>
        <dl class="metric"><dt>Bulutluluk</dt><dd>${esc(val("cloud_cover", 0))}</dd></dl>
        <dl class="metric"><dt>Yağış</dt><dd>${esc(val("precipitation"))}<small>${esc(v.precipitation?.interval || "")}</small></dd></dl>
        <dl class="metric"><dt>Bağıl nem</dt><dd>${esc(val("relative_humidity_2m", 0))}</dd></dl>
        <dl class="metric"><dt>Yüzey basıncı</dt><dd>${esc(val("surface_pressure", 0))}</dd></dl>
      </div>
      <div class="charts">
        <div><p class="chart-title">Rüzgâr hızı (10 m)</p>${C.timeSeries({ times: h.time, title: "Rüzgâr hızı tahmini", unit: f.units.wind_speed_10m, zero: true, series: [{ values: h.wind_speed_10m, label: "10 m rüzgâr" }] })}</div>
        <div><p class="chart-title">Sıcaklık (2 m)</p>${C.timeSeries({ times: h.time, title: "Sıcaklık tahmini", unit: f.units.temperature_2m, series: [{ values: h.temperature_2m, label: "2 m sıcaklık" }] })}</div>
        ${h.cloud_cover ? `<div><p class="chart-title">Bulutluluk</p>${C.timeSeries({ times: h.time, title: "Bulutluluk tahmini", unit: f.units.cloud_cover, zero: true, max: 100, series: [{ values: h.cloud_cover, label: "bulutluluk", cls: "alt" }] })}</div>` : ""}
        <div><p class="chart-title">Yağış</p>${C.timeSeries({ times: h.time, bars: true, title: "Yağış tahmini", unit: f.units.precipitation, series: [{ values: h.precipitation, label: "önceki 1 saatin toplamı", cls: "bar" }] })}</div>
      </div>
      ${(f.missingVariables || []).length ? `<p class="hint">Bu modelde alınamayan değişkenler: ${esc(f.missingVariables.join(", "))}</p>` : ""}
      <p class="attrib">${esc(f.attribution)} · <a href="${esc(f.sourceUrl)}" target="_blank" rel="noopener">Open-Meteo belgeleri ↗</a>. Bu değerler anlık saha sensörü ölçümü değildir.</p>`;
  }

  // ---- İklim Bağlamı ---------------------------------------------------------------------------------
  const climReq = latestOnly();
  let pollTimer = null, polls = 0;
  async function renderClimate() {
    const out = $("clim-out");
    clearTimeout(pollTimer);
    if (st.lat == null) { out.innerHTML = `<div class="status-box">Önce bir konum seç.</div>`; return; }
    const req = climReq();
    if (!out.dataset.loaded) out.innerHTML = `<div class="status-box">Yükleniyor…</div>`;
    let c;
    const cmp = st.compare ? `&compare=${encodeURIComponent(st.compare)}` : "";
    try { c = await getJSON(`/api/climate?lat=${st.lat.toFixed(4)}&lon=${st.lon.toFixed(4)}&period=1991-2020${cmp}`, { signal: req.signal }); }
    catch (e) { if (!e.aborted && req.current()) out.innerHTML = `<div class="status-box warn">İklim verisi alınamadı: ${esc(e.message)}</div>`; return; }
    if (!req.current()) return;
    out.dataset.loaded = "1";
    if (c.status === "queued" || c.status === "unavailable") {
      const pr = c.progress, pct = Math.round((pr.yearsFetched / pr.yearsNeeded) * 100);
      const job = c.job;
      out.innerHTML = `<div class="status-box ${c.status === "unavailable" ? "warn" : ""}">
        <b>1991–2020 referansı hazırlanıyor</b> — ${pr.yearsFetched} / ${pr.yearsNeeded} yıl indirildi (ERA5 ızgara hücresi ${fmtNum(c.cell.lat, 2)}° K, ${fmtNum(c.cell.lon, 2)}° D).
        <div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>
        <p class="hint">İş: ${esc(F.STATUS_TEXT[job?.state] || job?.state || "kuyrukta")}${job?.errorClass ? ` · son hata: ${esc(F.ERROR_CLASS[job.errorClass] || job.errorClass)}` : ""}. Veri yıl yıl indirilir ve bu ızgara hücresi için kalıcı olarak saklanır; sayfa açık kaldıkça durum kendiliğinden yenilenir.${c.message ? " " + esc(c.message) : ""}</p></div>`;
      if (c.status === "queued" && polls++ < CLIMATE_POLL_MAX && st.tab === "clim") pollTimer = setTimeout(renderClimate, CLIMATE_POLL_MS);
      return;
    }
    polls = 0;
    const m = c.months, cm = c.comparisonMonth;
    const monthNames = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"];
    const cmpLabel = cm ? `${monthNames[cm.month - 1]} ${cm.year}` : null;
    const mk = (key, title, unit, zero) => C.monthly({ values: m.map((x) => x[key]), unit, zero, title,
      refLabel: "1991–2020 ortalaması", compare: cm ? { month: cm.month, value: cm.values[key], label: cmpLabel } : null });
    const options = (c.comparisonAvailable || []).slice().reverse().map((ym) => {
      const [y, mo] = ym.split("-").map(Number);
      return `<option value="${ym}" ${ym === (c.comparisonRequested || "") ? "selected" : ""}>${monthNames[mo - 1]} ${y}</option>`;
    }).join("");
    const diff = cm ? `<div class="diffs">
        <dl class="metric"><dt>Sıcaklık farkı</dt><dd>${F.signed(cm.diff.temp, 1)} °C<small>${fmtNum(cm.values.temp, 1)} °C · referans ${fmtNum(m[cm.month - 1].temp, 1)} °C</small></dd></dl>
        <dl class="metric"><dt>Yağış farkı</dt><dd>${F.signed(cm.diff.precip, 1)} mm<small>${fmtNum(cm.values.precip, 1)} mm · referans ${fmtNum(m[cm.month - 1].precip, 1)} mm${F.num(cm.precipPercent) ? ` · ${F.signed(cm.precipPercent, 0)}%` : ""}</small></dd></dl>
        <dl class="metric"><dt>Ortalama rüzgâr hızı farkı</dt><dd>${F.signed(cm.diff.wind, 2)} m/s<small>${fmtNum(cm.values.wind, 2)} m/s · referans ${fmtNum(m[cm.month - 1].wind, 2)} m/s</small></dd></dl>
      </div>` : `<div class="status-box">${c.comparisonRequested ? `${esc(c.comparisonRequested)} için eksiksiz veri yok; tamamlanmamış ay bütün ay referansıyla karşılaştırılmaz.` : "Karşılaştırılacak tamamlanmış ay yok."}</div>`;
    out.innerHTML = `
      <h2>1991–2020 aylık iklim referansı</h2>
      <dl class="kv">
        <dt>Konum</dt><dd>${esc(st.label)} · ERA5 ızgara noktası ${fmtNum(c.resolvedGridPoint.lat, 2)}° K, ${fmtNum(c.resolvedGridPoint.lon, 2)}° D (${fmtNum(c.resolvedGridPoint.elevationM)} m)</dd>
        <dt>Veri</dt><dd>${esc(c.accessPath)} · model ${esc(c.model)}<small>Yöntem sürümü ${esc(c.methodVersion)} · gün sınırı ${esc(c.dayBoundary)} · ${esc(c.calendar)}</small></dd>
        <dt>Kullanılan yıl</dt><dd>${c.validYearCount} / 30 <small>${esc(c.method.coverage)}</small></dd>
      </dl>
      <label class="field" style="margin-top:14px">Karşılaştırılan tamamlanmış ay (aynı model, aynı ızgara, aynı yöntem)
        <select id="cmp-select">${options}</select></label>
      ${diff}
      <div class="charts">
        <div><p class="chart-title">Aylık ortalama sıcaklık</p>${mk("temp", "Aylık ortalama sıcaklık", "°C", false)}</div>
        <div><p class="chart-title">Aylık toplam yağış</p>${mk("precip", "Aylık toplam yağış", "mm", true)}</div>
        <div><p class="chart-title">Aylık ortalama rüzgâr hızı (10 m)</p>${mk("wind", "Aylık ortalama rüzgâr hızı", "m/s", true)}</div>
      </div>
      <details class="extra"><summary>Sayısal tablo ve yöntem</summary>
        <div class="table-wrap"><table class="data"><thead><tr><th>Ay</th><th>Sıcaklık °C</th><th>Yıl</th><th>Yağış mm</th><th>Yıl</th><th>Rüzgâr m/s</th><th>Yıl</th></tr></thead><tbody>
        ${m.map((x, k) => `<tr><td>${monthNames[k]}</td><td>${fmtNum(x.temp, 2)}</td><td>${x.tempYears}</td><td>${fmtNum(x.precip, 1)}</td><td>${x.precipYears}</td><td>${fmtNum(x.wind, 2)}</td><td>${x.windYears}</td></tr>`).join("")}
        </tbody></table></div>
        <p class="hint">Sıcaklık: ${esc(c.method.temperature)} Yağış: ${esc(c.method.precipitation)} Rüzgâr: ${esc(c.method.wind)}</p>
      </details>
      <p class="hint">Bu göstergeler bölgenin iklim bağlamını verir; bir sahadaki metan salımının nedeni veya otomatik emisyon hesabı olarak kullanılmaz.</p>
      <p class="attrib">${esc(c.attribution)} · ERA5: Copernicus Climate Change Service.</p>`;
    $("cmp-select").onchange = (e) => { st.compare = e.target.value; writeUrl(); renderClimate(); };
  }

  // ---- Yükleme -------------------------------------------------------------------------------------
  const cache = { obs: null, fc: null, clim: null };
  function load() {
    if (st.tab === "obs" && cache.obs !== st.obsId) { cache.obs = st.obsId; renderObs(); }
    const key = st.lat == null ? null : `${st.lat.toFixed(4)},${st.lon.toFixed(4)}`;
    if (st.tab === "fc" && cache.fc !== key) { cache.fc = key; renderForecast(); }
    if (st.tab === "clim" && cache.clim !== key) { cache.clim = key; polls = 0; $("clim-out").dataset.loaded = ""; renderClimate(); }
  }

  async function init() {
    const lat = parseFloat(q.get("lat")), lon = parseFloat(q.get("lon"));
    const items = await loadObservationList();
    if (Number.isFinite(lat) && Number.isFinite(lon)) setLocation(lat, lon, "Seçilen nokta");
    else if (st.obsId) {
      const o = items.find((x) => x.id === st.obsId);
      if (o) setLocation(o.lat, o.lon, "Gözlemin tespit noktası");
    }
    if (st.lat == null) {
      // Varsayılan: en çok bulut kaydı bağlanmış Carbon Mapper kaynak kaydının koordinatı
      try {
        const s = await getJSON("/api/source-summaries");
        const top = s.items[0];
        if (top) setLocation(top.lat, top.lon, `Carbon Mapper kaynak kaydı koordinatı (${top.providerId})`);
      } catch (e) { $("loc-text").textContent = "Konum seçilmedi. Haritada bir nokta seç."; }
    }
    setTab(st.tab);
    if (window.MetanContent) window.MetanContent.renderWeatherClimate($("wc-content"));
  }
  window.addEventListener("pagehide", () => { obsReq(); fcReq(); climReq(); clearTimeout(pollTimer); });
  init();
})();
