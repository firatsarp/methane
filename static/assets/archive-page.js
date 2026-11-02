/* Yüksek Olay Arşivi: kalıcı arşivdeki kayıtlar, seçim gerekçesi, dosya arşiv durumu ve saklama özeti. */
(function () {
  "use strict";
  const F = window.Fmt;
  const { esc, fmtNum, fmtLocal, fmtUtc, getJSON } = F;
  const $ = (id) => document.getElementById(id);

  const STATE = {
    peak_candidate: ["İnceleme adayı", "Serinin önceki 90 gününe göre robust kural (z ≥ 3,5) veya açıkça tanımlanmış mutlak eşik. Doğrulanmış yüksek salım değildir."],
    reviewed_peak: ["İncelenmiş yüksek olay", "İnsan incelemesiyle onaylanmış kayıt."],
    historical_high_reference: ["Serinin en yüksek gözlemlerinden", "Mevcut örneklemdeki en yüksek gözlemler; istatistiksel pik doğrulanmadı. Normal düzeyin bilindiği veya kaynağın tehlikeli olduğu anlamına gelmez."],
  };
  const ASSET = { archived: "Arşivlendi", not_archived: "Arşivlenemedi", fetch_failed: "Alınamadı" };
  const ROLE = { numeric_raster: "Sayısal artış GeoTIFF", plume_png: "Bulut görüntüsü (PNG)", thumbnail: "Önizleme görüntüsü" };

  function reasonText(r) {
    if (r.rule === "sparse_top_n") return `Serinin ${r.of} gözlemi içinde ${r.rank}. en yüksek`;
    if (r.rule === "robust_z") return `Robust z = ${fmtNum(r.z, 1)}`;
    if (r.rule === "absolute_threshold") return `Mutlak eşik ≥ ${fmtNum(r.thresholdKgh)} kg/sa (yönetici tanımı)`;
    if (r.rule === "human_review") return "İnsan incelemesi";
    if (r.rule === "no_series") return null;
    return r.rule;
  }

  function card(e) {
    const [label, help] = STATE[e.state] || [e.state, ""];
    const reasons = (e.reasons || []).map(reasonText).filter(Boolean);
    const assets = e.assets.length ? e.assets.map((a) => `<li>${esc(ROLE[a.role] || a.role)}: <b>${esc(ASSET[a.status] || a.status)}</b>${a.url ? ` · <a href="${esc(a.url)}">dosya</a> <small>sha256 ${esc(a.sha256.slice(0, 12))}…</small>` : ""}${a.note ? ` <small>${esc(a.note)}</small>` : ""}</li>`).join("")
      : "<li>Konumlu görüntü veya sayısal dosya yayımlanmamış; kimlik ve köken bilgisi saklanır.</li>";
    return `<article class="card ev" id="${esc(e.id)}">
      <header class="ev-head"><span class="pill s-${esc(e.state)}">${esc(label)}</span>
        <h2>${esc(e.locationLabel || `${fmtNum(e.lat, 3)}° K, ${fmtNum(e.lon, 3)}° D`)}</h2>
        <p class="ev-sub">${esc(fmtLocal(e.observedAt) || "zaman yok")} <small>${esc(fmtUtc(e.observedAt) || "")}</small> · ${esc(e.sensor || "?")} · ${esc(e.provider || "?")}</p></header>
      <p class="ev-num">${e.value != null ? `<b>${fmtNum(e.value)}</b> ${esc(e.unit || "")} <small>sağlayıcının tek gözlem tahmini</small>` : "Sağlayıcı tahmini yok (sıfır değildir)"}</p>
      <p class="hint">${esc(help)}${reasons.length ? ` <b>Gerekçe:</b> ${esc(reasons.join("; "))}.` : ""}</p>
      <details><summary>Paket ayrıntısı</summary>
        <dl class="kv">
          <dt>Kayıt</dt><dd>${esc(e.id)}</dd>
          <dt>Arşive giriş</dt><dd>${esc(fmtLocal(e.archivedAt) || "–")}</dd>
          <dt>Sağlayıcı revizyonları</dt><dd>${fmtNum(e.revisions)}</dd>
          <dt>Gözlem anı meteorolojisi</dt><dd>${e.hasWeather ? "Pakette" : "Henüz yok (sonraki günlük çalışmada tamamlanır)"}</dd>
          <dt>Yorum analizi</dt><dd>${e.hasAnalysis ? "Pakette" : "Henüz yok"}</dd>
          <dt>Kural sürümü</dt><dd>${esc(e.policyVersion || "–")}</dd>
        </dl>
        <ul class="assets">${assets}</ul>
      </details>
      <div class="ev-actions"><a class="btn" href="/?sel=${encodeURIComponent(e.id)}">Haritada aç</a>
        <a class="btn" href="/karsilastirma.html?a=${encodeURIComponent(e.id)}">Karşılaştır</a></div>
    </article>`;
  }

  async function loadRetention() {
    try {
      const r = await getJSON("/api/retention");
      const ev = r.archive.events || {};
      const total = Object.values(ev).reduce((a, b) => a + b, 0);
      const last = r.pipelineRuns?.[0];
      $("ret").innerHTML = `<dl class="kv">
        <dt>Normal gözlem penceresi</dt><dd>Son ${fmtNum(r.policy.ordinaryObservationDays)} gün <small>Sınır: ${esc(fmtLocal(r.cutoff))} öncesi gözlem zamanına göre silinir</small></dd>
        <dt>Kalıcı arşivdeki kayıt</dt><dd>${fmtNum(total)}</dd>
        <dt>Arşivlenmiş dosya</dt><dd>${fmtNum(r.archive.assets)} <small>${fmtNum(r.archive.assetBytes / 1024)} KB · sha256 ile doğrulanır</small></dd>
        <dt>Son günlük çalışma</dt><dd>${last ? `${esc(fmtLocal(last.finishedAt || last.startedAt))} · ${esc(last.state === "done" ? "tamamlandı" : last.state)}` : "Henüz yok"}</dd>
      </dl>`;
    } catch (e) {
      $("ret").innerHTML = `<div class="status-box">Saklama durumu alınamadı: ${esc(e.message)}</div>`;
    }
  }

  async function load() {
    const state = $("f-state").value;
    $("list").innerHTML = '<div class="status-box">Yükleniyor…</div>';
    try {
      const r = await getJSON(`/api/peaks${state ? `?state=${encodeURIComponent(state)}` : ""}`);
      $("count").textContent = `${fmtNum(r.items.length)} kayıt`;
      $("list").innerHTML = r.items.length ? r.items.map(card).join("")
        : '<div class="status-box">Bu seçimle arşivde kayıt yok. Bu, yüksek salım olmadığı anlamına gelmez.</div>';
      if (location.hash) document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView();
    } catch (e) {
      $("list").innerHTML = `<div class="status-box">Arşiv alınamadı: ${esc(e.message)}</div>`;
    }
  }
  $("f-state").onchange = load;
  loadRetention();
  load();
})();
