/* Bilimsel içerik: tek kaynak JSON'dan sayfa içeriği (light DOM). Atıflar sayfadaki kaynakça kartlarına bağlanır;
   URL'si olmayan kaynak için bağlantı uydurulmaz, kartın araştırma notu açılır. Geliştirici metadata'sı gösterilmez. */
(function (global) {
  "use strict";
  const VERSION = "2026-11-01-insight-2";
  const URL_JSON = `/content/metan-icerik.${VERSION}.json`;
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeURL = (v) => { try { const u = new URL(v); return u.protocol === "https:" ? u.href : null; } catch (e) { return null; } };
  // Sayfa aralığındaki "PDF" önekleri yerel dosya düzenine aittir; ziyaretçiye yalnızca sayfa bilgisi gösterilir
  const pagesText = (p) => String(p || "").replace(/^PDF\s*/i, "").trim();

  let data = null;
  async function load() {
    if (data) return data;
    const r = await fetch(URL_JSON, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(`İçerik dosyası alınamadı (${r.status})`);
    data = await r.json();
    return data;
  }

  const refMap = () => new Map((data.references || []).map((r) => [r.id, r]));
  function cite(ids) {
    const m = refMap();
    const links = (ids || []).filter((id) => m.has(id))
      .map((id) => `<a href="#ref-${esc(id)}" data-ref="${esc(id)}" title="${esc(m.get(id).title)}">[${esc(id)}]</a>`);
    return links.length ? ` <span class="refs">${links.join(" ")}</span>` : "";
  }
  const para = (p) => `<p>${esc(p.text)}${cite(p.refs)}</p>`;
  const refsOf = (sections) => [...new Set((sections || []).flatMap((s) => (s.paragraphs || []).flatMap((p) => p.refs || [])))];

  function sectionsHtml(sections, prefix = "") {
    return (sections || []).map((s) => `<section id="${esc(prefix + s.id)}"><h2>${esc(s.title)}</h2>${(s.paragraphs || []).map(para).join("")}</section>`).join("");
  }
  function tocHtml(sections, prefix = "", extra = []) {
    return `<nav class="toc" aria-label="Sayfa içeriği"><strong>BU SAYFADA</strong>${(sections || []).map((s) => `<a href="#${esc(prefix + s.id)}">${esc(s.title)}</a>`).join("")}${extra.map(([id, t]) => `<a href="#${esc(id)}">${esc(t)}</a>`).join("")}</nav>`;
  }

  function refCard(r) {
    const u = safeURL(r.url);
    const title = u ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(r.title)} ↗</a>` : esc(r.title);
    const note = [r.scope && `<p>${esc(r.scope)}</p>`, pagesText(r.pages) && `<p>İlgili sayfalar: ${esc(pagesText(r.pages))}</p>`,
      !u && "<p>Bu kaynak için çevrim içi bağlantı verilmemiştir.</p>"].filter(Boolean).join("");
    return `<article class="card" id="ref-${esc(r.id)}"><span class="rid">${esc(r.id)}</span><h3>${title}</h3>
      <p class="meta">${esc(r.author)} · ${esc(r.year)}</p>
      ${note ? `<details class="research" ${u ? "" : "data-nourl"}><summary>Araştırma notu</summary>${note}</details>` : ""}</article>`;
  }
  function bibliography(ids, title = "Bu sayfadaki kaynaklar") {
    const m = refMap();
    const list = ids ? ids.map((id) => m.get(id)).filter(Boolean) : data.references || [];
    if (!list.length) return "";
    return `<section id="kaynakca"><h2>${esc(title)}</h2><div class="cards">${list.map(refCard).join("")}</div></section>`;
  }

  function glossaryHtml(items) {
    return `<div class="glossary cards">${(items || []).map((g) => `<details id="kavram-${esc(g.id)}"><summary>${esc(g.title)}</summary><p>${esc(g.text)}${cite(g.refs)}</p></details>`).join("")}</div>`;
  }

  // Bağlantı hedefi bir araştırma notu veya kapalı kavram ise aç
  function openTarget() {
    const id = decodeURIComponent(location.hash.slice(1));
    if (!id) return;
    const el = document.getElementById(id);
    if (!el) return;
    if (el.tagName === "DETAILS") el.open = true;
    el.querySelector("details.research")?.setAttribute("open", "");
    el.scrollIntoView({ block: "start" });
  }
  window.addEventListener("hashchange", openTarget);
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-ref]");
    if (!a) return;
    const card = document.getElementById(`ref-${a.dataset.ref}`);
    if (card && !safeURL(refMap().get(a.dataset.ref)?.url)) card.querySelector("details.research")?.setAttribute("open", "");
  });

  function page(root, { eyebrow, title, lead, sections, prefix = "", after = "", refs = null, tocExtra = [] }) {
    const ids = refs || refsOf(sections);
    root.innerHTML = `<header class="hero"><p class="eyebrow">${esc(eyebrow)}</p><h1>${esc(title)}</h1>${lead ? `<p class="lead">${esc(lead)}</p>` : ""}</header>
      <div class="layout">${tocHtml(sections, prefix, tocExtra.concat([["kaynakca", "Kaynaklar"]]))}
        <article>${sectionsHtml(sections, prefix)}${after}${bibliography(ids)}</article></div>
      <p class="footer">İçerik sürümü: ${esc(data.editionDate)}.${data.scopeNote ? " " + esc(data.scopeNote) : ""}</p>`;
    openTarget();
  }

  async function render(root, fn) {
    root.innerHTML = `<p class="loading">Yükleniyor…</p>`;
    try {
      await load();
      fn(root);
    } catch (e) {
      root.innerHTML = `<p class="error">İçerik yüklenemedi: ${esc(e.message)}. Sayfayı yenilemeyi deneyin.</p>`;
    }
  }

  // Bölüm biçimi bilinmeyen içerik nesneleri için genel çizici: {title, subtitle|lead|summary, sections}
  const leadOf = (c) => c.lead || c.summary || c.subtitle || "";

  global.MetanContent = {
    VERSION, load, esc, cite, bibliography, glossaryHtml,
    renderInsights: (root) => render(root, (r) => {
      const c = data.insights;
      page(r, { eyebrow: "Veriyi anlamak", title: c.title, lead: leadOf(c), sections: c.sections, prefix: "" });
    }),
    renderScience: (root) => render(root, (r) => {
      const c = data.science;
      const flow = c.flow?.length ? `<ol class="flow" aria-label="Ölçüm akışı">${c.flow.map((v, i) => `<li><b>0${i + 1}</b>${esc(v)}</li>`).join("")}</ol>` : "";
      const glossaryRefs = (data.glossary || []).flatMap((g) => g.refs || []);
      page(r, {
        eyebrow: "Bilimsel rehber", title: c.title, lead: c.lead, sections: c.sections,
        after: `<section id="kavramlar"><h2>Haritadaki kavramlar</h2><p>Her başlığı açarak açıklamasını ve dayandığı kaynakları okuyabilirsin.</p>${glossaryHtml(data.glossary)}</section>`,
        refs: [...new Set(refsOf(c.sections).concat(glossaryRefs))], tocExtra: [["kavramlar", "Haritadaki kavramlar"]],
      });
      r.querySelector("article").insertAdjacentHTML("afterbegin", flow);
    }),
    renderSeymen: (root, extraHtml = "") => render(root, (r) => {
      const c = data.seymen;
      page(r, { eyebrow: "Seymen", title: c.title, lead: c.summary, sections: c.sections, after: extraHtml,
        tocExtra: extraHtml ? [["kaynak-kaydi", "Kaynak kayıtları"]] : [] });
    }),
    renderReferences: (root) => render(root, (r) => {
      r.innerHTML = `<header class="hero"><p class="eyebrow">Kaynaklar</p><h1>Kaynaklar</h1>
        <p class="lead">Sitedeki açıklamaların dayandığı çalışmalar ve ürün belgeleri. Her kartın araştırma notu, kaynağın kapsamını ve ilgili sayfalarını gösterir.</p></header>
        ${bibliography(null, "Kaynakça")}<p class="footer">İçerik sürümü: ${esc(data.editionDate)}.</p>`;
      openTarget();
    }),
    renderWeatherClimate: (root) => {
      load().then(() => {
        const c = data.weatherClimate;
        if (!c) return;
        root.innerHTML = `<article><h2>${esc(c.title || "Hava ve iklim verisini okumak")}</h2>${leadOf(c) ? `<p>${esc(leadOf(c))}</p>` : ""}
          ${sectionsHtml(c.sections, "wc-")}${bibliography(refsOf(c.sections))}</article>`;
        openTarget();
      }).catch((e) => { root.innerHTML = `<p class="hint">Açıklama metinleri yüklenemedi: ${esc(e.message)}</p>`; });
    },
  };
})(window);
