// Run: node --test tests/content.test.js
// Bilimsel içerik sözleşmesi: tek kaynak JSON (sürüm 2026-11-01-insight-2) ve sayfaların ona bağlanması.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const VERSION = "2026-11-01-insight-2";
const staticDir = path.join(__dirname, "..", "static");
const file = path.join(staticDir, "content", `metan-icerik.${VERSION}.json`);
const json = JSON.parse(fs.readFileSync(file, "utf8"));

// Ziyaretçiye gösterilen metinler (geliştirici metadata'sı hariç)
function visibleTexts(c) {
  const out = [];
  const walkSections = (obj) => {
    if (!obj) return;
    for (const k of ["title", "subtitle", "summary", "lead"]) if (obj[k]) out.push(obj[k]);
    for (const s of obj.sections || []) {
      out.push(s.title);
      for (const p of s.paragraphs || []) out.push(p.text);
    }
  };
  walkSections(c.seymen);
  walkSections(c.science);
  walkSections(c.insights);
  walkSections(c.weatherClimate);
  for (const g of c.glossary || []) out.push(g.title, g.text);
  for (const r of c.references || []) out.push(r.title, r.author, r.scope || "");
  if (c.scopeNote) out.push(c.scopeNote);
  return out;
}
const sectionParas = (obj) => (obj?.sections || []).flatMap((s) => s.paragraphs || []);

test("content edition and required parts", () => {
  assert.equal(json.editionDate, "2026-11-01");
  for (const key of ["seymen", "science", "insights", "weatherClimate", "glossary", "references"]) {
    assert.ok(json[key], `eksik bölüm: ${key}`);
  }
});

test("every reference id used in text exists in the bibliography", () => {
  const ids = new Set(json.references.map((r) => r.id));
  const paras = ["seymen", "science", "insights", "weatherClimate"].flatMap((k) => sectionParas(json[k]));
  for (const p of [...paras, ...json.glossary]) for (const r of p.refs || []) assert.ok(ids.has(r), `kaynakçada yok: ${r}`);
});

test("references keep real author, title, year and only https links", () => {
  for (const r of json.references) {
    assert.ok(r.author && r.title && Number.isInteger(r.year), r.id);
    if (r.url != null) assert.match(r.url, /^https:\/\//, r.id);
  }
});

test("no forbidden themes or development text in visitor-facing content", () => {
  const text = visibleTexts(json).join("\n");
  for (const bad of [/Kanadevia/i, /AB-27\+BK/, /bu paket/i, /hazır içerik bileşeni/i, /Claude/, /Codex/, /\.pdf\b/i, /sohbet/i]) {
    assert.doesNotMatch(text, bad);
  }
});

test("7,4 ton/saat appears only in the Seymen defined-period summary", () => {
  const where = ["science", "insights", "weatherClimate"].flatMap((k) => sectionParas(json[k]).map((p) => p.text));
  for (const t of where.concat(json.glossary.map((g) => g.text))) assert.doesNotMatch(t, /7,4\s*ton/);
  assert.ok(sectionParas(json.seymen).some((p) => /7,4\s*ton/.test(p.text)));
});

test("Seymen page is static, readable without JS and has closed sources at the bottom", () => {
  const html = fs.readFileSync(path.join(staticDir, "seymen.html"), "utf8");
  assert.match(html, /<h1>Seymen ölçümleri nasıl okunur\?<\/h1>/);
  for (const s of json.seymen.sections) {
    assert.match(html, new RegExp(`<section id="${s.id}"><h2>`), s.id);
    for (const p of s.paragraphs) assert.ok(html.includes(p.text), `${s.id}: paragraf eksik`);
  }
  // Kaynaklar sayfanın sonunda, kapalı başlayan details içinde; gövdede kaynak kodu yok
  assert.match(html, /<details class="sources"><summary>Kaynaklar<\/summary>/);
  assert.doesNotMatch(html, /<details class="sources" open/);
  assert.ok(html.indexOf('class="sources"') > html.lastIndexOf("</section>"));
  assert.doesNotMatch(html, /\[(G21|S24|SC23|B21|UCLA26|UCLAM|GU26|CM26)\]/);
  for (const bad of [/metan-icerik\.js/, /metan-bilimsel-icerik/, /metan-seymen/, /İDDİADAN KAYNAĞA/, /Kaynaklar ve kapsamları/, /AB-27/, /Sağlanan belge/]) {
    assert.doesNotMatch(html, bad);
  }
});

test("pages load the versioned content file, not the old package", () => {
  for (const page of ["veriyi-anlamak.html", "uydular-nasil-olcer.html", "kaynaklar.html", "hava-ve-iklim.html"]) {
    const html = fs.readFileSync(path.join(staticDir, page), "utf8");
    assert.doesNotMatch(html, /metan-bilimsel-icerik/, page);
    assert.match(html, /\/assets\/content\.js\?v=2026-11-01-insight-2/, page);
  }
  const js = fs.readFileSync(path.join(staticDir, "assets", "content.js"), "utf8");
  assert.match(js, new RegExp(`VERSION = "${VERSION}"`));
});

test("every page has the same six-item navigation and a way back to the map", () => {
  const labels = ["Harita", "Veriyi Anlamak", "Uydular Nasıl Ölçer?", "Hava ve İklim", "Seymen", "Kaynaklar"];
  for (const page of ["index.html", "veriyi-anlamak.html", "uydular-nasil-olcer.html", "hava-ve-iklim.html", "seymen.html", "kaynaklar.html"]) {
    const html = fs.readFileSync(path.join(staticDir, page), "utf8");
    const nav = html.match(/<nav id="site-menu"[\s\S]*?<\/nav>/)[0];
    assert.deepEqual([...nav.matchAll(/>([^<]+)<\/a>/g)].map((m) => m[1]), labels, page);
    if (page !== "index.html") assert.match(html, /Haritaya dön/, page);
  }
});
