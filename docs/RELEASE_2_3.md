# BDEncode 2.3 kiadási jegyzet

A 2.3 a minőségellenőrzést és a kiadási folyamatot erősíti; új képesség nincs, a 2.2 funkciói és az API változatlanok.

## Változások

- **A minőségkapu a rendszeres színeltolódást is megfogja.** A comparison minden mintapárnál a PSNR/SSIM mellett a síkonkénti előjeles átlageltérést is rögzíti (`plane_bias_8bit`, encode mínusz reference, 8 bites kódértékben, ugyanabból a natív Y4M mintából). Ha a minták átlaga bármelyik síkon meghaladja az 1,5 kódértéket, a job felülvizsgálatra kerül. Az indok: a 2.1.0 hibás színmátrix-konverziója átlagos képnél a PSNR-küszöböt nem feltétlenül sértette, az átlageltolódást viszont megmutatta volna. Valódi eszközös futásban az eltérés 0,02 kódérték körüli volt, a limit tehát bőven távol van a normál működéstől. A mérés az összesítésben `plane_bias_8bit_mean` néven is megjelenik (`video-metrics.json`, manifest).
- **A Windows-telepítő alapból a legfrissebb kiadást telepíti.** A `windows-install.cmd` / `windows.ps1` a legmagasabb `vX.Y.Z` taget klónozza (ahogy a napi frissítő), nem a `main` aktuális állapotát; `-Branch` megadásakor az az ág vagy tag kerül telepítésre. Ha egyetlen kiadási tag sem érhető el, a `main` ágra esik vissza.
- **Kiadás-ellenőrzés a CI-ban.** Egy `v*` tag pusholásakor a `Release` workflow (`tools/release.py check`) ellenőrzi, hogy a tag, a `pyproject.toml`, a `bdencode.__version__`, a `frontend/package.json` verziója és a `docs/RELEASE_*.md` jegyzet egyezik, majd létrehozza a GitHub Release-t, ha még nincs. Így nem kerülhet ki olyan tag, amelyet a frissítő `invalid_release`-ként utasítana el.
- **A Linux CI valódi médiaeszközökkel fut.** Az `ffmpeg`, `mkvtoolnix` és `mediainfo` telepítése után a korábban kihagyott valódi-eszközös tesztek (színkonverzió a kódolócsőben, 10 bites Y4M, mux) is lefutnak.
- **Végponttól végpontig tartó próba a repóban.** A `tools/e2e/synthetic_disc.py` szintetikus HDR10 „lemezen” futtatja végig a csővezetéket valódi eszközökkel (README 15.4); `BDENCODE_E2E=1 python -m pytest tests/test_e2e_synthetic.py` ugyanezt csinálja.
- **Frissítési állapot a felületen.** A Rendszer oldal új kártyája (és a `GET /api/v1/system/release-update`) a napi kiadáskeresés utolsó eredményét mutatja: telepített és legújabb kiadás, utolsó ellenőrzés és telepítés, üzenet.
- **Értesítés.** A `/etc/bdencode/release-update.toml` opcionális `notify_url` kulcsa (csak `https://`, jelszó nélkül) egy JSON POST-ot küld, ha egy frissítés települt, megbukott, leállt vagy érvénytelen kiadást talált; ugyanarról az állapotról nem ismétel, a hibás értesítés pedig soha nem akasztja meg a frissítést.
- **Visszaállás egy parancsal.** `sudo /usr/local/libexec/bdencode-release-update install --tag vX.Y.Z` pontosan azt a kiadást telepíti (régebbit is) a megszokott védelmekkel: üres sor, jelszó nélküli sudo, tag és verzió egyezése, a kiadás saját tranzakciós telepítője.
- **Médiacsomag-frissítések jelzése.** A napi futás csak jelzi (`media_updates`), ha az ffmpeg, x264, x265, mkvtoolnix, mediainfo vagy libbluray Debian-frissítése várakozik; telepíteni továbbra sem telepít.
- **Stabilabb teszt.** A párhuzamos adatbázis-migrációt vizsgáló teszt 16 szálat használ 64 helyett, mert lassú lemezen kimerítette a korlátozott újrapróbálási keretet, és ez egy felügyelet nélküli telepítést is megállíthatott volna.

## Frissítés

A 2.2.x rendszer a napi időzítővel magától frissít, ha a sor üres (README 11.1.). Kézi telepítésnél a README 11.2. szerint járj el.

## Ellenőrzőlista

1. `GET /api/v1/capabilities` `backend_version`: `2.3.0`.
2. `cat /var/lib/bdencode/release-update/status.json`: `installed`, és a `release-update.log` végén a telepítés naplója.
3. Egy új job comparison-jának `video-metrics.json` fájljában az `aggregate.plane_bias_8bit_mean` értékei 1,5 alatt vannak.

## További dokumentáció

- [Felhasználói és telepítési útmutató](../README.md)
- [Architektúra és biztonsági határok](ARCHITECTURE.md)
- [BDEncode 2.2.1 kiadási jegyzet](RELEASE_2_2_1.md)
- [BDEncode 2.2 kiadási jegyzet](RELEASE_2_2.md)
