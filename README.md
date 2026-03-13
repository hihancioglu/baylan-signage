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


## Client State Transition Log Varsayılanı

`client/client.py` içinde `STATE_LOG_PATH` verilmezse varsayılan yol:

- Windows: `C:\ProgramData\BaylanSignage\state_transitions.jsonl`
- Diğer ortamlar: runtime tabanında `state_transitions.jsonl`

İsterseniz `STATE_LOG_PATH` ile özel bir dosya yolu verebilirsiniz.

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
- `WIDGET_USE_PYTHON_VIEWER` (`1` varsayılan): `0/false/no` verilmezse URL widget'larda Python gösterici tercih edilir.
- `PYTHON_WIDGET_VIEWER_ENABLED` (`1` varsayılan): Python widget göstericiyi global olarak aç/kapatır.
- `WIDGET_VIEWER_BACKEND` (`auto` varsayılan): `auto`, `cef`, `pywebview`.
- `WIDGET_SINGLE_ENGINE` (`0` varsayılan): `1/true/yes` ise URL widget doğrudan açılmak yerine `client/widget_engine.html` içine tek Chromium instance mantığıyla `iframe` olarak yüklenir.
- `CEF_EXTRA_SWITCHES`: Virgülle ayrılmış ek CEF switch listesi (`switch` veya `switch=value`).

Notlar:
- CEF backend, kiosk için Chrome uyumlu switch'lerle (`--kiosk`, `--disable-translate`, `--disable-infobars`, `--disable-session-crashed-bubble`, `--disable-features=TranslateUI`) başlatılır.
- Backend başlatılamazsa diğer backend denenir; Python gösterici tamamen kullanılamazsa mevcut tarayıcı kiosk akışına geri dönülür.

### Frozen build'de widget viewer'ı aktif etme
`client/player.py` içinde frozen (`agent.exe`) modda Python widget gösterici için aynı klasörde `widget_viewer(.exe)` aranır.

Aktivasyon için:
- `client/build_agent.ps1` script'ini **varsayılan ayarlarla** çalıştırın (widget viewer sidecar exe'si üretir).
- `dist` klasöründen en az şu iki dosyayı birlikte dağıtın: `BaylanSignageAgent.exe`, `widget_viewer.exe`.
- Widget viewer için ayrıca `cefpython3` veya `pywebview` bağımlılıklarından en az biri kurulu olmalıdır.
- CEF backend'i frozen dağıtıma dahil etmek için build'i `-EnableCefCollect` ile çalıştırın. Bu parametre, `cefpython3` kuruluysa viewer PyInstaller çağrısına `--collect-all cefpython3` ekler.
- `-EnableCefCollect` verilse bile build hard-fail olmaz: `cefpython3` bulunamazsa CEF collect adımı uyarı ile atlanır.
- `pywebview` kuruluysa build script'i viewer tarafında gerekli paketleri (`--collect-all webview` + platform hidden import'ları) otomatik ekler; kurulu değilse bu adım da hataya düşmeden atlanır.

`WIDGET_SINGLE_ENGINE=1` paketleme notu:
- Agent build artık `client/widget_engine.html` dosyasını `BaylanSignageAgent.exe` içine gömer; runtime controller bu gömülü kaynağı kullanır.
- Sidecar `widget_viewer.exe` build'i aynı dosyayı executable yanında erişilebilir olacak şekilde paketler; tek-engine akışı viewer tarafında da çalışır.
- Bu nedenle dağıtımda `widget_engine.html` dosyasını ayrıca kopyalamanız gerekmez (build script kullanıldığı sürece).

Viewer build'ini kapatmak isterseniz `-SkipViewerBuild` parametresini geçebilirsiniz; bu durumda frozen modda Python widget viewer devre dışı kalır.
