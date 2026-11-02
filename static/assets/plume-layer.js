/* Seçime bağlı tek bulut katmanı. Haritada aynı anda en fazla bir plume rasterı bulunur.
   - Açılışta 0; yalnızca açıkça seçilen gözlemin görüntüsü yüklenir. Pan/zoom yeni katman açmaz.
   - Her seçim yeni bir nesil (generation) başlatır ve önceki isteği AbortController ile iptal eder; A'nın geç
     dönen görüntüsü B'nin üstüne çizilemez.
   - Görüntü yoksa nokta/ayrıntı korunur, neden onState ile bildirilir. Sayısal lejant uydurulmaz.
   Tarayıcıda window.PlumeLayer, Node testlerinde module.exports. */
(function (root) {
  "use strict";
  const ID = "plume-active";

  function createPlumeLayer(map, { before = null, fetchImpl = (u, o) => fetch(u, o), onState = () => {} } = {}) {
    let gen = 0;
    let ctrl = null;
    let active = null; // { id, bounds, objectUrl, meta }

    function removeLayer() {
      if (map.getLayer(ID)) map.removeLayer(ID);
      if (map.getSource(ID)) map.removeSource(ID);
    }
    function draw() {
      removeLayer();
      if (!active) return;
      const b = active.bounds;
      map.addSource(ID, { type: "image", url: active.objectUrl,
        coordinates: [[b[0], b[3]], [b[2], b[3]], [b[2], b[1]], [b[0], b[1]]] });
      map.addLayer({ id: ID, type: "raster", source: ID, paint: { "raster-fade-duration": 0 } },
        before && map.getLayer(before) ? before : undefined);
    }
    function release() {
      removeLayer();
      if (active?.objectUrl) URL.revokeObjectURL(active.objectUrl);
      active = null;
    }

    async function show(obs) {
      const g = ++gen;
      if (ctrl) ctrl.abort();
      ctrl = null;
      release();
      if (!obs) { onState({ status: "none" }); return; }
      const r = obs.raster;
      const bounds = Array.isArray(r?.bounds) ? r.bounds : null;
      if (!r?.png || !bounds || bounds.length !== 4) {
        onState({ status: "no_raster", id: obs.id,
          reason: obs.thumbnail?.url ? "Sağlayıcı yalnızca konumsuz önizleme görüntüsü yayımlamış; haritaya yerleştirilemez."
            : "Bu kayıt için yayımlanmış konumlu bulut görüntüsü yok." });
        return;
      }
      const c = new AbortController();
      ctrl = c;
      onState({ status: "loading", id: obs.id });
      try {
        const res = await fetchImpl(r.png, { signal: c.signal });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        if (g !== gen) return;               // Bu arada başka seçim yapıldı: geç yanıt çizilmez
        active = { id: obs.id, bounds, objectUrl: URL.createObjectURL(blob), meta: r };
        draw();
        onState({ status: "ready", id: obs.id, label: r.label });
      } catch (e) {
        if (g !== gen || e.name === "AbortError") return;
        onState({ status: "error", id: obs.id, reason: "Bulut görüntüsü alınamadı (bağlantının süresi dolmuş olabilir)." });
      } finally {
        if (ctrl === c) ctrl = null;
      }
    }

    return {
      show,
      clear: () => show(null),
      /* Stil yeniden kurulduysa (setStyle) dinamik katman tekil olarak geri eklenir */
      restore: () => { if (active && !map.getLayer(ID)) draw(); },
      activeId: () => active?.id ?? null,
      count: () => (map.getStyle()?.layers || []).filter((l) => l.type === "raster" && (l.id === ID || l.id.startsWith("img-"))).length,
    };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { createPlumeLayer, LAYER_ID: ID };
  else root.PlumeLayer = { createPlumeLayer, LAYER_ID: ID };
})(typeof window !== "undefined" ? window : globalThis);
