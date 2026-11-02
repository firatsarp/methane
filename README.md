# Türkiye Metan İzleyici

Türkiye üzerindeki atmosferik metanı (CH₄) uydu verisinden haritalayan web uygulaması.

| Katman | Kaynak | Güncellik |
|---|---|---|
| Günlük metan haritası (0.1° grid, ppb) | Sentinel-5P TROPOMI CH₄ L2, NASA GES DISC üzerinden | Son 5 gün NRT (geçişten ~1 saat sonra), daha eskisi kesin (OFFL) ürün |
| Metan bulutları (plume, nokta kaynak) | [Carbon Mapper](https://data.carbonmapper.org/) kataloğu (Planet Tanager + NASA EMIT): görüntü, emisyon (kg/sa), sektör | Günde bir kez tamamen yenilenir |
| Sonraki geçişler, anlık konum | CelesTrak TLE + Skyfield | 12 saatte bir yenilenir |

Uygulama 90 dakikada bir NASA'ya yeni veri olup olmadığını sorar, yalnızca Türkiye kesitini işler ve
indirilen dosyayı siler. Diskte sadece küçük bir SQLite veritabanı kalır.

## 1. NASA Earthdata hazırlığı (bir kez)

1. https://urs.earthdata.nasa.gov adresinde oturum açın.
2. **Applications → Authorized Apps → Approve More Applications** bölümünde
   **"NASA GESDISC DATA ARCHIVE"** uygulamasını onaylayın. Bu adım yapılmazsa TROPOMI indirmeleri 401 hatası verir.

## 2. DNS (Hostinger paneli)

`sarpify.com` → DNS / Nameservers → yeni **A kaydı**:

| Tür | Ad | Değer |
|---|---|---|
| A | `metan` | VPS'in IP adresi |

Uygulama `https://metan.sarpify.com` adresinde açılacak.

## 3. Kodu GitHub'a yükleyin

Coolify kodu bir Git deposundan çeker. Bu klasörü bir GitHub deposuna (özel olabilir) yükleyin.

## 4. Coolify'da kurulum

1. **Projects → (proje) → + New → Private Repository (GitHub App)** veya herkese açık depo ise **Public Repository**.
2. Depoyu ve `main` dalını seçin, **Build Pack: Docker Compose**, compose dosyası: `/docker-compose.yml`.
3. `metan` servisinin **Domains** alanına şunu yazın:
   ```
   https://metan.sarpify.com:8000
   ```
   (`:8000` konteynerin iç portudur, Coolify proxy'si 443'ten buraya yönlendirir. SSL sertifikası otomatik alınır.)
4. **Environment Variables**:
   | Değişken | Değer |
   |---|---|
   | `EARTHDATA_USERNAME` | Earthdata kullanıcı adınız |
   | `EARTHDATA_PASSWORD` | Earthdata şifreniz (Coolify'da gizli olarak işaretleyin) |
   | `BACKFILL_DAYS` | `90` (isteğe bağlı; geriye kaç gün veri çekilip saklansın, daha eskisi silinir) |
5. **Deploy**.

## 5. Kontrol

- `https://metan.sarpify.com/api/status` → `"auth": "ok"` görmelisiniz.
- Coolify **Logs** ekranında `S5P_L2__CH4____HiR_NRT: N granül bulundu` satırları görünür.
  90 günlük geçmiş (~195 dosya, toplam ~13 GB indirme; her dosya işlendikten sonra silinir) ilk açılışta
  yaklaşık bir saatte iner.
- Bulutlu günlerde Türkiye'nin büyük kısmı boş görünür. TROPOMI bulut altını ölçemez; bu normaldir.
  7, 30 veya 90 günlük ortalama daha dolu bir harita verir.

## Yerel çalıştırma

```bash
EARTHDATA_USERNAME=... EARTHDATA_PASSWORD=... docker compose up --build
```

Ardından http://localhost:8000 (compose dosyasında yerelde `ports: ["8000:8000"]` ekleyin).

## API

| Uç nokta | Açıklama |
|---|---|
| `GET /api/days` | İşlenmiş günler, Türkiye kapsama oranı ve ortalaması |
| `GET /api/tropomi?start=YYYY-MM-DD&end=YYYY-MM-DD` | Grid hücreleri `[j, i, ppb, piksel]` ve istatistikler |
| `GET /api/plumes` | Carbon Mapper metan bulutları (GeoJSON; emisyon, sektör, görüntü bağlantısı ve sınırları) |
| `GET /api/passes` | Önümüzdeki 72 saatteki Türkiye geçişleri |
| `GET /api/positions` | Uyduların anlık konumu ve sonraki 100 dk'lık izi |
| `GET /api/status` | Giriş durumu, son güncelleme, son hata |
| `GET /api/sources` | Kaynak başına son kontrol, son başarılı alma, son gözlem/yayımlanma zamanı, token durumu |
| `GET /api/alerts` | Eski sözleşme: yeni bulut kaydı ve kaynak uyarıları (`/api/monitoring-events` alt kümesi) |
| `GET /api/observations` | Kanonik gözlemler (Carbon Mapper + NASA EMIT, aynı EMIT gözlemi tek kayıt): `sector`, `sensor`, `days`, `since`, `until`, `emission_only`, `sort`, `limit`, `offset`; toplam, filtrelenmiş sayı ve filtre seçenekleri |
| `GET /api/observations/{id}` | Normalize gözlem, erişim yolları, sağlayıcı kaynak özeti ve aynı kaynağın diğer kayıtları |
| `GET /api/source-summaries[/{id}]` | Carbon Mapper kaynak özetleri (tek bulutlardan ayrı) |
| `GET /api/weather/observation/{id}` | Gözlemin kendi koordinatı ve saatine ait ERA5 (Open-Meteo yolu; CDS tanımlıysa CDS işi) |
| `GET /api/weather/forecast?lat=&lon=` | Open-Meteo güncel durum + 72 saat tahmin (1 saat önbellek) |
| `GET /api/climate?lat=&lon=&period=1991-2020[&compare=YYYY-MM]` | ERA5 1991–2020 aylık referans ve tamamlanmış ay karşılaştırması; ilk istekte arka plan işi kuyruğa alınır |
| `GET /api/imagery/{id}` | Sentinel-2 L2A yüzey görüntüsü durumu / PNG (CDSE tanımlıysa) |
| `GET /api/data-status` | Sır içermeyen kaynak durumları ve iş kuyruğu sayıları |
| `GET /api/monitoring-events` | Açıklanabilir izleme olayları: yeni gözlem, geçmiş gözlem yeni yayımlandı, tekrarlanan tespit, kayıt güncellendi, TROPOMI bölgesel artış adayı (kural gösterilir), veri güncellenemedi |
| `GET /api/observations/{id}/analysis` | Sürümlü kural motoru yorumu (gözlenen sonuç → geçmiş → diğer sensörler → meteoroloji → belirlenemeyenler → sonraki gözlem) |
| `GET /api/observations/{id}/comparisons` | Karşılaştırma adayları ve her biri için sunucu kararı (havuz: kalıcı arşiv + son 90 gün) |
| `GET /api/comparisons?a=&b=` | İki gözlem: `near_time_independent` / `historical_change` / `context_only`, gerekçe ve reddedilen sayısal karşılaştırma kodu |
| `GET /api/peaks[?state=]` | Kalıcı yüksek olay arşivi (seçim gerekçesi, dosya arşiv durumu, revizyon sayısı) |
| `GET /api/archive/assets/{sha256}` | Arşivlenmiş dosya (sha256 ile doğrulanmış) |
| `GET /api/sources/{id}/history` | Kaynak ilişkisine bağlı gözlem serileri (sensör/yöntem sürümüne göre ayrı) |
| `GET /api/retention` | 90 gün penceresi, arşiv sayıları, son günlük iş hattı raporları |
| `GET /api/capabilities`, `GET /api/source-setup` | Kaynak yetenek envanteri, yöntem kaydı (ön koşullarıyla), hesap aileleri hazır mı (değer basılmaz) |
| `POST /api/admin/peaks/{id}/review`, `POST /api/admin/pipeline/run`, `GET /api/admin/imports` | `X-Admin-Token` başlığı ile; `ADMIN_TOKEN` tanımsızsa kapalı |

## Günlük iş hattı, 90 gün saklama ve kalıcı arşiv

Her gün 03:30 TSİ (ve açılışta bir kez): Carbon Mapper, NASA EMIT, UNEP MARS ve CAMS alınır → aynı çekim tekilleştirilir
(`app/acquisition.py`) → seri içi pik sınıflandırması (`app/peaks.py`) → yüksek olaylar için gözlem anı ERA5, sayısal raster
morfolojisi ve yorum → kalıcı paket (`app/archive.py`) → 90 gün temizliği (`app/retention.py`). Kurallar ve eşikler
`app/policy/scientific-rules.json` dosyasında sürümlüdür; kaynak envanteri `app/policy/data-sources.json`, kaynakça
`app/policy/references.json`. Normal gözlemler gözlem zamanına göre 90 gün tutulur; kalıcı arşivdeki olaylar ve dosyaları silinmez.
Yetkili/lisanslı EnMAP, PRISMA, Landsat dosyaları `DATA_DIR/import/<kaynak>/` altına konursa kaydedilir ve seviyesi denetlenir;
doğrulanmış sensöre özgü tarif olmadan metan çıkarımı yapılmaz.

## Sayfalar ve içerik

Her sayfada aynı üst menü bulunur (telefonda JavaScript gerektirmeyen Menü): `/` (harita),
`/yuksek-degerleri-yorumlamak.html`, `/veriyi-anlamak.html`, `/uydular-nasil-olcer.html`,
`/hava-ve-iklim.html` (gerçek veriyle meteoroloji ve iklim; açıklama metni veri sekmelerinin altında), `/kaynaklar.html`,
`/yuksek-olay-arsivi.html` (kalıcı arşiv) ve `/karsilastirma.html` (iki gözlem, her görünümde tek bulut).
Eski `/seymen.html` adresi 308 ile `/yuksek-degerleri-yorumlamak.html` adresine yönlenir.

Metinlerin tek kaynağı `static/content/metan-icerik.2026-11-01-general-3.json` dosyasıdır (şema 3.0.0). Okuma sayfaları bu
JSON'dan hazırlanmış statik HTML'dir; JavaScript kapalıyken de okunur, kaynaklar sayfa sonunda kapalı "Kaynaklar" bölümündedir.
`tests/content.test.js` sayfalardaki her paragrafın JSON'daki metinle aynı olduğunu denetler; yeni sürümde JSON ve sayfalar
birlikte güncellenmelidir. Harita detayındaki kavram açıklamaları aynı JSON'un `glossary` alanından okunur.
HTML/JS/CSS/JSON yanıtları `Cache-Control: no-cache` ile her açılışta yeniden doğrulanır.

Testler: `python -m unittest discover -s tests` ve `node --test tests/content.test.js tests/geo.test.js tests/plume-layer.test.js`.

## Erişimler (yalnızca sunucu ortam değişkenleri)

| Değişken | Kullanım |
|---|---|
| `EARTHDATA_TOKEN` veya `EARTHDATA_USERNAME` + `EARTHDATA_PASSWORD` | TROPOMI ve NASA EMIT (LP DAAC). Token boşsa kullanıcı adı/parola kullanılır |
| `CARBON_MAPPER_API_TOKEN` | İsteğe bağlı; reddedilirse herkese açık katalogla devam edilir |
| `CDSAPI_URL`, `CDSAPI_KEY` | Copernicus CDS ERA5 iş kuyruğu (gözlem anı). Hesapta "ERA5 hourly data on single levels" lisansı kabul edilmiş olmalı; edilmemişse iş `terms` tanısıyla durur |
| `CDSE_CLIENT_ID`, `CDSE_CLIENT_SECRET` | Copernicus Data Space Sentinel-2 L2A yüzey görüntüsü (Catalog/Process v1). Kimlik 6 saatte bir sınanır; geçerliyse yalnızca kimlik hatasıyla engellenmiş görüntü işleri yeniden kuyruğa alınır. Kaynak, gerçek sahne alınana kadar "sağlıklı" sayılmaz |
| `ADMIN_TOKEN` | İsteğe bağlı; yönetici uçlarını açar |
| `PEAK_ABSOLUTE_KGH` | İsteğe bağlı mutlak kg/sa aday eşiği; varsayılan yok |

Open-Meteo anahtarsız, ticari olmayan kullanım koşullarıyla kullanılır; atıf veri kartlarında gösterilir.
Tanımlı bir erişim yalnızca "hazır" sayılır; gerçek bir istek başarıyla sonuçlanınca `/api/data-status` içinde `healthy: true` olur.

## Notlar

- Değerler `methane_mixing_ratio_bias_corrected` değişkeninden, `qa_value > 0.5` filtresiyle alınır.
- "Anomali" görünümü her hücrenin o anki Türkiye medyanından farkını gösterir. Yüksek rakımlı bölgelerde
  (Doğu Anadolu) mutlak değerler sistematik olarak farklı olabilir; tekil sızıntı tespiti için bulut (plume) katmanı daha uygundur.
- Bulut verisi Carbon Mapper'ın [kullanım koşullarına](https://carbonmapper.org/terms) tabidir: yalnızca ticari olmayan
  kullanım ve kaynak gösterimi zorunlu. Görüntü bağlantıları imzalı ve süreli olduğu için katalog günde bir yenilenir.
  Haritada yalnızca seçilen gözlemin görüntüsü tarayıcıya yüklenir. Kalıcı arşive giren olayların sayısal GeoTIFF ve PNG
  dosyaları bu koşullar çerçevesinde sunucuda sha256 ile saklanır.
- UNEP MARS verisi CC BY-NC-SA 4.0 (UNEP IMEO atfı) ile kullanılır.
- Sentinel-5P bir ESA/Copernicus uydusudur; veriyi NASA GES DISC arşivinden alıyoruz.
