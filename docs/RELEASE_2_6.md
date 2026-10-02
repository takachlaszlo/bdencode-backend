# BDEncode 2.6 kiadási jegyzet

A 2.6 a kétrétegű (7-es profilú) Dolby Vision UHD Blu-rayeket is megtartja. Az API változatlan.

## Változások

- **Kétrétegű Dolby Vision UHD-lemezek felismerése és megtartása.** A valódi lemezes próba kimutatta, hogy a legtöbb UHD Blu-ray Dolby Vision-je 7-es profilú: az RPU nem az alapréteg 4K-s folyamában van, hanem egy másodlagos, 1080p-s HEVC-folyamban (kiegészítő réteg). Sem az FFmpeg, sem a libbluray nem jelezte Dolby Visionnek, ezért az `auto` mód csendben eldobta, és a kinyerés az alaprétegből akart RPU-t olvasni, ahol nincs. Mostantól:
  - a **scan** megvizsgálja a fő lejátszási listák másodlagos HEVC-folyamait (UHD-lemezen, `dovi_tool` esetén): lemásolja az első 120 képkockát, és a `dovi_tool` kiolvassa az RPU-t. 7-es profil esetén az alapréteget Dolby Visionnek jelöli, rögzíti a kiegészítő réteg folyamát és típusát (MEL vagy FEL). A felismerés puha: ha a probe elbukik, a scan változatlan marad;
  - a **terv** (`dynamic-hdr.json`) tartalmazza a kiegészítő réteg sorszámát (`el_video_ordinal`), és a kinyerés ebből a folyamból dolgozik (`dovi_tool -m 2 extract-rpu`, vágásnál `-c`), nem az alaprétegből;
  - FEL esetén a terv jelzi, hogy a teljes kiegészítő réteg a 8.1-es profilban nem fér el, és elmarad; MEL-nél lényegében nincs veszteség;
  - a megtartás többi része változatlan (injektálás a kész kódolásba, sáv újraépítése, visszaolvasás képkockára pontosan, a QC kapu a konfigurációs rekordra).
  Valódi lemezszakaszon ellenőrizve: a 7-es profilból 8.1-es RPU lett, a kimeneten ott a Dolby Vision konfigurációs rekord (8-as profil, HDR10-kompatibilis), és a visszaolvasott RPU-szám egyezik a képkockákkal.
- **A felismerés a scan idején fut.** A korábban beolvasott lemezeket újra kell olvastatni, hogy a Dolby Vision-jelölést megkapják.
- **Valódi mesterrel is futtatható a végponttól végpontig tartó próba.** A `tools/e2e/synthetic_disc.py --real-master <mkv>` egy valódi lemezszakaszt (4K alapréteg, második videófolyamként a Dolby Vision kiegészítő réteg) használ a szintetikus helyett, a worker saját scan-probájával, és megköveteli, hogy a kimenet 8-as profilú, képkockánként egy RPU-t tartalmazzon (`--crop-bars N` a fekete sávok vágásához).

## Frissítés

A 2.5.x rendszer a napi időzítővel magától frissít, ha a sor üres (README 11.1.).

## Ellenőrzőlista

1. `GET /api/v1/capabilities` `backend_version`: `2.6.0`.
2. Egy 7-es profilú Dolby Vision UHD-lemez újraolvasása után a scan eredményében (`scan.json`) az alapréteg `dolby_vision: true`, `dolby_vision_profile: 7`, és a kiegészítő réteg azonosítója megjelenik.
3. A job `dynamic-hdr.json` fájljában `plan.source_profile: 7`, `plan.el_video_ordinal` a kiegészítő réteg sorszáma, a kész MKV-ban az ffprobe `DOVI configuration record` (8-as profil) adatot mutat.

## További dokumentáció

- [Felhasználói és telepítési útmutató](../README.md)
- [Architektúra és biztonsági határok](ARCHITECTURE.md)
- [BDEncode 2.5 kiadási jegyzet](RELEASE_2_5.md)
- [BDEncode 2.4 kiadási jegyzet](RELEASE_2_4.md)
