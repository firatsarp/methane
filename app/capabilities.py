"""Kaynak yetenek envanteri ve yöntem kaydı. Sır içermez; yalnızca tanımlı olup olmadıkları raporlanır.

Durumlar:
- verified_live: uygulamada gerçek ürün alımı başarıyla gerçekleşti (last_product_at var).
- configured_unverified: erişim tanımlı, gerçek ürün alımı henüz yok (token almak ürün alımı değildir).
- credential_error: tanımlı erişim sağlayıcı tarafından reddedildi.
- awaiting_access: üyelik/lisans/kimlik bekleniyor; uygulamada otomatik adaptör yok, yetkili dosya içe aktarma var.
- context_only: farklı dikey/uzamsal duyarlılık; tesis bulutuyla eşitlenmez.
- discovery_only: erişim/lisans/Türkiye kapsamı doğrulanmadı; işlevsel adaptör listesinde değil.
"""
import os

from . import alerts, policy, sources

# id -> (sağlayıcı durum anahtarları, gerekli ortam değişkenleri, kimlik türü, resmî kayıt adresi, adaptör, not)
REGISTRY = {
    "carbon_mapper": {"status_keys": ["carbonmapper", "carbonmapper_sources"], "env": ["CARBON_MAPPER_API_TOKEN"],
                      "credentialType": "API token (isteğe bağlı; katalog tokensız da yanıt veriyor)",
                      "registration": "https://data.carbonmapper.org/", "adapter": "app/ingest.py",
                      "tokenOptional": True},
    "nasa_emit": {"status_keys": ["emit"], "env": ["EARTHDATA_TOKEN|EARTHDATA_USERNAME+EARTHDATA_PASSWORD"],
                  "credentialType": "NASA Earthdata kullanıcı tokenı veya kullanıcı adı/parola",
                  "registration": "https://urs.earthdata.nasa.gov/", "adapter": "app/emit.py"},
    "tropomi": {"status_keys": ["tropomi"], "env": ["EARTHDATA_TOKEN|EARTHDATA_USERNAME+EARTHDATA_PASSWORD"],
                "credentialType": "NASA Earthdata (GES DISC uygulama onayı)",
                "registration": "https://urs.earthdata.nasa.gov/", "adapter": "app/ingest.py"},
    "sentinel2": {"status_keys": ["cdse"], "env": ["CDSE_CLIENT_ID+CDSE_CLIENT_SECRET"],
                  "credentialType": "CDSE Sentinel Hub OAuth istemci kimliği + gizli anahtar (aynı istemci)",
                  "registration": "https://shapps.dataspace.copernicus.eu/dashboard/", "adapter": "app/imagery.py"},
    "unep_mars": {"status_keys": ["mars"], "env": [], "credentialType": "Gerekmez (CC BY-NC-SA 4.0)",
                  "registration": "https://methanedata.unep.org/download-dataset", "adapter": "app/mars.py"},
    "cams_hotspots": {"status_keys": ["cams_hotspots"], "env": [], "credentialType": "Gerekmez",
                      "registration": "https://atmosphere.copernicus.eu/ghg-services/cams-methane-hotspot-explorer",
                      "adapter": "app/cams.py"},
    "enmap": {"env": ["ENMAP_IMPORT_DIR"], "credentialType": "EnMAP IPS kaydı + EOWEB alt hesabı (kullanıcı/parola); "
              "otomatik indirme belgesi doğrulanana kadar yetkili dosya içe aktarma",
              "registration": "https://www.enmap.org/data_access/", "localImport": "enmap"},
    "prisma": {"env": ["PRISMA_IMPORT_DIR"], "credentialType": "ASI PRISMA dijital lisans + hesap onayı; self-servis "
               "API anahtarı varsayılmaz", "registration": "https://prismauserregistration.asi.it/",
               "localImport": "prisma"},
    "landsat": {"env": ["USGS_M2M_USERNAME+USGS_M2M_APP_TOKEN"], "credentialType": "USGS ERS hesabı + M2M Application "
                "Token (login-token akışı)", "registration": "https://ers.cr.usgs.gov/register",
                "localImport": "landsat"},
    "gosat_family": {"credentialType": "NIES portal kullanıcı kaydı (her arşiv ayrı)", "contextOnly": True,
                     "registration": "https://data2.gosat.nies.go.jp/index_en.html"},
    "ghgsat": {"credentialType": "Ticari anlaşma veya onaylı ESA TPM erişimi", "discoveryOnly": True,
               "registration": "https://www.ghgsat.com/partners/"},
    "methanesat_archive": {"credentialType": "Yayımlanmış arşiv; Türkiye sahnesi doğrulanmadı", "discoveryOnly": True,
                           "registration": "https://www.methanesat.org/"},
    "regional_profiles_archive": {"credentialType": "CDS veya NASA hesabı; ürün bazında kullanım koşulu",
                                  "contextOnly": True,
                                  "registration": "https://cds.climate.copernicus.eu/datasets/satellite-methane"},
}

# Yöntem kaydı: hangi hesap şimdi çalışıyor, hangisi hangi ön koşulu bekliyor (makale = yöntem kaynağı, garanti değil)
METHODS = [
    {"id": "provider_products", "name": "Sağlayıcı bulut/salım ürünlerinin kanonik okunması ve soy ağacı",
     "status": "operational", "refs": ["G21", "TH17"], "prerequisites": []},
    {"id": "numeric_raster_morphology", "name": "Sayısal artış GeoTIFF okuyucu, maske, piksel alanı, eksen/merkez",
     "status": "operational", "refs": ["P23", "B21"],
     "prerequisites": ["Carbon Mapper L3A IME CMF GeoTIFF (birim dosyada yok: toplamlar dosya biriminde)"]},
    {"id": "wind_interpretation", "name": "Gözlem anı ERA5 rüzgâr/PBL/yağış kural motoru", "status": "operational",
     "refs": ["B21", "PR26"], "prerequisites": []},
    {"id": "robust_peak_screen", "name": "Seri içi robust medyan/MAD aday taraması ve seyrek örneklem top-N",
     "status": "operational", "refs": ["SC23"], "prerequisites": ["Seri başına ≥10 önceki gözlem ve ≥5 gün (z için)"]},
    {"id": "own_ime_q", "name": "Kendi IME ve Q = IME·Ueff/L tahmini", "status": "requires_validation",
     "refs": ["B21", "P23", "S23"],
     "prerequisites": ["Birimi doğrulanmış jeoreferanslı artış (ppm·m veya kg/m²)", "Arka plan ve bulut maskesi",
                       "Yönteme göre kalibre edilmiş Ueff(U10) ve L", "Kontrollü salım/enjeksiyon doğrulaması"]},
    {"id": "matched_filter", "name": "Matched filter / mag1c / sahneye özgü hedef spektrumu", "status": "not_implemented",
     "refs": ["F20", "F21", "TH15"],
     "prerequisites": ["L1B radyans (L2A yansıma radyans yerine kullanılmaz)", "SRF ve kalibrasyon",
                       "Sahne geometrisi, su buharı, yüzey yüksekliği", "Sentetik enjeksiyon ve negatif kontroller"]},
    {"id": "ilmf", "name": "ILMF ve MF karşılaştırması", "status": "not_implemented", "refs": ["P23"],
     "prerequisites": ["Sayısal radyans küpü", "Bağımsız değerlendirme sahneleri"]},
    {"id": "imap_wfm_doas", "name": "IMAP-DOAS / WFM-DOAS", "status": "not_implemented", "refs": ["FR05", "B21", "TH17"],
     "prerequisites": ["Işınım aktarım modeli ve öncüller", "Spektral kalibrasyon", "Sensör SRF"]},
    {"id": "wide_spectral", "name": "Geniş NIR/SWIR aralığıyla çıkarım", "status": "not_implemented", "refs": ["R24"],
     "prerequisites": ["İlgili kanallar, SRF ve kalibrasyon"]},
    {"id": "prismethanet", "name": "PRISMethaNet benzeri segmentasyon", "status": "not_implemented", "refs": ["M24"],
     "prerequisites": ["Gerçek model ağırlıkları, eğitim kapsamı ve lisansı", "Bağımsız test kümesi"]},
    {"id": "sciatran_rt", "name": "SCIATRAN ışınım aktarımı", "status": "not_implemented", "refs": ["ME23"],
     "prerequisites": ["Kurulum, lisans ve girdi profilleri"]},
    {"id": "hapi_spectroscopy", "name": "HAPI/HITRAN spektroskopisi", "status": "not_implemented", "refs": ["H19"],
     "prerequisites": ["Tek başına ışınım aktarımı veya salım hesabı değildir; ileri model ve SRF gerekir"]},
    {"id": "fod_landfill", "name": "Depolama sahası FOD senaryosu", "status": "not_implemented", "refs": ["PR26"],
     "prerequisites": ["Atık miktarı/bileşimi, nem, örtü, gaz toplama ve saha girdileri"]},
]


def _has(spec):
    return any(all(os.environ.get(k, "").strip() for k in alt.split("+")) for alt in spec.split("|"))


def sources_inventory():
    status_by_id = {s["id"]: s for s in alerts.source_status()}
    out = []
    for p in policy.data_sources()["providers"]:
        reg = REGISTRY.get(p["id"], {})
        env = reg.get("env", [])
        configured = all(_has(e) for e in env) if env else True
        sts = [status_by_id.get(k) or sources.get(k) for k in reg.get("status_keys", [])]
        last_product = max((s.get("last_product_at") for s in sts if s and s.get("last_product_at")), default=None)
        errors = [s.get("error_class") for s in sts if s and s.get("state") == "error"]
        if reg.get("discoveryOnly"):
            state = "discovery_only"
        elif reg.get("contextOnly"):
            state = "context_only"
        elif not reg.get("adapter"):
            state = "awaiting_access"
        elif errors and any(e in ("auth", "terms") for e in errors):
            state = "credential_error"
        elif last_product:
            state = "verified_live"
        elif configured or reg.get("tokenOptional"):
            state = "configured_unverified"
        else:
            state = "awaiting_access"
        out.append({"id": p["id"], "priority": p.get("priority"), "products": p.get("products"), "role": p.get("role"),
                    "access": p.get("access"), "automation": p.get("automation"), "state": state,
                    "credentialType": reg.get("credentialType"), "registration": reg.get("registration"),
                    "credentialsConfigured": configured if env else None, "adapter": reg.get("adapter"),
                    "localImport": reg.get("localImport"), "lastProductAt": last_product,
                    "errorClasses": errors, "references": p.get("citations")})
    return out


def methods():
    return [{**m, "references": [policy.reference(r) for r in m["refs"] if policy.reference(r)]} for m in METHODS]


def setup_report():
    """Kurulum çıktısı: dört hazır hesap ailesi (değer basmadan), ortam ayrımı."""
    families = {
        "carbon_mapper": ["CARBON_MAPPER_API_TOKEN"],
        "nasa_earthdata": ["EARTHDATA_TOKEN|EARTHDATA_USERNAME+EARTHDATA_PASSWORD"],
        "cdse": ["CDSE_CLIENT_ID+CDSE_CLIENT_SECRET"],
        "cds": ["CDSAPI_KEY"],
    }
    return {"environment": os.environ.get("APP_ENV", "production" if os.environ.get("COOLIFY_FQDN") else "unknown"),
            "environmentNote": "Değerler yalnızca bu çalışan sürecin ortamından okunur; Coolify Preview ve Production "
                               "ortam değişkenleri ayrıdır. Gizli değerler hiçbir yanıtta yer almaz.",
            "accountFamilies": {k: all(_has(e) for e in v) for k, v in families.items()},
            "sources": sources_inventory()}
