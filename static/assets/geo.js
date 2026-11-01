/* Türkiye sınırı yardımcıları: tek sınır kaynağı static/turkey.geojson.
   Kamera kutusu, nokta-poligon testi (MultiPolygon ve delikler) ve ülke dışını örten maske geometrisi. */
(function (global) {
  "use strict";

  // Halka yönü: pozitif alan = saat yönünün tersi (lon/lat düzleminde)
  const signedArea = (ring) => {
    let a = 0;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) a += (ring[j][0] - ring[i][0]) * (ring[j][1] + ring[i][1]);
    return a / 2;
  };
  const oriented = (ring, ccw) => ((signedArea(ring) > 0) === ccw ? ring : ring.slice().reverse());

  function polygonsOf(gj) {
    const out = [];
    for (const f of gj.features || [gj]) {
      const g = f.geometry || f;
      if (g.type === "Polygon") out.push(g.coordinates);
      else if (g.type === "MultiPolygon") out.push(...g.coordinates);
    }
    return out;
  }

  function ringContains(ring, lon, lat) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i], [xj, yj] = ring[j];
      if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  function create(gj) {
    const polys = polygonsOf(gj);
    let w = 180, s = 90, e = -180, n = -90;
    for (const p of polys) for (const [x, y] of p[0]) { w = Math.min(w, x); e = Math.max(e, x); s = Math.min(s, y); n = Math.max(n, y); }
    const bounds = [[w, s], [e, n]];
    // Sınır kuralı: noktanın kendisi dış halkanın içinde ve hiçbir deliğin içinde değilse Türkiye içidir
    // (ışın testi; arka uçtaki app/geo.py ile aynı kural)
    const contains = (lon, lat) => polys.some((rings) => ringContains(rings[0], lon, lat) && !rings.slice(1).some((h) => ringContains(h, lon, lat)));
    // Maske: dünya halkası + Türkiye dış halkaları delik olarak. Türkiye içindeki delikler (varsa) ayrıca örtülür.
    const world = oriented([[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]], true);
    const maskPolys = [[world, ...polys.map((p) => oriented(p[0], false))]];
    for (const p of polys) for (const hole of p.slice(1)) maskPolys.push([oriented(hole, true)]);
    const mask = { type: "Feature", properties: {}, geometry: { type: "MultiPolygon", coordinates: maskPolys } };
    const outline = { type: "FeatureCollection", features: (gj.features || [gj]) };
    // Kamera sınırı: ülke kutusu + geniş pay. Pay, geniş ekranda yan panel ve dar/uzun telefon ekranında ülkenin tamamının sığabilmesi
    // için gereklidir; ülke dışında görünen alan maskeyle tamamen örtülüdür.
    const maxBounds = [[w - 30, s - 28], [e + 30, n + 28]];
    return { bounds, maxBounds, contains, mask, outline };
  }

  async function load(url = "/turkey.geojson") {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`Türkiye sınırı alınamadı (${r.status})`);
    return create(await r.json());
  }

  global.TurkeyGeo = { load, create, ringContains, signedArea };
  if (typeof module !== "undefined" && module.exports) module.exports = global.TurkeyGeo;
})(typeof window !== "undefined" ? window : globalThis);
