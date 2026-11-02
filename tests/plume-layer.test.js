// Run: node --test tests/plume-layer.test.js
// Seçime bağlı tek bulut katmanı: haritadaki plume raster sayısı en fazla 1, geç yanıt yeni seçimi kirletmez.
const test = require("node:test");
const assert = require("node:assert/strict");
const { createPlumeLayer, LAYER_ID } = require("../static/assets/plume-layer.js");

function fakeMap() {
  const layers = [];
  const sources = {};
  return {
    layers, sources,
    getLayer: (id) => layers.find((l) => l.id === id),
    getSource: (id) => sources[id],
    addSource: (id, s) => { if (sources[id]) throw new Error(`source exists ${id}`); sources[id] = s; },
    removeSource: (id) => { delete sources[id]; },
    addLayer: (l) => { if (layers.find((x) => x.id === l.id)) throw new Error(`layer exists ${l.id}`); layers.push(l); },
    removeLayer: (id) => { layers.splice(layers.findIndex((l) => l.id === id), 1); },
    getStyle: () => ({ layers }),
    simulateStyleReset() { layers.length = 0; for (const k of Object.keys(sources)) delete sources[k]; },
  };
}

// Kontrol edilebilir fetch: her URL için yanıtı testin istediği anda çözer
function deferredFetch() {
  const pending = new Map();
  const impl = (url, { signal }) => new Promise((resolve, reject) => {
    pending.set(url, () => resolve({ ok: true, blob: async () => new Blob([url]) }));
    signal.addEventListener("abort", () => { const e = new Error("aborted"); e.name = "AbortError"; reject(e); });
  });
  return { impl, resolve: (url) => pending.get(url)() };
}

const obs = (id, png = `https://x/${id}.png`) => ({ id, raster: { png, bounds: [35, 37, 35.1, 37.1], label: "test" } });
const tick = () => new Promise((r) => setImmediate(r));

test("0 at start, 1 after A, 1 after B, 0 after close", async () => {
  const map = fakeMap();
  const f = deferredFetch();
  const pl = createPlumeLayer(map, { fetchImpl: f.impl });
  assert.equal(pl.count(), 0);
  const a = pl.show(obs("A")); f.resolve("https://x/A.png"); await a;
  assert.equal(pl.count(), 1);
  assert.equal(pl.activeId(), "A");
  const b = pl.show(obs("B")); f.resolve("https://x/B.png"); await b;
  assert.equal(pl.count(), 1);
  assert.equal(pl.activeId(), "B");
  await pl.clear();
  assert.equal(pl.count(), 0);
  assert.equal(Object.keys(map.sources).length, 0);
});

test("late response for A never draws over B", async () => {
  const map = fakeMap();
  const states = [];
  const slow = new Map();
  const impl = (url) => new Promise((resolve) => slow.set(url, () => resolve({ ok: true, blob: async () => new Blob([url]) })));
  const pl = createPlumeLayer(map, { fetchImpl: impl, onState: (s) => states.push(s) });
  const a = pl.show(obs("A"));
  const b = pl.show(obs("B"));
  slow.get("https://x/B.png")();
  await b;
  slow.get("https://x/A.png")();  // A geç döner (iptale uymayan istemci senaryosu)
  await a;
  await tick();
  assert.equal(pl.count(), 1);
  assert.equal(pl.activeId(), "B");
  assert.equal(map.sources[LAYER_ID].url.length > 0, true);
});

test("observation without positioned raster keeps zero layers and reports reason", async () => {
  const map = fakeMap();
  let last;
  const pl = createPlumeLayer(map, { fetchImpl: async () => { throw new Error("should not fetch"); }, onState: (s) => (last = s) });
  await pl.show({ id: "N", thumbnail: { url: "https://x/t.png" }, raster: null });
  assert.equal(pl.count(), 0);
  assert.equal(last.status, "no_raster");
  assert.match(last.reason, /konumsuz/);
});

test("repeated selection of the same item, pan/zoom and style restore never duplicate", async () => {
  const map = fakeMap();
  const f = deferredFetch();
  const pl = createPlumeLayer(map, { fetchImpl: f.impl });
  for (let k = 0; k < 3; k++) { const p = pl.show(obs("A")); f.resolve("https://x/A.png"); await p; }
  assert.equal(pl.count(), 1);
  pl.restore(); pl.restore();             // stil yenilemesi yokken: değişiklik yok
  assert.equal(pl.count(), 1);
  map.simulateStyleReset();               // setStyle sonrası dinamik katmanlar silinir
  pl.restore();
  assert.equal(pl.count(), 1);
});

test("fetch error does not leave a layer", async () => {
  const map = fakeMap();
  let last;
  const pl = createPlumeLayer(map, { fetchImpl: async () => ({ ok: false, status: 403 }), onState: (s) => (last = s) });
  await pl.show(obs("E"));
  assert.equal(pl.count(), 0);
  assert.equal(last.status, "error");
});
