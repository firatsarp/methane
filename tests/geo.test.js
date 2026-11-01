// Run: node --test tests/geo.test.js
// Not: sınır dosyası sadeleştirilmiştir; kıyıya çok yakın noktalar (ör. Kadıköy) çizginin dışında kalabilir. Testte iç noktalar kullanılır.
// Türkiye sınırı yardımcıları: gerçek static/turkey.geojson ile nokta testi ve maske geometrisi.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const geoLib = require(path.join(__dirname, "..", "static", "assets", "geo.js"));

const gj = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "static", "turkey.geojson"), "utf8"));
const geo = geoLib.create(gj);

test("points inside and outside Turkey use the real border polygon, not the bbox", () => {
  for (const [name, lon, lat] of [["Ankara", 32.86, 39.93], ["Edirne", 26.56, 41.68], ["İstanbul Fatih", 28.95, 41.02], ["Van", 43.38, 38.5], ["Gökçeada", 25.85, 40.17]]) {
    assert.ok(geo.contains(lon, lat), name);
  }
  // Hepsi Türkiye kutusunun içinde ama ülke dışında
  for (const [name, lon, lat] of [["Lefkoşa", 33.36, 35.17], ["Halep", 37.16, 36.2], ["Tiflis yakını", 44.7, 41.7], ["Atina yakını (kutu dışı)", 23.7, 37.98], ["Sofya", 23.32, 42.7], ["Selanik", 22.94, 40.64]]) {
    assert.equal(geo.contains(lon, lat), false, name);
  }
});

test("mask covers the world with every Turkey part as an opposite-wound hole", () => {
  const [outer, ...holes] = geo.mask.geometry.coordinates[0];
  const parts = gj.features.flatMap((f) => (f.geometry.type === "MultiPolygon" ? f.geometry.coordinates : [f.geometry.coordinates]));
  assert.equal(holes.length, parts.length);
  const sign = Math.sign(geoLib.signedArea(outer));
  for (const h of holes) assert.equal(Math.sign(geoLib.signedArea(h)), -sign);
  // Sınır geometrisi değiştirilmez: delikler özgün halkalarla aynı noktaları taşır
  for (const [k, p] of parts.entries()) {
    const pts = new Set(p[0].map((c) => c.join(",")));
    assert.ok(holes[k].every((c) => pts.has(c.join(","))));
  }
});

test("camera bounds come from the border geometry", () => {
  const [[w, s], [e, n]] = geo.bounds;
  assert.ok(w > 25 && w < 26.5 && e > 44 && e < 45.5 && s > 35.5 && s < 36.2 && n > 41.8 && n < 42.5);
  const [[mw, ms], [me, mn]] = geo.maxBounds;
  assert.ok(mw < w && me > e && ms < s && mn > n);
});
