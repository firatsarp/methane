/* Ortak üst gezinme için isteğe bağlı iyileştirmeler. Menü JavaScript olmadan da çalışır (details/summary);
   bu betik dışarı tıklayınca/Escape ile kapatır ve haritaya dönüşte son harita seçimini geri yükler. */
(function () {
  "use strict";
  const MAP_STATE_KEY = "metan.mapState";
  const path = location.pathname.replace(/\/index\.html$/, "/");

  // Haritadaki filtre/seçim/görünüm oturumda saklanır; Harita bağlantıları ve "Haritaya dön" bunu geri yükler
  let saved = null;
  try { saved = sessionStorage.getItem(MAP_STATE_KEY); } catch (e) { saved = null; }
  if (saved && path !== "/") {
    document.querySelectorAll("a[data-map-link]").forEach((a) => { a.href = "/" + saved; });
  }

  const menu = document.querySelector(".mobile-menu");
  if (!menu) return;
  const close = () => { menu.open = false; };
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && menu.open) { close(); menu.querySelector("summary").focus(); }
  });
  document.addEventListener("click", (e) => { if (menu.open && !menu.contains(e.target)) close(); });
  menu.addEventListener("click", (e) => { if (e.target.closest("a")) close(); });
})();
