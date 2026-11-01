/* Ortak biçimlendirme ve veri alma yardımcıları (harita ve Hava ve İklim sayfaları). */
(function (global) {
  "use strict";
  const TZ = "Europe/Istanbul";
  const REQUEST_TIMEOUT_MS = 20000;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const num = (v) => typeof v === "number" && Number.isFinite(v);
  const fmtNum = (v, d = 0) => (num(v)
    ? v.toLocaleString("tr-TR", { maximumFractionDigits: d, minimumFractionDigits: d })
    : "–");
  const signed = (v, d = 1) => (num(v) ? (v > 0 ? "+" : v < 0 ? "−" : "±") + fmtNum(Math.abs(v), d) : "–");

  const parseTime = (iso) => {
    if (typeof iso !== "string" || !iso) return null;
    // Sunucu UTC'yi "Z" ile verir; ofsetsiz saat UTC kabul edilir
    const s = /([zZ]|[+-]\d{2}:\d{2})$/.test(iso) ? iso : iso + "Z";
    const t = Date.parse(s);
    return Number.isFinite(t) ? new Date(t) : null;
  };
  // Tam tarih, yıl ve saat, Türkiye saati
  const fmtLocal = (iso) => {
    const d = parseTime(iso);
    return d ? d.toLocaleString("tr-TR", { day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: TZ }) + " (TSİ)" : null;
  };
  const fmtUtc = (iso) => {
    const d = parseTime(iso);
    return d ? d.toISOString().replace("T", " ").slice(0, 16) + " UTC" : null;
  };
  const fmtShort = (iso) => {
    const d = parseTime(iso);
    return d ? d.toLocaleString("tr-TR", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: TZ }) : null;
  };
  const fmtDay = (day) => new Date(day + "T12:00:00Z").toLocaleDateString("tr-TR", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
  const fmtHour = (iso) => {
    const d = parseTime(iso);
    return d ? d.toLocaleString("tr-TR", { weekday: "short", hour: "2-digit", minute: "2-digit", timeZone: TZ }) : "";
  };
  const notProvided = "Sağlanmıyor";

  /** Zaman aşımı ve iptal destekli JSON isteği. Hata durumunda anlamlı mesajla reddeder. */
  async function getJSON(path, { signal, timeout = REQUEST_TIMEOUT_MS } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(new DOMException("timeout", "TimeoutError")), timeout);
    const onAbort = () => ctrl.abort(signal.reason);
    if (signal) {
      if (signal.aborted) ctrl.abort(signal.reason);
      else signal.addEventListener("abort", onAbort, { once: true });
    }
    try {
      const r = await fetch(path, { signal: ctrl.signal, headers: { Accept: "application/json" } });
      if (!r.ok) {
        const err = new Error(r.status === 404 ? "Kayıt bulunamadı" : `Sunucu yanıtı ${r.status}`);
        err.status = r.status;
        throw err;
      }
      return await r.json();
    } catch (e) {
      if (e.name === "AbortError" || e.name === "TimeoutError") {
        const timedOut = !signal?.aborted;
        const err = new Error(timedOut ? "İstek zaman aşımına uğradı" : "İptal edildi");
        err.aborted = !timedOut;
        throw err;
      }
      throw e;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener?.("abort", onAbort);
    }
  }

  /** Aynı paneldeki ardışık seçimlerde yalnızca son isteğin sonucu uygulanır. */
  function latestOnly() {
    let ctrl = null;
    return () => {
      ctrl?.abort();
      ctrl = new AbortController();
      const mine = ctrl;
      return { signal: mine.signal, current: () => mine === ctrl && !mine.signal.aborted };
    };
  }

  const DIRS = ["K", "KKD", "KD", "DKD", "D", "DGD", "GD", "GGD", "G", "GGB", "GB", "BGB", "B", "BKB", "KB", "KKB"];
  const compass = (deg) => (num(deg) ? DIRS[Math.round(((deg % 360) + 360) % 360 / 22.5) % 16] : "");

  /** Taşınma yönünü gösteren ok (yukarı = kuzey). Yön belirsizse ok yerine işaret. */
  function windArrow(toDeg, size = 28) {
    if (!num(toDeg)) return `<span class="calm" title="Rüzgâr çok zayıf; yön belirsiz">○</span>`;
    return `<svg class="arrow" width="${size}" height="${size}" viewBox="0 0 24 24" role="img" aria-label="Taşınma yönü ${fmtNum(toDeg)}°" style="transform:rotate(${toDeg}deg)"><path d="M12 2 L18 14 L13 12 L13 22 L11 22 L11 12 L6 14 Z" fill="currentColor"/></svg>`;
  }
  function windText(w) {
    if (!w) return "–";
    if (w.calm || !num(w.from)) return `${fmtNum(w.speed, 1)} m/s · yön belirsiz (rüzgâr çok zayıf)`;
    return `${fmtNum(w.speed, 1)} m/s · ${fmtNum(w.from)}° (${compass(w.from)}) yönünden esiyor, ${fmtNum(w.to)}° (${compass(w.to)}) yönüne taşır`;
  }

  const STATUS_TEXT = {
    loading: "Yükleniyor…", ready: "Hazır", empty: "Veri yok", queued: "Kuyrukta",
    running: "İşleniyor", unavailable: "Şu an alınamıyor", not_configured: "Erişim tanımlı değil",
    stale: "Son alınan veri gösteriliyor", not_published: "Henüz yayımlanmadı",
  };
  const ERROR_CLASS = {
    auth: "kimlik doğrulama reddedildi", terms: "veri seti koşulları hesapta kabul edilmemiş",
    quota: "kota veya istek sınırı", not_found: "kaynak bulunamadı", network: "ağ bağlantısı kurulamadı",
    server: "sağlayıcı sunucu hatası", bad_response: "beklenmeyen yanıt", not_published: "veri henüz yayımlanmadı",
    not_configured: "erişim tanımlı değil",
  };

  global.Fmt = { TZ, esc, num, fmtNum, signed, parseTime, fmtLocal, fmtUtc, fmtShort, fmtDay, fmtHour, notProvided,
    getJSON, latestOnly, compass, windArrow, windText, STATUS_TEXT, ERROR_CLASS };
})(window);
