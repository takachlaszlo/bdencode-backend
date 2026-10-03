# BDEncode 2.8 kiadási jegyzet

Az előkészítés utolsó nagy tétele, a VapourSynth-forrás indexelése 45 percről kb. 90 másodpercre rövidül.

## Háttér

A 2.7-es előkészítés az első valódi UHD-jobon így alakult:

| Lépés | Idő |
|---|---|
| Lemezbeolvasás | 1,8 perc |
| Helyi másolat | 3 perc |
| Remux | 5 perc |
| Crop | 19 perc (GPU, 2.7.1) |
| BestSource-index | 45 perc |

A BestSource az indexhez a 60 GB-os referencia minden képkockáját dekódolja és hasheli.

## Változások

- **L-SMASH az alapértelmezett VapourSynth-forrás.** Az `LWLibavSource` a konténer csomagjaiból indexel. A valódi UHD-referencián (168 744 képkocka) ez 90 mp a BestSource kb. 45 perce helyett. A két forrás ugyanarra a képkockaszámra bitre azonos képet ad: a teljes filmen 347 képkockát vetettem össze, köztük 47 véletlenszerű ugrást, egyezik a képkockaszám, és egyeznek a szín-metaadatok is. Az L-SMASH a pontos 1001/24000 képkocka-időtartamot adja (a BestSource az MKV ezredmásodperces időbélyegéből 21/500-at), a képsebesség mindkettőnél 24000/1001. A rögzített csomag `vapoursynth-lsmas==1310.0.0.0`, amelyet a telepítő és a napi eszközfrissítő is telepít, a `doctor` pedig ellenőriz. Beállítás: `source_filter` (`lsmas` | `bestsource`). BestSource-szal a korábbi jobok ellenőrzőpontjai érvényesek maradnak.
- **Az index a crop-kereséssel egy időben készül.** A GPU-s crop-keresés indulásakor egy külön `vspipe --info` felépíti a forrás indexét ugyanazzal a forráshívással, mint a referencia-szkript. A referencia-szkript ezt az indexet olvassa: valódi mintán a cropolt szkript 0,2 mp alatt nyílt meg vele. BestSource-szal ez az átfedés a 45 percből kb. 19-et elrejt, L-SMASH-sel az index már a crop első két percében elkészül.

Kipróbált, de nem működő lehetőség: a BestSource GPU-s dekódolása (`hwdevice="cuda"`). A csomagba épített FFmpeg nem tud NVDEC-et, ezért a sebesség változatlan, kb. 50 fps.

## Várható előkészítés egy új UHD-lemeznél

Lemezbeolvasás kb. 2 perc + helyi másolat kb. 3 perc + remux kb. 5 perc + crop és index kb. 19 perc, **összesen kb. 30 perc** (a 2.6-ban kb. 3 óra 35 perc). Az integritás-ellenőrzés a kódolás mellett fut.

## Frissítés

A napi időzítő magától telepíti, ha a sor üres (README 11.1.). Az új eszközcsomagot a telepítő hozza.

## Ellenőrzőlista

1. `GET /api/v1/capabilities` `backend_version`: `2.8.0`.
2. `bdencode doctor`: a VapourSynth-pluginok között `lsmas: true`.
3. Új job előkészítésénél a `work/reference.vpy` az `LWLibavSource`-t hívja, a `logs/source-index.log` „Creating lwi index file 100%”-kal zárul.

## További dokumentáció

- [Felhasználói és telepítési útmutató](../README.md) (14.3. Az előkészítés gyorsítása)
- [BDEncode 2.7.1 kiadási jegyzet](RELEASE_2_7_1.md)
