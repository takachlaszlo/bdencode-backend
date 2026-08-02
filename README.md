# BDEncode Backend

Headless, tartós Blu-ray/UHD Blu-ray kódoló backend x264/x265 kimenethez. A projekt ebben a fázisban csak a backend, a webes frontend később erre az API-ra épül.

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
- progresszív forrásnál alapból az eredeti és a kész bitstream I/P/B típusa is egyezik;
- hivatalos standalone VMAF + PSNR/SSIM, privát FIFO-kon keresztül, nagy nyers köztes fájlok nélkül;
- lossless PNG, HDR-native 16 bites és determinisztikus SDR proof;
- teljes dekódolási QC, MediaInfo/MKVInfo, végső MKV codec/profile/szín/HDR10 hard gate, audio PCM/sample/delay/layout/loudness/phase/spektrum;
- ImgBB feltöltés byteazonos visszaellenőrzéssel és BBCode-generálással;
- raw és kitisztított log; az MKV csak a kitisztított encode logot kapja meg, comparison fájlokat soha;
- MPLS/CLPI/PMT nyelv-provenance, ismeretlen audiónál CPU-only faster-whisper mintavétel, bizonytalanságnál review;
- systemd API/worker/update szolgáltatások és Swizzin nginx `/encoder/` konfiguráció.

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
6. létrehozza és elindítja a systemd egységeket;
7. a Swizzin `/etc/htpasswd` védelmét újrahasználva beköti az nginx `/encoder/` útvonalat.

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
```

Az API csak `127.0.0.1:8796` címen figyel. Nginx mögött az OpenAPI dokumentáció: `/encoder/api/v1/docs`.

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
- `POST /api/v1/jobs/{id}/selection`
- `GET /api/v1/scans?job_id=...`
- `GET /api/v1/events?job_id=...`
- `GET /api/v1/artifacts?job_id=...`
- `GET /api/v1/analyze-mkv?path=...`

A job először lemezscanre kerül. Több playlist/edition vagy bizonytalan sáv esetén `AWAITING_SELECTION` / `NEEDS_REVIEW` állapotban blokkolja a sort; a javított selection ugyanazon endpointon újraküldhető, és biztonsági okból a pipeline `READY` állapottól újraellenőriz mindent. A következő encode csak a teljes QC, comparison és feltöltés lezárása után indulhat.

## CPU-korlát

A worker systemd `CPUQuota` értéke a logikai CPU-k számának 80%-ára készül. Például 12 logikai CPU esetén `960%`, ami a teljes subprocess-fára együtt legfeljebb 9,6 CPU-t jelent. A systemd `CPUQuota=80%` önmagában csak egy CPU 80%-a lenne, ezért azt a telepítő szándékosan nem használja.

## Teszt

```bash
python -m pip install -e '.[test]'
python -m pytest
```

Részletes API/selection leírás: [docs/API.md](docs/API.md). Pipeline és artifact szabályok: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
