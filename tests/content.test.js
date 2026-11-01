// Run: node --test tests/content.test.js
// Genel okuma rehberi (sürüm 2026-11-01-general-3): tek kaynak JSON ve sunulan sayfaların ona uyması.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const VERSION = "2026-11-01-general-3";
const staticDir = path.join(__dirname, "..", "static");
const json = JSON.parse(fs.readFileSync(path.join(staticDir, "content", `metan-icerik.${VERSION}.json`), "utf8"));
const read = (f) => fs.readFileSync(path.join(staticDir, f), "utf8");
const decode = (s) => s.replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">");

const PAGES = { highValues: "yuksek-degerleri-yorumlamak.html", insights: "veriyi-anlamak.html",
  science: "uydular-nasil-olcer.html", weatherClimate: "hava-ve-iklim.html" };
const ALL = ["index.html", "yuksek-degerleri-yorumlamak.html", "veriyi-anlamak.html", "uydular-nasil-olcer.html",
  "hava-ve-iklim.html", "kaynaklar.html"];

test("content version and section counts", () => {
  assert.equal(json.schemaVersion, "3.0.0");
  assert.equal(json.contentVersion, VERSION);
  assert.equal(json.highValues.sections.length, 8);
  assert.equal(json.science.sections.length, 13);
  assert.equal(json.insights.sections.length, 8);
  assert.equal(json.weatherClimate.sections.length, 6);
  assert.equal(json.glossary.length, 14);
  assert.equal(json.references.length, 22);
  assert.equal(json.seymen, undefined);
});

test("every reference id used in text exists", () => {
  const ids = new Set(json.references.map((r) => r.id));
  for (const k of Object.keys(PAGES)) for (const s of json[k].sections) for (const p of s.paragraphs)
    for (const r of p.refs) assert.ok(ids.has(r), `${k}/${s.id}: ${r}`);
  for (const g of json.glossary) for (const r of g.refs) assert.ok(ids.has(r), `glossary/${g.id}: ${r}`);
});

test("served pages carry exactly the JSON texts (single content source)", () => {
  for (const [key, file] of Object.entries(PAGES)) {
    const html = decode(read(file));
    for (const s of json[key].sections) {
      assert.ok(html.includes(`id="${s.id}"`), `${file}: bölüm ${s.id}`);
      for (const p of s.paragraphs) assert.ok(html.includes(p.text), `${file}/${s.id}: paragraf eksik`);
    }
  }
});

test("no location-specific editorial, citation codes or development text on pages", () => {
  for (const file of ALL) {
    const html = read(file);
    for (const bad of [/Seymen/i, /Silivri/i, /7,4\s*ton/, /Guardian/i, /Kanadevia/i, /AB-27/, /İDDİADAN KAYNAĞA/,
      /Kaynaklar ve kapsamları/, /Sağlanan belge/, /metan-icerik\.js/, /metan-bilimsel-icerik/, /\/assets\/content\.js/]) {
      assert.doesNotMatch(html, bad, `${file}: ${bad}`);
    }
    // Gövdede [G21] gibi kaynak kodu görünmez
    assert.doesNotMatch(html.replace(/<script[\s\S]*?<\/script>/g, ""), /\[(?:[A-Z]{1,5}\d{2}|UCLAM|TP|ERA5|OMH|OMF)\]/, file);
  }
});

test("reading pages end with a closed Kaynaklar list of real links", () => {
  for (const file of ["yuksek-degerleri-yorumlamak.html", "veriyi-anlamak.html", "uydular-nasil-olcer.html", "hava-ve-iklim.html"]) {
    const html = read(file);
    const at = html.indexOf('<details class="sources"><summary>Kaynaklar</summary>');
    assert.ok(at > 0, file);
    assert.ok(at > html.lastIndexOf('<section id="', at), `${file}: kaynaklar sonda değil`);
    assert.doesNotMatch(html, /<details class="sources" open/);
    assert.match(html.slice(at), /<li><span class="author">[^<]+<\/span><a href="https:\/\//);
  }
  const refs = decode(read("kaynaklar.html"));
  for (const r of json.references.filter((x) => x.url)) assert.ok(refs.includes(r.title), `kaynaklar: ${r.id}`);
});

test("same six-item visible navigation everywhere, with way back to the map", () => {
  const labels = ["Harita", "Yüksek Değerler", "Veriyi Anlamak", "Uydular Nasıl Ölçer?", "Hava ve İklim", "Kaynaklar"];
  for (const file of ALL) {
    const html = read(file);
    for (const cls of ['id="site-menu"', 'class="mobile-links"']) {
      const nav = html.match(new RegExp(`<nav ${cls}[^>]*>[\\s\\S]*?</nav>`))[0];
      assert.deepEqual([...nav.matchAll(/>([^<]+)<\/a>/g)].map((m) => m[1]), labels, `${file} ${cls}`);
    }
    assert.match(html, /<details class="mobile-menu"><summary>Menü<\/summary>/, file);
    if (file !== "index.html") assert.match(html, /Haritaya dön/, file);
  }
});

test("map uses the general reading note and the current content file", () => {
  const html = read("index.html");
  assert.match(html, /Yüksek bir metan değeri gördüğümde neye bakmalıyım\?/);
  assert.match(html, /href="\/yuksek-degerleri-yorumlamak.html">Yüksek değerleri yorumlama rehberini aç/);
  assert.match(read("assets/map.js"), new RegExp(`metan-icerik\\.${VERSION}\\.json`));
  const ids = new Set(json.glossary.map((g) => g.id));
  for (const id of ["emisyon", "belirsizlik", "gecikme", "ruzgar"]) assert.ok(ids.has(id), id);
});
