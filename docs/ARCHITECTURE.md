# Backend architecture

Az API és a worker külön systemd folyamat. Közös állapotuk SQLite WAL adatbázis, az egyetlen aktív pipeline-t részleges egyedi index is védi, ezért két worker sem tud egyszerre jobot megszerezni.

```text
QUEUED → SCANNING → AWAITING_SELECTION → READY → ENCODING → MUXING
                                                    ↓
COMPLETED ← UPLOADING ← COMPARISON ← QC ←───────────┘
                 ↘ UPLOAD_FAILED
valamennyi aktív szakaszból: NEEDS_REVIEW / FAILED / CANCELLED
```

`AWAITING_SELECTION`, `NEEDS_REVIEW` és `UPLOAD_FAILED` szándékosan blokkoló állapot. Egy későbbi job nem előzheti meg a még le nem zárt encode-ot.

## Video evidence

Két összehasonlítási réteg készül:

1. `REFERENCE_ALIGNED`: a forrásból BestSource-szal dekódolt, tényleges encode előtti crop/IVTC/deinterlace eredmény;
2. `ENCODE_DECODED`: a végleges, már muxolt MKV `v:0` sávjából dekódolt, azonos presentation frame.

A kodek-összehasonlítás e két réteg között történik. Az I/P/B kategória mindig a kész encode frame típusa. Progresszív forrásnál a source bitstream frame típusa is kötelezően egyezik; opcionális eltérés csak explicit beállítással engedhető. Időbeli szűrés után a reference tömörítetlen, ezért ott source bitstream-típus nem értelmezhető, de ugyanazon reference index és a valós `vspipe --info` FPS-ből számított PTS kötelező. A teljes reference/encode frame-szám és PTS-sorozat is gate.

Alapértelmezés: 4 I + 4 P + 4 B pár. Ha valamelyik kategória hiányzik, a comparison nem minősül teljesnek. PNG-n kívül JPEG/WebP/resizing nem használható. Minden fájl SHA-256 hash-t kap; ImgBB után az eredeti URL-ről letöltött byte-ok hashének is egyeznie kell. A teljes-videós mérés az official libvmaf CLI-t használja privát POSIX FIFO-kon, PSNR/float-SSIM/float-MS-SSIM feature-ökkel; capability-hiánynál külön FFmpeg SSIM és PSNR stats sidecar készül.

A kész MKV külön hard gate-en igazolja az elvárt H.264/H.265 profilt, méretet, pixel-formátumot és a scanből átvett színmetaadatot. HDR10-nél az első dekódolt képkockák Mastering Display és MaxCLL/MaxFALL oldalsó adatai is pontosan egyeznek; Dolby Vision és dinamikus HDR10+ adat tiltott. Eltérő kézi színcímke explicit színkonverzió nélkül nem engedélyezett.

## Audio evidence

Minden választott sávhoz sample rate/count, start delay, duration, bit depth, channels/layout, kanonikus dekódolt `pcm_s32le` SHA-256, EBU R128, peak/clipping, astats és phase mérés készül. A source és a végső MKV sávjának spektruma azonos FFmpeg scale/window/pixel formátummal külön PNG. A PCM, topológia és legfeljebb egy audio-sample PTS-tolerancia minden megtartott hangsávnál gate.

FLAC esetén a tömörítési szint 8, a dekódolt PCM-egyezés kötelező. TrueHD Atmos/DTS:X objektum-metaadat FLAC-ban nem tartható meg; ehhez a frontend Copy-t ajánl és figyelmeztet.

A Blu-ray LPCM (`pcm_bluray`) nem muxolható változtatás nélküli bitstreamként Matroskába, ezért ennél a sávnál a backend elutasítja a Copy választást, és FLAC vagy omit döntést kér. A FLAC út továbbra is sample-pontos PCM-hash gate-en megy át.

## Artifact policy

Az MKV csatolmánya kizárólag a sanitizált `encode.log` lehet. Raw log, manifest, MediaInfo/MKVInfo, metrics, frame PNG, spektrum, ImgBB response és BBCode az encode mappa sidecar fájljai. Az analyzer külön jelzi, ha comparison jellegű csatolmány mégis az MKV-ba került.

Minden job manifest rögzíti a parancsargumentumokat, bináris verziókat és SHA-256 hash-eket, OS/CPU adatot, source fingerprintet, effektív profilt, filter script hashét és artifact hasheket.

## Updates

A napi timer közös deployment lockkal előbb lezárja az új job-claim lehetőségét, és csak bizonyítottan idle queue esetén állítja le az API/worker szolgáltatást. Az apt allowlist és a külön verziózott VapourSynth tool-release frissül; a candidate saját konfigurációjával futó plugin/VMAF/doctor smoke test megelőzi az atomi symlink-aktiválást. Változatlan Python toolchainnél a candidate törlődik, változásnál az aktuális és két korábbi release marad meg a külön védett VMAF-tulajdonos mellett. Hiba esetén a korábbi symlink áll vissza. Folyamatban lévő job alatt nincs megszakítás vagy hot swap.
