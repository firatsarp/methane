/* Seymen sayfası için isteğe bağlı ek: Seymen çevresindeki Carbon Mapper kaynak kayıtlarına bağlantı.
   Sayfa metni HTML'dedir ve JavaScript olmadan okunur; bu betik yalnızca canlı kaynak listesini ekler.
   Konum dayanağı: UCLA STOP Methane yöntem sayfasının Silivri–Seymen için verdiği konum. Yakınlık tesis doğrulaması değildir. */
(function () {
  "use strict";
  const SEYMEN = { lat: 41.217722, lon: 28.145806 };
  const UCLAM_URL = "https://law.ucla.edu/news/spotlight-top-25-methane-plumes-2025-landfills";
  const RADIUS_M = 1500;
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const nf = (v, d = 0) => (typeof v === "number" && Number.isFinite(v) ? v.toLocaleString("tr-TR", { maximumFractionDigits: d }) : "–");
  const dist = (a, b) => {
    const r = 6371000, p1 = a.lat * Math.PI / 180, p2 = b.lat * Math.PI / 180;
    const dp = p2 - p1, dl = (b.lon - a.lon) * Math.PI / 180;
    const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
    return 2 * r * Math.asin(Math.sqrt(h));
  };

  async function run() {
    const anchor = document.querySelector("main .actions");
    if (!anchor) return;
    let items;
    try {
      const r = await fetch("/api/source-summaries", { headers: { Accept: "application/json" } });
      if (!r.ok) return;
      items = (await r.json()).items || [];
    } catch (e) {
      return; // canlı liste alınamazsa sayfa metni olduğu gibi kalır
    }
    const near = items.map((s) => ({ s, d: dist(SEYMEN, s) })).filter((x) => x.d <= RADIUS_M).sort((a, b) => a.d - b.d);
    if (!near.length) return;
    const rows = near.map(({ s, d }) => `<p><b>${esc(s.providerId)}</b> · yaklaşık ${nf(d)} m uzaklıkta · ${esc(s.firstObserved || "?")} – ${esc(s.lastObserved || "?")} · ${nf(s.plumeCount)} bulut kaydı · ${nf(s.observationDateCount)} gözlem tarihinde ${nf(s.detectionDateCount)} tespit tarihi (sağlayıcı sayımı)<br>
      <a href="/?sources=1&amp;src=${encodeURIComponent(s.id)}">Haritada kaynak zaman çizelgesini aç</a> · <a href="/hava-ve-iklim.html?lat=${s.lat.toFixed(4)}&amp;lon=${s.lon.toFixed(4)}">Bu konumun hava ve iklimi</a></p>`).join("");
    anchor.insertAdjacentHTML("beforebegin", `<section id="kaynak-kaydi"><h2>Seymen çevresindeki kaynak kayıtları</h2>
      <p>Carbon Mapper'ın bulutları kümeleyerek oluşturduğu kaynak kayıtlarından, <a href="${UCLAM_URL}" target="_blank" rel="noopener noreferrer">UCLA STOP Methane yöntem sayfasında</a> Silivri–Seymen için verilen konuma en fazla ${nf(RADIUS_M / 1000, 1)} km uzaklıktakiler. Bu yakınlık tesis doğrulaması değildir; her bulutun kendi tahmini kendi kaydındadır.</p>${rows}</section>`);
  }
  run();
})();
