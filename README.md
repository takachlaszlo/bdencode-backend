# BDEncode

Tartós Blu-ray/UHD Blu-ray kódoló rendszer x264/x265 kimenethez, headless backenddel és reszponzív webes kezelőfelülettel. Szerveren Swizzin nginx mögött, Windows alatt helyi WSL2 szolgáltatásként fut.

## Fő tulajdonságok

- normál BD (AVC, VC-1, MPEG-2) → x264;
- UHD HEVC → x265, kizárólag statikus HDR10-megőrzéssel;
- Dolby Vision, HDR10+ dinamikus metaadat és 3D/MVC kimenet tiltva;
- film, koncert, anime, sorozatlemez, több playlist/edition és seamless branching modell;
- sávonként `copy`, `flac`, `ac3`, `eac3`, `dts` vagy `omit`, külön nyelv/név/default/forced/role;
- rögzített minőségi audio presetek: AC-3 640 kb/s, E-AC-3 1024 kb/s és DTS core 1536 kb/s, 48 kHz-en, legfeljebb 5.1 csatornával;
- DTS-HD célzott DTS-konverziójánál újrakódolás nélküli `dca_core` kinyerés; TrueHD/egyéb forrásnál ellenőrzött DTS core átkódolás;
- Blu-ray LPCM-nél a Matroska által nem támogatott bitstream-copy helyett kötelező a FLAC/AC-3/E-AC-3/DTS konverzió vagy az `omit`;
- kezdő, haladó és profi encoder-séma;
- egyszerre pontosan egy teljes pipeline, SQLite WAL állapottal és reboot utáni folytatással;
- frame-pontos VapourSynth + BestSource referencia, FFmpeg/x264/x265 encode;
- kötelező I/P/B videopárok ugyanazon presentation index/PTS alapján;
- progresszív forrásnál az eredeti és a kész bitstream I/P/B típusa kötelezően egyezik;
- gyors, rövid időablakokra korlátozott I/P/B mintavétel, alapból öt lossless PNG-párral és a kiválasztott képeken számolt SSIM/PSNR metrikával; teljes filmes VMAF-menet nélkül;
- lossless PNG, HDR-native 16 bites és determinisztikus SDR proof;
- teljes dekódolási QC, MediaInfo/MKVInfo, végső MKV codec/profile/szín/HDR10 hard gate; veszteségmentes audiónál PCM-hash, veszteséges audiónál cél-codec/bitráta/mintavétel/csatorna/időzítés, továbbá loudness/phase/spektrum;
- választható automatikus ImgBB → Catbox → Freeimage tartaléklánc vagy kézzel rögzített szolgáltató, egyetlen hostra zárt csomaggal, byteazonos visszaellenőrzéssel és BBCode-generálással;
- raw és kitisztított log; az MKV csak a kitisztított encode logot kapja meg, comparison fájlokat soha;
- MPLS/CLPI/PMT nyelv-provenance, ismeretlen audiónál CPU-only faster-whisper mintavétel, bizonytalanságnál review;
- systemd API/worker/update szolgáltatások és Swizzin nginx `/encoder/` konfiguráció.
- csempés webes kezelőfelület forrásböngészővel, várólistával, kezdő/haladó/profi beállításokkal, szerveroldali tervellenőrzéssel, log- és artifact-nézettel;
- interaktív I/P/B PNG comparison (csúszka, A/B, villogó és difference nézet), audio-spektrum párok és BBCode-másolás.

## Windows 10/11 telepítés

A Windows-változat WSL2-ben futtatja ugyanazt a Debian backendet és média-
toolchaint, ezért nincs külön, eltérően viselkedő Windows encoder. A webes
felületet a normál Windows böngészőben használod; videokártya továbbra sem
szükséges.

Feltételek:

- 64 bites Windows 10 2004 vagy újabb, illetve Windows 11;
- bekapcsolható hardveres virtualizáció és rendszergazdai jogosultság;
- legalább 100 GB szabad hely ajánlott a WSL virtuális lemezén;
- a BDMV forrás egy helyi, meghajtóbetűjeles Windows-mappában legyen.

Telepítés:

1. töltsd le vagy klónozd ezt a repót;
2. kattints duplán az `install/windows-install.cmd` fájlra;
3. engedélyezd a rendszergazdai kérést, majd válaszd ki a BDMV-ket tartalmazó
   mappát vagy meghajtót;
4. ha a WSL még nincs telepítve, a gép egyszeri újraindítása szükséges; a
   telepítő a következő bejelentkezéskor automatikusan folytatódik.

A telepítő létrehozza a `BDEncode` webes és az `BDEncode elkészült filmek`
asztali parancsikont. A felület alapértelmezett címe:

```text
http://localhost:8787/encoder/
```

A forrás a Windows-meghajtón marad és a worker csak olvassa. A gyors, sok
apró fájlt használó munkaterület a WSL Linux-fájlrendszerébe kerül; a kész
fájlok Windowsból a létrehozott parancsikonnal vagy ezen az útvonalon érhetők
el:

```text
\\wsl.localhost\Debian\home\<felhasználó>\encode\completed
```

A helyi webkiszolgáló kizárólag `127.0.0.1:8787` címen figyel, ezért az otthoni
hálózat felől nem nyit portot. Ne készíts hozzá routeres porttovábbítást. UNC
és közvetlen hálózati megosztás jelenleg nem választható forrásnak; azt előbb
Windows-meghajtóbetűjelhez kell csatlakoztatni. Meglévő, nem a BDEncode által
kezelt `Debian` WSL-disztribúciót a telepítő alapból nem módosít.

## Szervertelepítés

Debian 12 vagy 13 alatt, a célfelhasználóként:

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
az eltávolítás nem indul el. A képfeltöltő credentialök csak a külön
`--purge-credentials` kapcsolóval törlődnek.

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

## Képfeltöltő credentialök

A kulcsok nem kerülhetnek configba, adatbázisba, logba vagy Gitbe. A worker három külön systemd encrypted credentialt támogat:

- `imgbb-api-key` – az elsődleges ImgBB API-kulcs;
- `catbox-userhash` – opcionális, de a fiókhoz kötött és kezelhető Catbox-feltöltéshez szükséges;
- `freeimage-api-key` – a Freeimage.host API-kulcs.

Mindegyiket rejtett terminálbevitellel, stdinről kell titkosítani. Példa:

```bash
provision_bdencode_credential() (
  set -Eeuo pipefail
  local credential_name="$1" prompt="$2"
  local credential_directory="$HOME/.config/bdencode"
  local credential_tmp bdencode_image_secret
  sudo -v
  install -d -m 700 "$credential_directory"
  credential_tmp="$(mktemp "$credential_directory/.${credential_name}.cred.XXXXXX")"
  trap 'rm -f -- "$credential_tmp"' EXIT
  read -rsp "$prompt" bdencode_image_secret
  printf '\n'
  [[ -n "$bdencode_image_secret" ]] || {
    echo 'Az üres credential nem megengedett.' >&2
    return 1
  }
  printf '%s' "$bdencode_image_secret" | sudo systemd-creds encrypt \
    --name="$credential_name" - - >"$credential_tmp"
  unset bdencode_image_secret
  chmod 600 "$credential_tmp"
  mv -f -- "$credential_tmp" "$credential_directory/${credential_name}.cred"
  trap - EXIT
)
provision_bdencode_credential imgbb-api-key 'ImgBB API key: '
unset -f provision_bdencode_credential
```

A másik két rögzített név `catbox-userhash` és `freeimage-api-key`; a fenti
`credential_name` értékét kell ezekre cserélni. A stdout-átirányítást a
célfelhasználó shellje nyitja meg, ezért az encrypted fájl nem lesz
root-tulajdonú. A telepítő csak symlinkmentes, a célfelhasználó tulajdonában
lévő `0700` mappából, `0600` módú, visszafejthető credentialt fogad el. A már
futó workerhez ezután a telepítőt újra kell futtatni.

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

A job először az önálló előkészítési sávban lemezscanre kerül. Ez a könnyű scan egy már futó encode mellett is dolgozhat, de egyszerre csak egy lemezt vizsgál. A scan után több job is várhat `AWAITING_SELECTION` állapotban; a jóváhagyott paraméterekkel `READY` állapotba kerülnek. A sor első READY munkája csak akkor foglalhatja el az egyetlen encode sávot, amikor az előző encode teljes QC-, comparison- és feltöltési folyamata lezárult. Egy késői `NEEDS_REVIEW` továbbra is megtartja az encode sávot, így egy későbbi munka nem előzheti meg.

## Gyors videó-comparison

A comparison nem pásztázza végig képkockánként a teljes source-ot és a kész MKV-t. Rövid, korlátozott probe-ablakokból választ időben elosztott I-, P- és B-frame-eket, majd az azonos presentation indexű reference/encode képeket veszteségmentes PNG-ként menti. Minden megosztható PNG külön, a képet nem takaró fejlécben jelzi a `SOURCE`/`ENCODE` szerepet, a nullától számozott presentation frame indexet és az I/P/B típust. Időbeli transzformáció után a source fejléc pontosan `MATCHED TO …-FRAME` jelölést használ, mert ott eredeti bitstream-képtípus nem állítható. Alapértelmezésben összesen öt képpár készül; a `comparison_pair_count` értéke 3 és 5 között állítható. Mindhárom képtípusból legalább egy pár kötelező.

Az SSIM és PSNR a kiválasztott képpárok felirat nélküli, belső pixelmásolatán fut, ezért a különböző `SOURCE`/`ENCODE` fejléc nem torzítja a mérést. Nincs többórás teljes filmes VMAF vagy teljes fájlos SSIM/PSNR menet. A videó-comparison teljes szerveroldali időkerete öt perc; túllépéskor kontrollált ellenőrzést kér, nem folytat korlátlan háttérmunkát. A régi `comparison_frames_per_type` konfigurációs kulcsot a betöltő visszafelé kompatibilitásból továbbra is elfogadja, de az új comparison már nem használja.

## CPU-korlát

A worker systemd `CPUQuota` értéke a logikai CPU-k számának 80%-ára készül. Például 12 logikai CPU esetén `960%`, ami a teljes subprocess-fára együtt legfeljebb 9,6 CPU-t jelent. A systemd `CPUQuota=80%` önmagában csak egy CPU 80%-a lenne, ezért azt a telepítő szándékosan nem használja.

## Tranzakciós napi frissítés

A host FFmpeg/MKVToolNix/MediaInfo/libbluray/x264/x265 csomagjai csak ellenőrzött APT-tranzakcióban frissülhetnek. Globális source-package pin védi őket attól, hogy az unattended-upgrades vagy egy általános APT upgrade megkerülje ezt az utat; a frissítő saját, izolált APT-scope-ja tudja kontrolláltan feloldani a védelmet. Előre letölti az új `.deb` fájlokat, `dpkg-repack` segítségével elkészíti valamennyi ténylegesen változó csomag pontos régi visszaállító példányát, majd csomagazonosítót, verziót, architektúrát és SHA-256 hasht rögzít. Új csomag, eltávolítás, hold, Essential/Protected csomag vagy az engedélyezett média source-listán kívüli függőség esetén még a módosítás előtt leáll.

Az APT guard kizárólag a vaultban szereplő helyi csomagokat engedi a `dpkg` elé. A tranzakció csak a codec-smoke tesztek, a `doctor`, az API healthcheck és a worker valódi systemd readiness-jelzése után lesz végleges. Bármely hiba visszaállítja a régi tool-symlinket és az összes régi csomagot; a szolgáltatások és az APT-timerek előző állapota is tartós journal része. SIGKILL vagy áramszünet után a boot előtti `bdencode-update-recovery.service` ugyanebből fejezi be a rollbacket, és sikertelen recovery esetén blokkolja az API/worker, valamint az `apt-daily` indulását. A csomagvault helye `/var/lib/bdencode/apt-transactions`, a runtime journalé `/var/lib/bdencode/update-runtime`; az utolsó három lezárt csomagtranzakció megmarad.

A telepítő külön, fix célpontlistás rollback-snapshotot tart a változó host unitokról, az APT drop-injeiről, nginx beállításáról és az app/tool release pointereiről. Az `apt-daily` timerek aktív/engedélyezett állapota is a journal része: a telepítő a csomagművelet előtt leállítja őket, kivárja a futó dpkg-t, korán telepíti a media pint, majd pontosan visszaállítja az előállapotot. A telepítő `apt-get` gyermeke külön tartós lockot örököl; a watchdog kivárja ezt és a natív apt/dpkg lockokat, majd tiszta `dpkg --audit` és `apt-get check` nélkül nem indít runtime-ot. A recovery helper, az API/worker stabil recovery drop-inje és az install-watchdog szándékosan a snapshoton kívül marad, így a visszaállítás a régi runtime-unitok visszatérése után is folytatható. A journal két fázist különböztet meg: a queue vizsgálata alatt egy futó encode-ot soha nem állít le, tényleges mutáció után viszont teljes rollbacket végez. Normál hiba esetén azonnal, SIGKILL után a négy install/runtime/APT markert figyelő `bdencode-install-recovery.path`, rebootkor pedig a recovery gate fejezi be; a journal helye `/var/lib/bdencode/install-transactions`.

A telepítő `QUEUED`, `AWAITING_SELECTION` vagy még el nem indított `READY`
munkák mellett is frissítheti az alkalmazást. Aktív `SCANNING`, `NEEDS_REVIEW`,
kódolás, mux, QC, comparison vagy `UPLOADING` alatt fail-closed módon halaszt.
Tartós `UPLOAD_FAILED` állapotban minden helyi eredmény és feltöltési checkpoint
lezártan vár, ezért frissítés után csak a feltöltés folytatódik.

## Teszt

```bash
python -m pip install -e '.[test]'
python -m pytest
cd frontend && pnpm install --frozen-lockfile && pnpm typecheck && pnpm test && pnpm build
```

Részletes API/selection leírás: [docs/API.md](docs/API.md). Pipeline és artifact szabályok: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
