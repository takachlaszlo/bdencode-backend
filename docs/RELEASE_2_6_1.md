# BDEncode 2.6.1 kiadási jegyzet

Egyetlen javítás: az összehasonlító lépés időkerete UHD-felbontásnál.

## Változások

- **Az összehasonlítás időkerete a felbontással arányosan nő.** Valódi 4K-s próbafutásnál az összehasonlítás képkocka-vizsgálata (24 átfedő ablak, mindegyik egy teljes GOP-nyi dekódolt előfutással) átlépte a parancsonkénti 300 másodperces keretet, és a job felülvizsgálatra került: „fast comparison exceeded its bounded command/time budget”. A keretek 1080p-hez készültek; egy UHD-kép négyszer annyi pixelt dekódol. Mostantól a parancsonkénti keret és a lépés 30 perces teljes kerete a forrás pixelszámával arányosan nő (1080p-ig 1×, UHD-nál 4×, felső korlát 4×), tehát UHD-nál 20 perc/parancs és 2 óra a lépésre. A keret továbbra is korlát, nem tipikus futásidő: az éles, hosszú UHD-kódolás végén a lépés ettől nem kerül felülvizsgálatra pusztán lassú dekódolás miatt. 1080p és kisebb felbontásnál nincs változás.

## Frissítés

A 2.6.0 rendszer a napi időzítővel magától frissít, ha a sor üres (README 11.1.).

## Ellenőrzőlista

1. `GET /api/v1/capabilities` `backend_version`: `2.6.1`.
2. Egy UHD-job összehasonlításánál a felülvizsgálati üzenet (ha mégis lejárna) „comparison exceeded its time budget (120 minutes)”.

## További dokumentáció

- [Felhasználói és telepítési útmutató](../README.md)
- [Architektúra és biztonsági határok](ARCHITECTURE.md)
- [BDEncode 2.6 kiadási jegyzet](RELEASE_2_6.md)
