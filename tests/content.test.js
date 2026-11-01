// Run: node --test tests/content.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const dir = path.join(__dirname, "..", "static", "metan-bilimsel-icerik");
const api = require(path.join(dir, "metan-icerik.js"));
const json = JSON.parse(fs.readFileSync(path.join(dir, "metan-icerik.json"), "utf8"));
const text = (o) => api.summarizeObservation(o).paragraphs.join(" ");

const plume = (extra = {}) => ({
  status: "detected", scope: "plume", sensor: "Planet Tanager-1", provider: "Carbon Mapper",
  observedAt: "2026-09-27T09:13:55.440Z", emission: { value: 605.3, unit: "kg/h", uncertainty: { value: 175, label: null } },
  ...extra,
});

test("JS-embedded content and JSON are the same edition", () => {
  assert.deepEqual(api.content, json);
  assert.equal(json.editionDate, "2026-11-01");
});

test("every paragraph reference exists in the bibliography", () => {
  const ids = new Set(json.references.map((r) => r.id));
  const paras = [...json.seymen.sections, ...json.science.sections].flatMap((s) => s.paragraphs);
  for (const p of [...paras, ...json.glossary]) for (const r of p.refs) assert.ok(ids.has(r), r);
});

test("symmetric uncertainty keeps the existing wording and missing label is stated", () => {
  const t = text(plume());
  assert.match(t, /simetrik belirsizlik: ±175/);
  assert.match(t, /istatistiksel tanımı belirtilmemiş/);
});

test("asymmetric interval is shown with its own bounds and label", () => {
  const t = text(plume({ emission: { value: 600, unit: "kg/h", uncertainty: { lower: 450, upper: 900, label: "%95 güven aralığı" } } }));
  assert.match(t, /belirsizlik aralığı: 450–900 kg CH₄\/saat\. %95 güven aralığı/);
  assert.doesNotMatch(t, /±/);
});

test("asymmetric interval not containing the value is rejected", () => {
  const t = text(plume({ emission: { value: 1000, unit: "kg/h", uncertainty: { lower: 450, upper: 900 } } }));
  assert.doesNotMatch(t, /aralığı: 450/);
});

test("missing or non-numeric emission never becomes zero", () => {
  for (const value of [null, "N/A", "605", undefined, NaN, -5]) {
    const t = text(plume({ emission: { value, unit: "kg/h" } }));
    assert.match(t, /kullanılabilir emisyon hızı tahmini sağlanmıyor/, String(value));
  }
});

test("not_detected without confirmed valid observation is downgraded to invalid", () => {
  assert.equal(api.summarizeObservation({ status: "not_detected" }).status, "invalid");
  assert.equal(api.summarizeObservation({ status: "not_detected", validObservationConfirmed: true }).status, "not_detected");
});

test("time without zone is not accepted", () => {
  assert.equal(api.formatTime("2026-09-27T09:13:55"), null);
  assert.ok(api.formatTime("2026-09-27T09:13:55Z"));
});

test("source_summary and plume scopes produce different interpretations", () => {
  assert.match(text(plume({ scope: "source_summary" })), /kaynak özetidir/);
  assert.match(text(plume()), /gözlem anını temsil eder/);
});
