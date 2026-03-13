# Signage Server

## Run
```bash
cp .env.example .env
# edit SHARED_SECRET in .env
pip install -r requirements.txt
alembic upgrade head
docker compose up -d --build
```

## Dokümantasyon
- [Client Dağıtım Pipeline'ı (Windows)](docs_client_deployment_pipeline.md)


## Panel Active Directory Girişi
Panel erişimi için AD tabanlı oturum açma eklendi. Aşağıdaki ortam değişkenlerini ayarlayın:

- `PANEL_SESSION_SECRET`: Flask session anahtarı
- `AD_SERVER_URI`: Örn. `ldap://dc1.company.local:389` veya `ldaps://...`
- `AD_USE_SSL`: `true/false`
- `AD_CONNECT_TIMEOUT`: LDAP bağlantı timeout değeri (saniye), varsayılan `5`

Basit LDAP bind akışı:
- `AD_DOMAIN`: Örn. `COMPANY`
- `AD_USER_DN_TEMPLATE` (opsiyonel): Örn. `CN={username},OU=Users,DC=company,DC=local`
- `AD_ALLOWED_USERS` (opsiyonel): Virgülle ayrılmış kullanıcı listesi (`ali,veli`). Tanımlanırsa sadece listedekiler panele girebilir, boş bırakılırsa AD'de doğrulanan tüm kullanıcılar girebilir.

İpucu: Kullanıcı adı sadece `ali` gibi gelirse ve `AD_DOMAIN` tanımlıysa uygulama bunu otomatik `DOMAIN\ali` formatına çevirir.

Not: Panel oturum açma ve panel API işlemleri AD oturumu ile korunur; girişte ek `X-SECRET` istenmez.

## Dış Sistem Entegrasyonu: İş Emri Başlatılmamış Uyarısı

Başka bir sistemden aşağıdaki endpoint'e istek atarak ekranlarda kalıcı uyarı açabilirsiniz:

- `POST /api/integrations/work-order-alert`
- Kimlik doğrulama: `X-Shared-Secret: <SHARED_SECRET>` veya `Authorization: Bearer <SHARED_SECRET>`

Örnek gövde (tüm cihazlar):

```json
{
  "active": true,
  "message": "İŞEMRİ BAŞLATILMAMIŞ"
}
```

Örnek gövde (tek cihaz):

```json
{
  "hostname": "BAYLAN-CLIENT-01",
  "active": true,
  "message": "İŞEMRİ BAŞLATILMAMIŞ"
}
```

Uyarıyı kaldırmak için `active` alanını `false` gönderin.


## Widget Tam Ekran Gösterim (Windows)
Widget URL oynatımında tarayıcı kiosk moduna alternatif olarak Python tabanlı bir gösterici vardır (`client/widget_viewer.py`). Gösterici artık hem `cefpython3` (tercihli) hem de `pywebview` backend'lerini destekler.

Ortam değişkenleri:
- `WIDGET_USE_PYTHON_VIEWER` (`0` varsayılan): `1/true/yes` ise URL widget'larda önce Python gösterici denenir.
- `PYTHON_WIDGET_VIEWER_ENABLED` (`1` varsayılan): Python widget göstericiyi global olarak aç/kapatır.
- `WIDGET_VIEWER_BACKEND` (`auto` varsayılan): `auto`, `cef`, `pywebview`.
- `WIDGET_SINGLE_ENGINE` (`0` varsayılan): `1/true/yes` ise URL widget doğrudan açılmak yerine `client/widget_engine.html` içine tek Chromium instance mantığıyla `iframe` olarak yüklenir.
- `CEF_EXTRA_SWITCHES`: Virgülle ayrılmış ek CEF switch listesi (`switch` veya `switch=value`).

Notlar:
- CEF backend, kiosk için Chrome uyumlu switch'lerle (`--kiosk`, `--disable-translate`, `--disable-infobars`, `--disable-session-crashed-bubble`, `--disable-features=TranslateUI`) başlatılır.
- Backend başlatılamazsa diğer backend denenir; Python gösterici tamamen kullanılamazsa mevcut tarayıcı kiosk akışına geri dönülür.

### Frozen build'de image/widget viewer uyarılarını aktif etme
`client/player.py` içindeki şu uyarılar, uygulama frozen (`agent.exe`) modda çalışırken aynı klasörde `image_viewer(.exe)` ve `widget_viewer(.exe)` bulunamadığında görünür.

Aktivasyon için:
- `client/build_agent.ps1` script'ini **varsayılan ayarlarla** çalıştırın (yeni davranışla sidecar viewer exe'leri de üretir).
- `dist` klasöründen en az şu üç dosyayı birlikte dağıtın: `BaylanSignageAgent.exe`, `image_viewer.exe`, `widget_viewer.exe`.
- Widget viewer için ayrıca `cefpython3` veya `pywebview` bağımlılıklarından en az biri kurulu olmalıdır.

Viewer build'lerini kapatmak isterseniz `-SkipViewerBuild` parametresini geçebilirsiniz; bu durumda frozen modda viewer özellikleri bilerek pasif kalır.
