# BDEncode

Tartós Blu-ray/UHD Blu-ray kódoló rendszer x264/x265 kimenethez, headless backenddel és a Swizzin nginx mögött futó, reszponzív webes kezelőfelülettel.

## Fő tulajdonságok

- normál BD (AVC, VC-1, MPEG-2) → x264;
- UHD HEVC → x265, kizárólag statikus HDR10-megőrzéssel;
- Dolby Vision, HDR10+ dinamikus metaadat és 3D/MVC kimenet tiltva;
- film, koncert, anime, sorozatlemez, több playlist/edition és seamless branching modell;
- sávonként `copy`, `flac` vagy `omit`, külön nyelv/név/default/forced/role;
- Blu-ray LPCM-nél a Matroska által nem támogatott bitstream-copy helyett kötelező a veszteségmentes FLAC vagy az `omit`;
- kezdő, haladó és profi encoder-séma;
- egyszerre pontosan egy teljes pipeline, SQLite WAL állapottal és reboot utáni folytatással;
- frame-pontos VapourSynth + BestSource referencia, FFmpeg/x264/x265 encode;
- kötelező I/P/B videopárok ugyanazon presentation index/PTS alapján;
- progresszív forrásnál az eredeti és a kész bitstream I/P/B típusa kötelezően egyezik;
- gyors, rövid időablakokra korlátozott I/P/B mintavétel, alapból öt lossless PNG-párral és a kiválasztott képeken számolt SSIM/PSNR metrikával; teljes filmes VMAF-menet nélkül;
- lossless PNG, HDR-native 16 bites és determinisztikus SDR proof;
- teljes dekódolási QC, MediaInfo/MKVInfo, végső MKV codec/profile/szín/HDR10 hard gate, audio PCM/sample/delay/layout/loudness/phase/spektrum;
- ImgBB feltöltés byteazonos visszaellenőrzéssel és BBCode-generálással;
- raw és kitisztított log; az MKV csak a kitisztított encode logot kapja meg, comparison fájlokat soha;
- MPLS/CLPI/PMT nyelv-provenance, ismeretlen audiónál CPU-only faster-whisper mintavétel, bizonytalanságnál review;
- systemd API/worker/update szolgáltatások és Swizzin nginx `/encoder/` konfiguráció.
- csempés webes kezelőfelület forrásböngészővel, várólistával, kezdő/haladó/profi beállításokkal, szerveroldali tervellenőrzéssel, log- és artifact-nézettel;
- interaktív I/P/B PNG comparison (csúszka, A/B, villogó és difference nézet), audio-spektrum párok és BBCode-másolás.

## Szervertelepítés

Debian 12 alatt, a célfelhasználóként:

```bash
bash install/install.sh
```

A telepítő:

1. létrehozza a verziózott alkalmazás- és tool-környezetet a `~/encode/app` alatt;
2. telepíti és teszteli a Python csomagot;
3. külön Python 3.12 környezetbe telepíti a VapourSynth/BestSource/Bwdif/VIVTC toolchaint;
4. lefordítja a verziórögzített hivatalos VMAF CLI-t;
5. telepíti a natív libbluray JSON scannert;
6. a repóban lévő, előre buildelt `frontend/dist` csomagot root-owned, verziózott web release-be telepíti;
7. létrehozza és elindítja a systemd egységeket;
8. a Swizzin `/etc/htpasswd` védelmét újrahasználva beköti az nginx `/encoder/` UI- és API-útvonalat.

## Eltávolítás

A program, a systemd/nginx integráció és a verziózott alkalmazáskód eltávolítása,
a queue, a munkák és a kész kimenetek megőrzésével:

```bash
bash install/uninstall.sh
```

Ha egy félbemaradt telepítés visszaállította a configot, a pontos gyökereket
kötelező megadni. A teljes, még használatlan munkakönyvtár törlése:

```bash
bash install/uninstall.sh \
  --data-root "$HOME/encode" \
  --source-root /storage \
  --purge-data \
  --confirm-data-root "$HOME/encode"
```

A source gyökér soha nem törlési célpont. A teljes adattörlés az átfedő
source/data útvonalat, symlinket, mountpointot, idegen tulajdonost és túl tág
rendszerútvonalat elutasítja. Aktív queue vagy helyreállítási tranzakció mellett
az eltávolítás nem indul el. Az ImgBB credential csak a külön
`--purge-credential` kapcsolóval törlődik.

Az APT-csomagokat az általános eltávolító szándékosan megőrzi: a régi
telepítések nem rögzítették, mely csomagok voltak már korábban a szerveren,
ezért azok automatikus eltávolítása más programokat is érinthetne. A Git checkoutot
szintén külön kell törölni.

Alapértelmezett útvonalak:

```text
/home/accofil/storage/       source-ok
/home/accofil/encode/
  app/                       verziózott alkalmazás/toolchain
  state/encoder.sqlite3      tartós queue és események
  jobs/<job-id>/             work, raw log, analysis, comparison
  completed/                 kész release-ek
  cache/                     indexek/modellek/build cache
  updates/                   napi update riportok
/var/www/bdencode/
  releases/<release-id>/     változatlan frontend release-ek
  current -> releases/...    atomikusan cserélt web pointer
```

Az API csak `127.0.0.1:8796` címen figyel. A kezelőfelület nginx mögött a `/encoder/`, az OpenAPI dokumentáció a `/encoder/api/v1/docs` útvonalon érhető el. Mindkettő a meglévő Swizzin Basic Auth védelmét használja.

## Frontend fejlesztése

Node.js 22.22+ és pnpm 11 szükséges:

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm typecheck
pnpm test
pnpm build
```

A Vite build rögzített base pathja `/encoder/`. A szerver nem futtat Node.js-t: a telepítő kizárólag a tesztelt, repóban lévő `frontend/dist` tartalmát publikálja.

## ImgBB credential

A kulcs nem kerülhet configba, adatbázisba, logba vagy Gitbe. A worker systemd encrypted credentialt olvas `imgbb-api-key` néven. Példa új kulcs rögzítésére:

```bash
read -rsp 'ImgBB API key: ' bdencode_imgbb_key
printf '%s\n' "$bdencode_imgbb_key" | sudo systemd-creds encrypt \
  --name=imgbb-api-key - "$HOME/.config/bdencode/imgbb-api-key.cred"
unset bdencode_imgbb_key
chmod 600 "$HOME/.config/bdencode/imgbb-api-key.cred"
```

A már futó workerhez ezután a telepítőt újra kell futtatni, vagy a credential drop-int kézzel frissíteni.

## Használat

```bash
bdencode doctor --json
bdencode init-db
bdencode api
bdencode worker
```

Fontos API-k:

- `GET /api/v1/health`
- `GET /api/v1/runtime-capabilities`
- `GET /api/v1/sources`
- `GET /api/v1/profiles/{x264|x265}/schema`
- `POST /api/v1/jobs`
- `POST /api/v1/jobs/{id}/selection/validate`
- `POST /api/v1/jobs/{id}/selection`
- `GET /api/v1/scans?job_id=...`
- `GET /api/v1/events?job_id=...`
- `GET /api/v1/artifacts?job_id=...`
- `GET /api/v1/analyze-mkv?path=...`

A job először lemezscanre kerül. Több playlist/edition vagy bizonytalan sáv esetén `AWAITING_SELECTION` / `NEEDS_REVIEW` állapotban blokkolja a sort; a javított selection ugyanazon endpointon újraküldhető, és biztonsági okból a pipeline `READY` állapottól újraellenőriz mindent. A következő encode csak a teljes QC, comparison és feltöltés lezárása után indulhat.

## Gyors videó-comparison

A comparison nem pásztázza végig képkockánként a teljes source-ot és a kész MKV-t. Rövid, korlátozott probe-ablakokból választ időben elosztott I-, P- és B-frame-eket, majd az azonos presentation indexű reference/encode képeket veszteségmentes PNG-ként menti. Alapértelmezésben összesen öt képpár készül; a `comparison_pair_count` értéke 3 és 5 között állítható. Mindhárom képtípusból legalább egy pár kötelező.

Az SSIM és PSNR kizárólag ezeken a kiválasztott PNG-párokon fut, ezért nincs többórás teljes filmes VMAF vagy teljes fájlos SSIM/PSNR menet. A videó-comparison teljes szerveroldali időkerete öt perc; túllépéskor kontrollált ellenőrzést kér, nem folytat korlátlan háttérmunkát. A régi `comparison_frames_per_type` konfigurációs kulcsot a betöltő visszafelé kompatibilitásból továbbra is elfogadja, de az új comparison már nem használja.

## CPU-korlát

A worker systemd `CPUQuota` értéke a logikai CPU-k számának 80%-ára készül. Például 12 logikai CPU esetén `960%`, ami a teljes subprocess-fára együtt legfeljebb 9,6 CPU-t jelent. A systemd `CPUQuota=80%` önmagában csak egy CPU 80%-a lenne, ezért azt a telepítő szándékosan nem használja.

## Tranzakciós napi frissítés

A host FFmpeg/MKVToolNix/MediaInfo/libbluray/x264/x265 csomagjai csak ellenőrzött APT-tranzakcióban frissülhetnek. Globális source-package pin védi őket attól, hogy az unattended-upgrades vagy egy általános APT upgrade megkerülje ezt az utat; a frissítő saját, izolált APT-scope-ja tudja kontrolláltan feloldani a védelmet. Előre letölti az új `.deb` fájlokat, `dpkg-repack` segítségével elkészíti valamennyi ténylegesen változó csomag pontos régi visszaállító példányát, majd csomagazonosítót, verziót, architektúrát és SHA-256 hasht rögzít. Új csomag, eltávolítás, hold, Essential/Protected csomag vagy az engedélyezett média source-listán kívüli függőség esetén még a módosítás előtt leáll.

Az APT guard kizárólag a vaultban szereplő helyi csomagokat engedi a `dpkg` elé. A tranzakció csak a codec-smoke tesztek, a `doctor`, az API healthcheck és a worker valódi systemd readiness-jelzése után lesz végleges. Bármely hiba visszaállítja a régi tool-symlinket és az összes régi csomagot; a szolgáltatások és az APT-timerek előző állapota is tartós journal része. SIGKILL vagy áramszünet után a boot előtti `bdencode-update-recovery.service` ugyanebből fejezi be a rollbacket, és sikertelen recovery esetén blokkolja az API/worker, valamint az `apt-daily` indulását. A csomagvault helye `/var/lib/bdencode/apt-transactions`, a runtime journalé `/var/lib/bdencode/update-runtime`; az utolsó három lezárt csomagtranzakció megmarad.

A telepítő külön, fix célpontlistás rollback-snapshotot tart a változó host unitokról, az APT drop-injeiről, nginx beállításáról és az app/tool release pointereiről. Az `apt-daily` timerek aktív/engedélyezett állapota is a journal része: a telepítő a csomagművelet előtt leállítja őket, kivárja a futó dpkg-t, korán telepíti a media pint, majd pontosan visszaállítja az előállapotot. A telepítő `apt-get` gyermeke külön tartós lockot örököl; a watchdog kivárja ezt és a natív apt/dpkg lockokat, majd tiszta `dpkg --audit` és `apt-get check` nélkül nem indít runtime-ot. A recovery helper, az API/worker stabil recovery drop-inje és az install-watchdog szándékosan a snapshoton kívül marad, így a visszaállítás a régi runtime-unitok visszatérése után is folytatható. A journal két fázist különböztet meg: a queue vizsgálata alatt egy futó encode-ot soha nem állít le, tényleges mutáció után viszont teljes rollbacket végez. Normál hiba esetén azonnal, SIGKILL után a négy install/runtime/APT markert figyelő `bdencode-install-recovery.path`, rebootkor pedig a recovery gate fejezi be; a journal helye `/var/lib/bdencode/install-transactions`.

A telepítő egy kizárólag `AWAITING_SELECTION` állapotban szünetelő job mellett is frissítheti az alkalmazást, mert ekkor még nem készült encode és a választás nincs jóváhagyva. `READY`, `NEEDS_REVIEW`, kódolás, mux, QC, comparison vagy feltöltés alatt továbbra is fail-closed módon elhalasztja a telepítést.

## Teszt

```bash
python -m pip install -e '.[test]'
python -m pytest
cd frontend && pnpm install --frozen-lockfile && pnpm typecheck && pnpm test && pnpm build
```

Részletes API/selection leírás: [docs/API.md](docs/API.md). Pipeline és artifact szabályok: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
