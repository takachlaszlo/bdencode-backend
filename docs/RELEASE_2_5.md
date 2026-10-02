# BDEncode 2.5 kiadási jegyzet

A 2.5 megoldja a HDR10+ megtartást, amely a Debian x265-tel eddig nem volt elérhető. Az API változatlan.

## Változások

- **A HDR10+ megtartás mostantól működik, egyedi x265 nélkül.** A Debian x265 HDR10+ támogatás nélkül készül, és az FFmpeg libx265-e a `dhdr10-info` paramétert csendben eldobja, ezért a `hdr10plus` mód és az `auto` mód HDR10+ forrásnál eddig nem tudott kimenetet adni. Mostantól a Dolby Visionnél 2.4.0-ban bevezetett eljárás érvényes rá is: a kódolás a megszokott módon készül, utána a worker a hash-ellenőrzött metaadatot a kész HEVC-folyamba szúrja (`hdr10plus_tool inject`), az MKV sávot az eredeti időbélyegekkel, képkockaidővel és színleírással újraépíti, az újraépített sávból pedig visszaolvassa a metaadatot (`hdr10plus_tool extract`), és csak akkor fogadja el, ha a képkockák száma pontosan egyezik a referencia idővonalával. A QC kapu továbbra is bizonyítja a kész MKV-n az `SMPTE2094-40` side data meglétét. Az x265 `dhdr10-opt` blokkszintű optimalizálása így nem érvényesül, ez csekély minőségi finomítás. Valódi próba generált HDR10+ forráson: 1440 képkocka a forrásban, 1440 a kimeneten, a job `COMPLETED`.
- **Az `auto` mód viselkedése megváltozik HDR10+ forrásnál.** Mivel a HDR10+ elérhető, az `auto` mód ilyen forrásnál a HDR10+ metaadatot megtartja (korábban a hiányzó x265-támogatás miatt csendben eldobta). Ha ezt nem szeretnéd, használd a `discard` módot.
- **A telepítő felteszi a `hdr10plus_tool`-t.** A hivatalos `quietvoid/hdr10plus_tool` rögzített 1.7.2-es kiadását, SHA-256 ellenőrzéssel, az eszközkiadásba (`tools/current/bin`), ugyanúgy, ahogy a `dovi_tool`-t (egy közös, hibatűrő telepítőfüggvény). Ha a letöltés nem sikerül, a telepítés ettől nem hiúsul meg, csak a megtartás marad elérhetetlen, és a `bdencode doctor` jelzi. Az eszközfrissítő mindkét eszközt átviszi az új eszközkiadásba.
- **A `bdencode doctor` és az API HDR10+-nál csak az eszközt kéri.** A `dynamic_hdr.hdr10plus.available` igaz, ha a `hdr10plus_tool` megvan; az `x265_supported` mező csak tájékoztató. A Dolby Visionnél továbbra is kell az x265 profiljelzése.
- **Éjszakai próba minden megtartási úton.** Az `End-to-end` workflow éjszaka három változatot futtat valódi eszközökkel (sima HDR10, Dolby Vision, HDR10+); kézi indításnál a `dolby_vision` és a `hdr10plus` kapcsoló választ. A `tools/e2e/synthetic_disc.py --hdr10plus` generált HDR10+ forrást készít, és megköveteli, hogy a kimeneti metaadat képkockára pontosan egyezzen.

## Frissítés

A 2.4.x rendszer a napi időzítővel magától frissít, ha a sor üres (README 11.1.). A frissítés a `hdr10plus_tool`-t is feltelepíti.

## Ellenőrzőlista

1. `GET /api/v1/capabilities` `backend_version`: `2.5.0`.
2. `hdr10plus_tool --version` a `~/encode/app/tools/current/bin` mappában `hdr10plus_tool 1.7.2`-t ír, és a `bdencode doctor` `dynamic_hdr` szakasza a HDR10+-t elérhetőnek mutatja.
3. Egy HDR10+ forrású jobnál (`dynamic_hdr`: `hdr10plus`) a kész MKV-ban az ffprobe `HDR Dynamic Metadata SMPTE2094-40 (HDR10+)` képkocka-adatot mutat.

## További dokumentáció

- [Felhasználói és telepítési útmutató](../README.md)
- [Architektúra és biztonsági határok](ARCHITECTURE.md)
- [BDEncode 2.4 kiadási jegyzet](RELEASE_2_4.md)
- [BDEncode 2.3 kiadási jegyzet](RELEASE_2_3.md)
