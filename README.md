# Türkiye Metan İzleyici

Türkiye üzerindeki atmosferik metanı (CH₄) uydu verisinden haritalayan web uygulaması.

| Katman | Kaynak | Güncellik |
|---|---|---|
| Günlük metan haritası (0.1° grid, ppb) | Sentinel-5P TROPOMI CH₄ L2, NASA GES DISC üzerinden | Son 5 gün NRT (geçişten ~1 saat sonra), daha eskisi kesin (OFFL) ürün |
| Metan bulutları (nokta kaynak) | NASA EMIT L2B CH4PLM v002 | Arşiv (katalog Eylül 2025'te bitiyor) |
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
   | `BACKFILL_DAYS` | `14` (isteğe bağlı; geriye kaç gün veri çekilsin) |
5. **Deploy**.

## 5. Kontrol

- `https://metan.sarpify.com/api/status` → `"auth": "ok"` görmelisiniz.
- Coolify **Logs** ekranında `S5P_L2__CH4____HiR_NRT: N granül bulundu` satırları görünür.
  İlk 14 günlük veri yaklaşık 10–20 dakikada iner.
- Bulutlu günlerde Türkiye'nin büyük kısmı boş görünür. TROPOMI bulut altını ölçemez; bu normaldir.
  7 veya 30 günlük ortalama daha dolu bir harita verir.

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
| `GET /api/plumes` | EMIT metan bulutları (GeoJSON) |
| `GET /api/passes` | Önümüzdeki 72 saatteki Türkiye geçişleri |
| `GET /api/positions` | Uyduların anlık konumu ve sonraki 100 dk'lık izi |
| `GET /api/status` | Giriş durumu, son güncelleme, son hata |

## Notlar

- Değerler `methane_mixing_ratio_bias_corrected` değişkeninden, `qa_value > 0.5` filtresiyle alınır.
- "Anomali" görünümü her hücrenin o anki Türkiye medyanından farkını gösterir. Yüksek rakımlı bölgelerde
  (Doğu Anadolu) mutlak değerler sistematik olarak farklı olabilir; tekil sızıntı tespiti için EMIT katmanı daha uygundur.
- Sentinel-5P bir ESA/Copernicus uydusudur; veriyi NASA GES DISC arşivinden alıyoruz.
