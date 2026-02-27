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

Basit bind (eski davranış):
- `AD_DOMAIN`: Örn. `COMPANY`
- `AD_USER_DN_TEMPLATE` (opsiyonel): Örn. `CN={username},OU=Users,DC=company,DC=local`

Search + bind (ad-file-share benzeri önerilen yapı):
- `AD_BASE_DN`: Örn. `DC=company,DC=local`
- `AD_BIND_DN` (opsiyonel): Arama için servis hesabı DN'i
- `AD_BIND_PASSWORD` (opsiyonel): Servis hesabı parolası
- `AD_USER_SEARCH_FILTER`: Varsayılan `(&(objectClass=user)(sAMAccountName={username}))`

Not: Panel API çağrılarında mevcut `SHARED_SECRET` (`X-SECRET`) kontrolü devam eder.
