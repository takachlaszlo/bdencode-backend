# BDEncode 2.7 kiadási jegyzet

Gyorsabb lemezbeolvasás és előkészítés. Egy UHD-lemeznél (La Femme Nikita, 117 perc, 63,5 GB, Windows-meghajtón, WSL alatt) a beolvasás és az előkészítés együtt 2 óra 50 percig tartott, mielőtt a kódolás elindulhatott. Az idő a lemez lassú olvasásából és ugyanannak a 60 GB-os referenciának a kétszeri teljes dekódolásából adódott.

## Változások

- **Párhuzamos playlist-vizsgálat.** A beolvasás a playlisteket nyolcasával vizsgálja egymás után helyett. A 127 playlistes UHD-lemez beolvasása közvetlenül a Windows-meghajtóról 12 percről 74 másodpercre rövidült. Az eredmény (a playlistek sorrendje és a lemez ujjlenyomata) azonos a soros vizsgálatéval.
- **A kiválasztott cím helyi másolata a remux előtt.** WSL alatt a Windows-meghajtót a 9p-híd éri el, amely a libbluray 6 KB-os olvasásait kb. 11–16 MB/s-mal szolgálja ki: a 60 GB-os referencia-remux egy óráig tartott. Lassú csatolásnál (9p, drvfs, SMB/CIFS, NFS, sshfs) a worker most a kiválasztott cím fájljait (az összes playlist- és clipinfó-fájlt, valamint a címben szereplő streamfájlokat) párhuzamos, nagy blokkos olvasással a helyi `cache/disc-stage` mappába másolja. Ez kb. 5 perc, 216 MB/s. A remux a helyi másolatból kb. 5 perc. A másolat bájtazonos (méret és módosítási idő ellenőrizve), a remux után törlődik, és ha a forrás közben megváltozik, újra elkészül. Ha nincs elég hely (a cím mérete + a referencia + 10 GB), a lemez a helyén olvasódik, és erről `worker.source-staging-skipped` esemény kerül a jobhoz. A másolás szüneteltetéskor és megszakításkor leáll. Beállítás: `source_staging` (`auto` | `always` | `never`).
- **Az integritás-ellenőrzés a kódolással párhuzamosan fut.** A forrás teljes, szigorú dekódolása (`-xerror -err_detect explode`) UHD-n kb. egy óra (mérve: 58,9 perc). Eddig a kódolás előtt futott, most mellette, alacsonyabb CPU-prioritással (`nice -n 10`, 16 dekódoló szál). Ha sérülést talál, leállítja a kódolást, és a job felülvizsgálatra kerül. A muxolás csak sikeres ellenőrzés után indul. A remux naplóját az előkészítés továbbra is átnézi, mert az azonnali. Egy félbeszakított és folytatott kódolásnál az ellenőrzés újraindul, ha még nem fejeződött be.
- **A crop-keresés a GPU-n.** A teljes című crop-keresés (minden képkocka, a legkisebb biztonságos crop a teljes filmre) NVIDIA GPU-n NVDEC-kel dekódol: 148 fps a CPU kb. 50 fps-e helyett, UHD-filmre kb. 19 perc a 48 helyett. A GPU által dekódolt képeken a crop-eredmény azonos a CPU-éval. Hiba esetén CPU-n ismétel. Beállítás: `crop_hwaccel` (`auto` | `cuda` | `none`).

Kipróbált, de elvetett megoldások:

- A GPU-s crop-keresés és a CPU-s integritás-ellenőrzés egyidejű futtatása az előkészítésben: a képkockák visszamásolása négy CPU-magot foglalt, és az integritás-dekódolás 53-ról 44 fps-re lassult.
- A crop-keresés az integritás-dekódolásban: gyakorlatilag ingyenes volt, de az így kapott közös menet is 59 percig tartott volna a kódolás előtt.

## Mért és várható idők (UHD, 117 perc)

| Lépés | 2.6 | 2.7 |
|---|---|---|
| Lemezbeolvasás | 12 perc | 1,2 perc |
| Helyi másolat | – | ~5 perc |
| Referencia-remux | 60 perc | ~5 perc |
| Crop-keresés | 48 perc | ~19 perc (GPU) |
| Integritás-ellenőrzés | 48 perc | a kódolás mellett |
| **A kódolás indulásáig** | **~2 óra 50 perc** | **~30 perc** |

## Frissítés

A 2.6.x rendszer a napi időzítővel magától frissít, ha a sor üres (README 11.1.). Az új beállítások alapértéke `auto`, konfigurációs változtatás nem kell.

## Ellenőrzőlista

1. `GET /api/v1/capabilities` `backend_version`: `2.7.0`.
2. Windows-meghajtón lévő lemez előkészítésénél a job eseményei között ott van a `worker.source-staged` („title files copied to local disk …”), a remux után pedig a `cache/disc-stage` mappa üres.
3. A kódolás alatt a `ps` két folyamatot mutat: az x265-öt és egy `nice -n 10 ffmpeg … -xerror` dekódolást. Az utóbbi befejezése után a `stages/source-video-integrity.json` jelölő megjelenik.

## További dokumentáció

- [Felhasználói és telepítési útmutató](../README.md) (14.3. Az előkészítés gyorsítása)
- [BDEncode 2.6.1 kiadási jegyzet](RELEASE_2_6_1.md)
