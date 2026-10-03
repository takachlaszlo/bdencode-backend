# BDEncode 2.7.1 kiadási jegyzet

Két hiba javítása, mindkettő az első valódi UHD-előkészítésen (2.7.0) jött elő.

## Változások

- **A hangsávok nyelvfelismerése ismét működik.** A `faster-whisper` 1.2.1 a mintákat a PyAV-val olvasta volna be, de olyan paraméterrel (`metadata_errors`) hívja az `av.open()`-t, amelyet az újabb PyAV (a telepítésben 19.0.1) már nem ismer. Ezért minden mintánál „speech analysis failed” lett az eredmény, és minden job felülvizsgálatra került, amelyben nem kézzel megadott nyelvű hangsáv maradt („one or more retained tracks need a confirmed language before encoding”). A mintákat a BDEncode maga vágja ki fix formátumban (16 kHz, mono, 16 bites PCM WAV). Mostantól maga olvassa be őket, és tömbként adja át a modellnek, így a PyAV verziója nem számít. A valódi lemez 12 mintája mind franciának ismerve 0,94–0,99 biztonsággal.
- **A worker WSL alatt eléri a GPU-t.** A worker szolgáltatás homokozója (`PrivateDevices=true`) elzárta a `/dev/dxg` eszközt, ezért az NVDEC nem volt elérhető, és a teljes című crop-keresés CPU-n futott (UHD-n 49 perc a várt 19 helyett). A szolgáltatásfájl most csak ezt az egy eszközt engedi be (`BindPaths=-/dev/dxg`, `DeviceAllow=/dev/dxg rw`). A privát `/dev` és a többi védelem marad. Ahol az eszköz nem létezik, a szolgáltatás változatlanul indul.

## Felülvizsgálatra került job folytatása

A 2.7.1 telepítése után a „language” okból felülvizsgálatra került job a felületről vagy a `POST /api/v1/jobs/<id>/resume` hívással folytatható. A remux és a crop eredménye megmarad, csak a nyelvfelismerés fut le újra.

## Ellenőrzőlista

1. `GET /api/v1/capabilities` `backend_version`: `2.7.1`.
2. `systemctl show bdencode-worker -p DeviceAllow` tartalmazza a `/dev/dxg rw` sort.
3. A következő előkészítésnél a `logs/commands.jsonl`-ben a `cropdetect` parancsban ott van a `-hwaccel cuda`.

## További dokumentáció

- [BDEncode 2.7 kiadási jegyzet](RELEASE_2_7.md)
