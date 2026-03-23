# Client Dağıtım Pipeline’ı (Windows)

Bu doküman, agent uygulamasının Windows istemcilere kurumsal ölçekte dağıtımı için uçtan uca bir pipeline tanımlar:

1. Service wrapper ile Windows Service olarak çalıştırma
2. MSI paketleme ve GPO ile konfigürasyon enjeksiyonu
3. Loglama, crash recovery ve otomatik restart politikaları
4. Silent install/uninstall komutları + GPO startup script örnekleri
5. Versiyonlu rollout (pilot → kademeli) ve rollback prosedürü

---

## 1) Agent’i Windows Service olarak paketleme

Aşağıdaki seçeneklerden **birini** standartlaştırın:

- **NSSM**: Hızlı ve pratik, düşük karmaşıklık
- **WinSW**: XML tabanlı, policy ve log yönetimi daha düzenli
- **Native wrapper (SCM API / .NET Worker Service)**: Uzun vadede en temiz, geliştirme maliyeti daha yüksek

> Öneri: Operasyonel izlenebilirlik için WinSW; hızlı başlangıç için NSSM.

### 1.1 NSSM ile servis kurulum örneği

Dosya yerleşimi (öneri):

- `C:\Program Files\BaylanSignageAgent\agent.exe`
- `C:\ProgramData\Baylan\SignageAgent\logs\`
- `C:\ProgramData\Baylan\SignageAgent\config\agent.json`

Kurulum komutları:

```powershell
nssm install BaylanSignageAgent "C:\Program Files\BaylanSignageAgent\agent.exe"
nssm set BaylanSignageAgent AppDirectory "C:\Program Files\BaylanSignageAgent"
nssm set BaylanSignageAgent AppParameters "--config C:\ProgramData\Baylan\SignageAgent\config\agent.json"
nssm set BaylanSignageAgent AppStdout "C:\ProgramData\Baylan\SignageAgent\logs\agent.out.log"
nssm set BaylanSignageAgent AppStderr "C:\ProgramData\Baylan\SignageAgent\logs\agent.err.log"
nssm set BaylanSignageAgent Start SERVICE_AUTO_START
nssm set BaylanSignageAgent AppExit Default Restart
nssm set BaylanSignageAgent AppThrottle 1500
nssm set BaylanSignageAgent AppRestartDelay 5000
nssm start BaylanSignageAgent
```

### 1.2 WinSW ile servis kurulum örneği

`BaylanSignageAgent.xml`:

```xml
<service>
  <id>BaylanSignageAgent</id>
  <name>Baylan Signage Agent</name>
  <description>Baylan istemci agent servisi</description>

  <executable>C:\Program Files\BaylanSignageAgent\agent.exe</executable>
  <arguments>--config C:\ProgramData\Baylan\SignageAgent\config\agent.json</arguments>
  <workingdirectory>C:\Program Files\BaylanSignageAgent</workingdirectory>

  <startmode>Automatic</startmode>
  <onfailure action="restart" delay="10 sec"/>
  <onfailure action="restart" delay="30 sec"/>
  <onfailure action="restart" delay="60 sec"/>

  <log mode="roll-by-size-time">
    <sizeThreshold>10240</sizeThreshold>
    <pattern>yyyyMMdd</pattern>
    <keepFiles>14</keepFiles>
  </log>
</service>
```

Komutlar:

```powershell
BaylanSignageAgent.exe install
BaylanSignageAgent.exe start
```

---

## 2) MSI üretimi + config enjeksiyonu (GPO)

### 2.1 MSI paket içeriği

MSI aşağıdakileri kurmalı:

- Agent binary + bağımlılıklar (`Program Files`)
- Service wrapper (NSSM/WinSW)
- Varsayılan config şablonu (`ProgramData`)
- Log klasörleri ve ACL
- Install/upgrade sırasında service stop-start aksiyonu

Araç önerisi:

- **WiX Toolset v4** (kurumsal MSI standardı)

### 2.2 Konfigürasyon modeli

Config değerleri:

- `server_url`
- `tenant_id`
- `client_cert_thumbprint` veya cert path

Önerilen öncelik sırası:

1. Registry (GPO Preferences ile)
2. `C:\ProgramData\Baylan\SignageAgent\config\agent.json`
3. MSI default config

Agent açılışında bu sırayla okuyup doğrulamalı (validation + fail-fast log).

### 2.3 GPO ile Registry enjeksiyonu

Registry path (öneri):

- `HKLM\SOFTWARE\Baylan\SignageAgent`

Örnek değerler:

- `ServerUrl` (REG_SZ)
- `TenantId` (REG_SZ)
- `CertThumbprint` (REG_SZ)

GPO:

- `Computer Configuration > Preferences > Windows Settings > Registry`
- Item-level targeting ile OU / Security Group bazlı parametre seti uygulanabilir.

### 2.4 GPO ile dosya enjeksiyonu

Alternatif:

- `Computer Configuration > Preferences > Files`
- Merkez share’den `agent.json` kopyalanır.

Sertifika yönetimi:

- `Computer Configuration > Policies > Windows Settings > Security Settings > Public Key Policies`
- Gerekirse client cert private key ACL’i servis hesabına verilir.

---

## 3) Log path, crash recovery, restart policy

### 3.1 Loglama standardı

- Uygulama logları: `C:\ProgramData\Baylan\SignageAgent\logs\agent.log`
- State transition log (client varsayılan): `C:\ProgramData\BaylanSignage\state_transitions.jsonl` (`STATE_LOG_PATH` ile override edilebilir)
- Wrapper logları: NSSM stdout/stderr veya WinSW rolling log
- Event Log: kritik hatalar için `Application` kanalına yazım

Rotasyon:

- Günlük veya boyut bazlı (ör. 10 MB)
- Retention: 14–30 gün

### 3.2 Crash recovery

SCM Recovery ayarları (öneri):

- First failure: Restart service (10 sn)
- Second failure: Restart service (30 sn)
- Subsequent failures: Restart service (60 sn)
- Reset fail count: 1 day

PowerShell:

```powershell
sc.exe failure BaylanSignageAgent reset= 86400 actions= restart/10000/restart/30000/restart/60000
sc.exe failureflag BaylanSignageAgent 1
```

### 3.3 Servis hesabı

- Tercihen: `LocalService` veya domain managed service account (gMSA)
- En az yetki prensibi (ProgramData/config/log yazımı için gerekli ACL)

---

## 4) Silent install/uninstall + GPO startup scripts

### 4.1 Silent install/uninstall komutları

Install:

```powershell
msiexec /i "BaylanSignageAgent-x.y.z.msi" /qn /norestart /L*v "C:\Windows\Temp\BaylanSignageAgent-install.log"
```

Upgrade (aynı ProductCode/UpgradeCode stratejisine göre):

```powershell
msiexec /i "BaylanSignageAgent-x.y.z.msi" /qn /norestart REINSTALL=ALL REINSTALLMODE=vomus /L*v "C:\Windows\Temp\BaylanSignageAgent-upgrade.log"
```

Uninstall:

```powershell
msiexec /x "{PRODUCT-CODE-GUID}" /qn /norestart /L*v "C:\Windows\Temp\BaylanSignageAgent-uninstall.log"
```

### 4.2 GPO Startup script örneği (install/upgrade)

`\\domain.local\SYSVOL\domain.local\scripts\Install-BaylanSignageAgent.ps1`

```powershell
$msiPath = "\\fileserver\packages\BaylanSignageAgent-1.4.2.msi"
$logPath = "C:\Windows\Temp\BaylanSignageAgent-gpo-install.log"

if (-Not (Test-Path $msiPath)) {
  exit 1
}

Start-Process -FilePath "msiexec.exe" -ArgumentList "/i `"$msiPath`" /qn /norestart /L*v `"$logPath`"" -Wait -NoNewWindow

# Opsiyonel: servis durumu doğrulama
$svc = Get-Service -Name "BaylanSignageAgent" -ErrorAction SilentlyContinue
if ($null -eq $svc -or $svc.Status -ne 'Running') {
  exit 2
}

exit 0
```

### 4.3 GPO Startup script örneği (uninstall)

```powershell
$productCode = "{PRODUCT-CODE-GUID}"
$logPath = "C:\Windows\Temp\BaylanSignageAgent-gpo-uninstall.log"

Start-Process -FilePath "msiexec.exe" -ArgumentList "/x $productCode /qn /norestart /L*v `"$logPath`"" -Wait -NoNewWindow
exit 0
```

---

## 5) Versiyonlu rollout ve rollback prosedürü

### 5.1 Ring tabanlı rollout

Örnek ring modeli:

- **Ring 0 (Pilot):** IT + test kiosklar (%5)
- **Ring 1:** düşük kritik lokasyonlar (%20)
- **Ring 2:** kalan lokasyonlar (%75)

Her ring için giriş kriteri:

- Servis uptime ≥ %99
- Crash loop yok
- Telemetri/heartbeat normal
- Kritik incident sayısı 0

Örnek takvim:

- Gün 1–2: Ring 0
- Gün 3–4: Ring 1
- Gün 5+: Ring 2

### 5.2 Dağıtım kontrol noktaları

- MSI checksum doğrulaması (SHA-256)
- İmzalı binary doğrulaması
- Kurulum sonrası service health check
- Config doğrulaması (server_url/tenant/cert)
- Merkezi dashboard’da sürüm dağılımı izleme

### 5.3 Rollback prosedürü

Rollback tetikleyicileri:

- Crash rate eşik üstü (ör. >%3 cihaz)
- Heartbeat kaybı (ör. >%5 cihaz, 15 dk)
- Kritik fonksiyon kaybı

Adımlar:

1. İlgili ring GPO deployment’ını **duraklat**
2. Önceki stabil MSI’a yönlendir (örn. `1.4.1`)
3. Zorunlu downgrade startup script’i uygula
4. Problemli versiyon için service disable gerekiyorsa geçici politika uygula
5. RCA tamamlanana kadar sadece pilotta yeniden dene

Rollback komut örneği:

```powershell
# Önce mevcut sürümü kaldır
msiexec /x "{CURRENT-PRODUCT-CODE}" /qn /norestart

# Stabil sürümü kur
msiexec /i "\\fileserver\packages\BaylanSignageAgent-1.4.1.msi" /qn /norestart
```

---

## Operasyonel kontrol listesi (özet)

- [ ] Service wrapper standardı seçildi (NSSM/WinSW)
- [ ] MSI build pipeline (CI) oluşturuldu
- [ ] GPO ile config ve sertifika dağıtımı test edildi
- [ ] Silent install/uninstall script’leri doğrulandı
- [ ] Ring rollout + rollback runbook yayınlandı
- [ ] İzleme/alarm eşikleri dashboard’a işlendi


## 6) BAYLAN_CLIENT_BUILD değerini build sırasında binary içine gömme

`app/main.py` içindeki updater akışı, yüklenen dosyada `BAYLAN_CLIENT_BUILD:<değer>` marker’ını arar.
Aynı marker client tarafında da okunabildiği için build versiyonunu tek bir yerde taşıyabilirsiniz.

Önerilen format:

- `build-YYYYMMDDHHMMSS` (ör. `build-20260227153045`)

Repository'de client build + marker embed adımlarını tek bir yerde yöneten
`client/build_agent.ps1` script'i bulunur.

Kullanım:

```powershell
powershell -ExecutionPolicy Bypass -File client\build_agent.ps1
```

İsterseniz farklı python executable veya artifact adı verebilirsiniz:

```powershell
powershell -ExecutionPolicy Bypass -File client\build_agent.ps1 `
  -Python ".\.venv\Scripts\python.exe" `
  -Name "BaylanSignageAgent"
```

Onefile extraction kaynaklı `%TEMP%\_MEIxxxxx` sorunlarını azaltmak için build script,
runtime extraction klasörünü sabit bir dizine yönlendirir (`--runtime-tmpdir`).
Varsayılan dizin:

- `C:\ProgramData\BaylanSignage\RuntimeTmp`

Temizlik uygulaması:

- Çıkış anında silmeye güvenmeyin (crash/power-loss senaryolarında çalışmayabilir).
- Uygulama açılışında, kullanımda olmayan ve yaş eşiğini (varsayılan 24 saat) aşmış alt klasörler temizlenir.
- Dosya kilidi/alınamayan izin (`file in use` / `access denied`) durumlarında ilgili klasörü atlayıp sonraki açılışta tekrar deneyin.

İlgili env ayarları:

- `RUNTIME_TMP_DIR`
- `RUNTIME_TMP_CLEANUP_ENABLED`
- `RUNTIME_TMP_CLEANUP_MAX_AGE_HOURS`

Gerekirse override edebilirsiniz:

```powershell
powershell -ExecutionPolicy Bypass -File client\build_agent.ps1 `
  -RuntimeTmpDir "D:\BaylanSignage\RuntimeTmp"
```

Script'in yaptığı işlemler:

1. `pyinstaller` bağımlılığını kurar/günceller.
2. `client/client.py` dosyasını tek exe (`dist\BaylanSignageAgent.exe`) olarak build eder.
3. Binary sonuna `BAYLAN_CLIENT_BUILD:build-YYYYMMDDHHMMSS` marker'ını ekler.

Pipeline içinde sadece marker embed adımını ayrıca göstermek isterseniz:

```powershell
$buildVersion = "build-$(Get-Date -Format 'yyyyMMddHHmmss')"
$marker = "BAYLAN_CLIENT_BUILD:$buildVersion"

# Agent binary sonuna marker ekle
Add-Content -Path "dist\BaylanSignageAgent.exe" -Value $marker -Encoding ASCII -NoNewline

Write-Host "Embedded build marker: $buildVersion"
```

Not: Marker'dan önce/sonra newline olsa da regex araması marker'ı bulur; ancak binary sonuna ekstra satır sonu eklememek için `-NoNewline` önerilir.

Bu yöntemle:

- Client açılışta kendi sürümünü marker’dan okuyabilir.
- Server’a update yüklenince versiyon otomatik marker’dan çıkarılabilir.
- Manuel `CLIENT_BUILD_VERSION` verilmezse bile tarih-saat tabanlı tekil build sürümü korunur.

## 7) Ayrı bir Updater.exe ile swap/restart akışı

İstemciyi daha dayanıklı güncellemek için update işlemini ikinci bir executable'a ayırın:

1. Client yeni `agent.exe` dosyasını indirir.
2. Client `BaylanUpdater.exe` sürecini `--src/--dst/--old-pid` argümanlarıyla başlatır.
3. Client kendini kapatır.
4. Updater eski süreç kapanana kadar bekler, binary swap yapar ve uygulamayı yeniden başlatır.

Bu modelde process-kopyalama/yeniden başlatma mantığı ana process'ten ayrıldığı için,
kilitlenme veya yarım-kalma senaryolarında recovery daha güvenli olur.

Updater build için repository’de `client/build_updater.ps1` script'i bulunur:

```powershell
powershell -ExecutionPolicy Bypass -File client\build_updater.ps1
```

Script, `client/updater.py` dosyasını PyInstaller ile tek dosya exe'ye çevirip
`dist\BaylanUpdater.exe` üretir.
