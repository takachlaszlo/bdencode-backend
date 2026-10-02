# BDEncode 2.4 kiadási jegyzet

A 2.4 a frissítési láncot teszi megbízhatóbbá, és kijavítja a Dolby Vision-megőrzést, amely valódi eszközös próbán derült ki, hogy nem működött. Az API változatlan.

## Változások

- **A Dolby Vision-megőrzés mostantól valóban működik.** Valódi eszközökkel futtatott próba mutatta ki, hogy a `dynamic_hdr: dolby_vision` (és `auto`, ha a forrás DV) korábban soha nem adott DV-kimenetet: a kódoló az FFmpeg `libx265`-e, az `--dolby-vision-rpu` pedig az x265 parancssori program funkciója, a könyvtáré nem, ezért a kódolt videóban egyetlen RPU sem volt. A végső ellenőrzés ezt helyesen elkapta, és a job felülvizsgálatra került, így hibás kimenet nem készült. Mostantól a kódolás után a worker a hash-ellenőrzött RPU-t beszúrja a HEVC-folyamba (`dovi_tool inject-rpu`), és az MKV sávot az eredeti időbélyegekkel újraépíti (`mkvextract` + `mkvmerge`, ami a Dolby Vision konfigurációs rekordot is létrehozza). Az eredeti sáv képkockaidejét és színleírását átviszi (`mkvpropedit`, `--colour-*`), különben az `mkvmerge` 42 ms-os képkockaidőt számolna, és az FFmpeg elutasítaná a dekódolást. Az eredmény csak akkor váltja le a kódolást, ha a visszaolvasott RPU-k száma képkockára pontosan egyezik a referencia idővonalával. Valódi próba: 1440 forrás-RPU, 1440 RPU a kimeneten, 8-as profil, a job `COMPLETED`.
- **A telepítő felteszi a `dovi_tool`-t.** A hivatalos `quietvoid/dovi_tool` rögzített 2.3.4-es kiadását, SHA-256 ellenőrzéssel, az eszközkiadásba (`tools/current/bin`); az eszközfrissítő átviszi az új eszközkiadásba. Ha a letöltés nem sikerül (nincs hálózat, nem x86_64 a gép, eltér az ellenőrzőösszeg), a telepítés ettől nem hiúsul meg, csak a Dolby Vision-megőrzés marad elérhetetlen, és a `bdencode doctor` jelzi. A `hdr10plus_tool` továbbra sem része a telepítésnek: a Debian x265 HDR10+ támogatás nélkül készül.
- **A médiaeszköz-frissítő APT-hibája kijavítva.** A kézzel indítható `bdencode-daily-update` minden futása elbukott a várakozó `mkvtoolnix` biztonsági frissítésén (`E: Unable to fetch some archives`), és visszagörgetett; a rendszer ilyenkor sértetlen maradt, de a frissítés soha nem települt. Az ok: az apt a helyi `.deb` fájlt eldobta, mert ugyanaz a verzió volt a tároló jelöltje, és a tárolóból akarta letölteni, amit a `--no-download` tilt. Mostantól a tranzakció a hash-ellenőrzött csomagot a saját, root-only archívum-gyorsítótárába másolja az apt által várt fájlnévvel, és `név=verzió` formában telepíti. Az apt ezt is összeveti az aláírt csomagindexszel, a `DPkg::Pre-Install-Pkgs` guard pedig csak a manifestben szereplő hashű archívumot engedi át.
- **Aláírt kiadások (opcionális).** A `/etc/bdencode/release-update.toml` `require_signed_tags = true` beállításával a napi frissítő csak olyan `vX.Y.Z` taget telepít, amelyet a `signers_file` (alapból `/etc/bdencode/release-signers`) egyik kulcsa SSH-val aláírt. Aláíratlan, idegen kulccsal aláírt vagy könnyű tag `invalid_release` és azonnal blokkolt állapotot kap, a telepítő el sem indul. Hiányzó vagy üres bizalmi lista, illetve hiányzó `ssh-keygen` esetén a frissítő nem telepít, tehát az ellenőrzés nem kapcsol ki csendben. Alapértelmezés szerint kikapcsolt. A beállítás lépései a README 11.1. pontjában vannak.
- **Aláírt tag egy paranccsal.** `python tools/release.py tag vX.Y.Z` ellenőrzi a verziófájlokat és a jegyzetet, majd annotált, aláírt taget hoz létre a `HEAD`-en (nem pusholja). A kézi `install --tag vX.Y.Z` is követeli az aláírást, ha be van kapcsolva; aláírás előtti kiadás visszaállításához a `--allow-unsigned` kapcsoló kihagyja ezt (csak a kézi parancsnál).
- **A telepítő felteszi az `openssh-client` csomagot**, mert az aláírás-ellenőrzés az `ssh-keygen`-t használja.
- **UHD- és Dolby Vision-próba.** A `tools/e2e/synthetic_disc.py` új `--size` és `--scenes` kapcsolója 3840×2160-as szintetikus HDR10 „lemezzel” futtatja végig a teljes csővezetéket valódi eszközökkel (valódi próba: Main 10, a HDR10 metaadatok megmaradnak, PSNR 62,9 dB, színeltolódás kb. 0,01); a `--dolby-vision` generált 8.1-es Dolby Vision-forrást készít, és megköveteli, hogy a kimeneti RPU-k képkockára pontosan egyezzenek. Az `End-to-end` workflow kézi indításnál ezeket is bekéri.

## Frissítés

A 2.3.x rendszer a napi időzítővel magától frissít, ha a sor üres (README 11.1.). Az APT-javítás a kézi eszközfrissítőre csak a telepítés után érvényes: `sudo env BDENCODE_USER=<fiók> /usr/local/libexec/bdencode-daily-update`.

## Ellenőrzőlista

1. `GET /api/v1/capabilities` `backend_version`: `2.4.0`.
2. `cat /var/lib/bdencode/release-update/status.json`: `installed`.
3. `dovi_tool --version` a telepítés után `dovi_tool 2.3.4`-et ír (`~/encode/app/tools/current/bin`), és a `bdencode doctor` `dynamic_hdr` szakasza a Dolby Visiont elérhetőnek mutatja.
4. Ha az eszközfrissítőt kézzel futtatod, a `/var/lib/bdencode/apt-transactions/` legutóbbi tranzakciójának `state` fájlja `COMMITTED` (nem `ROLLED_BACK`), és `dpkg -l mkvtoolnix` az újabb verziót mutatja.

## További dokumentáció

- [Felhasználói és telepítési útmutató](../README.md)
- [Architektúra és biztonsági határok](ARCHITECTURE.md)
- [BDEncode 2.3 kiadási jegyzet](RELEASE_2_3.md)
- [BDEncode 2.2.1 kiadási jegyzet](RELEASE_2_2_1.md)
