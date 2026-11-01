/* Küçük, bağımlılıksız SVG grafikleri. Eksik değerler çizgide boşluk bırakır, sıfır sayılmaz. */
(function (global) {
  "use strict";
  const { esc, fmtNum, num } = global.Fmt;

  function niceRange(values, pad = 0.08, zero = false) {
    const v = values.filter(num);
    if (!v.length) return [0, 1];
    let lo = Math.min(...v), hi = Math.max(...v);
    if (zero) lo = Math.min(0, lo);
    if (hi - lo < 1e-9) { hi += 1; lo -= zero ? 0 : 1; }
    const span = hi - lo;
    return [zero && lo >= 0 ? 0 : lo - span * pad, hi + span * pad];
  }

  /**
   * Zaman serisi çizgisi. opts: {times, series:[{values, label, cls}], unit, marker (ISO), markerLabel, height, bars}
   * bars: true ise ilk seri çubuk olarak çizilir (ör. yağış).
   */
  function timeSeries(opts) {
    const W = 640, H = opts.height || 180, L = 44, R = 12, T = 12, B = 28;
    const times = opts.times.map((t) => Date.parse(/Z$/.test(t) ? t : t + "Z"));
    const all = opts.series.flatMap((s) => s.values);
    let [lo, hi] = niceRange(all, 0.08, !!opts.bars || opts.zero);
    if (opts.max != null) hi = Math.min(hi, opts.max);
    const t0 = times[0], t1 = times[times.length - 1] || t0 + 1;
    const X = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
    const Y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    let g = "";
    for (let k = 0; k <= 4; k++) {
      const v = lo + ((hi - lo) * k) / 4;
      g += `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/><text class="tick" x="${L - 6}" y="${Y(v) + 4}" text-anchor="end">${fmtNum(v, Math.abs(hi - lo) < 10 ? 1 : 0)}</text>`;
    }
    const step = Math.max(1, Math.round(times.length / 6));
    times.forEach((t, k) => {
      if (k % step) return;
      const d = new Date(t);
      const lab = d.toLocaleString("tr-TR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Istanbul" });
      g += `<text class="tick" x="${X(t)}" y="${H - 8}" text-anchor="middle">${esc(lab)}</text>`;
    });
    opts.series.forEach((s, si) => {
      if (opts.bars && si === 0) {
        const bw = Math.max(1, (W - L - R) / times.length - 1);
        s.values.forEach((v, k) => {
          if (!num(v)) return;
          g += `<rect class="bar ${s.cls || ""}" x="${X(times[k]) - bw / 2}" y="${Y(v)}" width="${bw}" height="${Math.max(0, Y(lo) - Y(v))}"/>`;
        });
        return;
      }
      let d = "", pen = false;
      s.values.forEach((v, k) => {
        if (!num(v)) { pen = false; return; }
        d += `${pen ? "L" : "M"}${X(times[k]).toFixed(1)},${Y(v).toFixed(1)}`;
        pen = true;
      });
      g += `<path class="line ${s.cls || ""}" d="${d}"/>`;
    });
    if (opts.marker) {
      const m = Date.parse(opts.marker);
      if (m >= t0 && m <= t1) {
        g += `<line class="marker" x1="${X(m)}" x2="${X(m)}" y1="${T}" y2="${H - B}"/><text class="marker-label" x="${X(m) + 4}" y="${T + 10}">${esc(opts.markerLabel || "")}</text>`;
      }
    }
    const legend = opts.series.map((s) => `<span class="lg ${s.cls || ""}"><i></i>${esc(s.label)}</span>`).join("");
    return `<figure class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.title || "")}">${g}</svg>
      <figcaption>${legend}${opts.unit ? `<span class="unit">Birim: ${esc(opts.unit)}</span>` : ""}</figcaption></figure>`;
  }

  /** 12 aylık referans (çubuk) ve karşılaştırılan ay (işaret). */
  function monthly(opts) {
    const W = 640, H = opts.height || 190, L = 44, R = 12, T = 12, B = 28;
    const months = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"];
    const vals = opts.values;
    const extra = opts.compare && num(opts.compare.value) ? [opts.compare.value] : [];
    const [lo, hi] = niceRange(vals.concat(extra), 0.1, opts.zero);
    const bw = (W - L - R) / 12;
    const Y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    let g = "";
    for (let k = 0; k <= 4; k++) {
      const v = lo + ((hi - lo) * k) / 4;
      g += `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/><text class="tick" x="${L - 6}" y="${Y(v) + 4}" text-anchor="end">${fmtNum(v, Math.abs(hi - lo) < 10 ? 1 : 0)}</text>`;
    }
    const base = Y(Math.max(lo, 0));
    vals.forEach((v, k) => {
      const x = L + k * bw;
      g += `<text class="tick" x="${x + bw / 2}" y="${H - 8}" text-anchor="middle">${months[k]}</text>`;
      if (!num(v)) return;
      const y = Y(v);
      g += `<rect class="bar ref" x="${x + bw * 0.18}" y="${Math.min(y, base)}" width="${bw * 0.64}" height="${Math.abs(base - y)}"><title>${months[k]}: ${fmtNum(v, 1)} ${esc(opts.unit)}</title></rect>`;
    });
    if (extra.length) {
      const k = opts.compare.month - 1;
      const x = L + k * bw + bw / 2, y = Y(opts.compare.value);
      g += `<line class="cmp" x1="${x - bw * 0.42}" x2="${x + bw * 0.42}" y1="${y}" y2="${y}"/><circle class="cmp" cx="${x}" cy="${y}" r="4"/>`;
    }
    const legend = `<span class="lg ref"><i></i>${esc(opts.refLabel)}</span>` + (extra.length ? `<span class="lg cmp"><i></i>${esc(opts.compare.label)}</span>` : "");
    return `<figure class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.title || "")}">${g}</svg>
      <figcaption>${legend}<span class="unit">Birim: ${esc(opts.unit)}</span></figcaption></figure>`;
  }

  global.Charts = { timeSeries, monthly };
})(window);
