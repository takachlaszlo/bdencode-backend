# Architecture

Az API és a worker külön systemd folyamat. Közös állapotuk SQLite WAL adatbázis. Két külön részleges egyedi index védi az egyetlen aktív scan sávot és az egyetlen aktív encode sávot, ezért versenyhelyzetben sem indulhat két scan vagy két encode. A két sáv egymás mellett futhat, és ugyanazon systemd CPU-kereten osztozik.

A React/Vite frontend nem külön szerverfolyamat: verziózott statikus release-ként kerül a `/var/www/bdencode/releases/<id>/encoder` könyvtárba. Nginx ugyanazon Basic Auth védelem alatt szolgálja ki a `/encoder/` SPA-t, és a `/encoder/api/` kéréseket a loopback FastAPI felé továbbítja. A `/var/www/bdencode/current` symlink cseréje atomikus, és az app/tool pointerekkel, nginx konfigurációval együtt az installer tartós rollback-snapshotjának része.

```text
QUEUED → SCANNING → AWAITING_SELECTION → READY → ENCODING → MUXING
                                                    ↓
COMPLETED ← UPLOADING ← COMPARISON ← QC ←───────────┘
                 ↘ UPLOAD_FAILED
valamennyi aktív szakaszból: NEEDS_REVIEW / FAILED / CANCELLED
```

`AWAITING_SELECTION` nem blokkolja a további lemezek scanjét, a `READY` pedig teljesen beállított, kódolásra váró munkát jelent. `NEEDS_REVIEW` és `UPLOAD_FAILED` megtartja a már elfoglalt encode sávot: egy későbbi job nem előzheti meg a még le nem zárt encode-ot.

## Video evidence

Két összehasonlítási réteg készül:

1. `REFERENCE_ALIGNED`: a forrásból BestSource-szal dekódolt, tényleges encode előtti crop/IVTC/deinterlace eredmény;
2. `ENCODE_DECODED`: a végleges, már muxolt MKV `v:0` sávjából dekódolt, azonos presentation frame.

A kodek-összehasonlítás e két réteg között történik. Az I/P/B kategória mindig a kész encode frame típusa. Progresszív forrásnál a source bitstream frame típusa is kötelezően egyezik. Időbeli szűrés után a reference tömörítetlen, ezért ott source bitstream-típus nem értelmezhető, de ugyanazon reference index és a valós `vspipe --info` FPS-ből számított PTS kötelező.

A frame-kiválasztás nem készít teljes fájlos `ffprobe -show_frames` indexet. A worker rövid, korlátozott időablakokat vizsgál, azokból választ időben elosztott képeket, és csak a kiválasztott indexeket dekódolja. Alapértelmezés: összesen 5 pár, a `comparison_pair_count` 3 és 5 közötti értékével. Minden comparison tartalmaz legalább egy I-, egy P- és egy B-párt; ha valamelyik kategória a bounded mintából nem igazolható, a comparison nem minősül teljesnek.

PNG-n kívül JPEG/WebP/resizing nem használható. A megosztott reference/encode PNG-k a teljes képet érintetlenül hagyó, külön fekete fejlécben hordozzák a kép szerepét, a nullától számozott presentation frame indexet és az I/P/B kategóriát; időbeli transzformáció esetén a source fejléc `MATCHED TO` megfogalmazása nem állít nem létező bitstream-képtípust. Minden fájl SHA-256 hash-t kap; ImgBB, Catbox vagy Freeimage után a közvetlen URL-ről letöltött byte-ok hashének is egyeznie kell. Automatikus módban a szolgáltató csak az első sikeres kép előtt váltható, kézi módban egyáltalán nem; utána az egész csomagra rögzül, ezért egy BBCode soha nem kever hostokat. A feltöltési POST és az ellenőrző GET nem követ redirectet, a közvetlen URL szolgáltatói allowlisthez kötött. Az SSIM és PSNR a kiválasztott lossless reference/encode képpárok felirat nélküli belső pixelmásolatán, páronként és összesítve készül, így a különböző fejlécszöveg nem módosítja a metrikát. A comparison nem futtat teljes filmes VMAF-, SSIM- vagy PSNR-menetet, így a költsége a rövid probe-ablakok és a legfeljebb öt képpár feldolgozására korlátozott. A teljes videó-comparison hard időkerete 300 másodperc; a worker ezt túllépve review állapotot kér ahelyett, hogy korlátlan elemzést folytatna. A korábbi `comparison_frames_per_type` mező csak konfigurációbetöltési kompatibilitásból marad meg, az új algoritmus nem használja.

A kész MKV külön hard gate-en igazolja az elvárt H.264/H.265 profilt, méretet, pixel-formátumot és a scanből átvett színmetaadatot. HDR10-nél az első dekódolt képkockák Mastering Display és MaxCLL/MaxFALL oldalsó adatai is pontosan egyeznek; Dolby Vision és dinamikus HDR10+ adat tiltott. Eltérő kézi színcímke explicit színkonverzió nélkül nem engedélyezett.

## Audio evidence

Minden választott sávhoz sample rate/count, start delay, duration, bit depth, channels/layout, EBU R128, peak/clipping, astats és phase mérés készül. A source és a végső MKV sávjának spektruma azonos FFmpeg scale/window/pixel formátummal külön PNG. Copy és FLAC esetén a kanonikus dekódolt `pcm_s32le` SHA-256, a topológia és legfeljebb egy audio-sample PTS-tolerancia kötelező gate.

AC-3/E-AC-3/DTS veszteséges átkódolásnál a PCM hash szándékosan nem fut és nem jelenik meg hibaként. Helyette az effektív cél-codec, bitráta, 48 kHz mintavétel, tervezett csatornaszám, kezdő PTS és legfeljebb egy codec-frame időtartam-eltérés a hard gate. A manifest `verification_mode` és `effective_target` mezőkkel rögzíti, melyik szabály érvényesült.

FLAC esetén a tömörítési szint 8, a dekódolt PCM-egyezés kötelező. TrueHD Atmos/DTS:X objektum-metaadat FLAC-ban nem tartható meg; ehhez a frontend Copy-t ajánl és figyelmeztet.

Az AC-3 preset 640 kb/s, az E-AC-3 1024 kb/s, a DTS core 1536 kb/s; mindegyik 48 kHz-es és legfeljebb 5.1 csatornás. A 6.1/7.1 forrás downmixe látható figyelmeztetés és naplózott effektív cél mellett történik. DTS-HD MA/HRA vagy DTS:X forrás `dts` céljánál a worker a beágyazott DTS core-t `dca_core` bitstream-szűrővel, újrakódolás nélkül emeli ki; sima DTS-t másol, TrueHD/egyéb forrást a `dca` encoderrel kódol.

A Blu-ray LPCM (`pcm_bluray`) nem muxolható változtatás nélküli bitstreamként Matroskába, ezért ennél a sávnál a backend elutasítja a Copy választást, és FLAC/AC-3/E-AC-3/DTS vagy omit döntést kér. A FLAC út továbbra is sample-pontos PCM-hash gate-en megy át.

## Artifact policy

Az MKV csatolmánya kizárólag a sanitizált `encode.log` lehet. Raw log, manifest, MediaInfo/MKVInfo, metrics, frame PNG, spektrum, képfeltöltő response és BBCode az encode mappa sidecar fájljai. Az analyzer külön jelzi, ha comparison jellegű csatolmány mégis az MKV-ba került.

Minden job manifest rögzíti a parancsargumentumokat, bináris verziókat és SHA-256 hash-eket, OS/CPU adatot, source fingerprintet, effektív profilt, filter script hashét és artifact hasheket.

## Updates

A napi timer közös deployment lockkal előbb lezárja az új job-claim lehetőségét, és csak bizonyítottan idle queue esetén állítja le az API/worker szolgáltatást. Az APT-indexelés külön, Debian main/bookworm/bookworm-updates/bookworm-security forráslistát használ; proposed-updates, backports és third-party repository nem kerülhet a médiacsomag-tervbe. A kezelt source-csomagokat globális negatív pin védi, ezért unattended-upgrades, általános `apt upgrade` vagy más rutin APT-frissítés sem válthatja le a verziójukat; csak a tranzakció izolált preferences-scope-ja kerüli meg ezt a pint. A root által szándékosan kért eltávolítás nem tartozik e garancia körébe.

Módosítás előtt tartós, root-only tranzakció készül `/var/lib/bdencode/apt-transactions` alatt. A szimulált terv minden csomagjának új `.deb` fájlja előre letöltődik, a telepített régi példány pedig `dpkg-repack` vaultba kerül. Package/version/architecture/source/SHA-256 eltérés, új telepítés, eltávolítás, held vagy Essential/Protected csomag fail-closed eredmény. Az APT v3 pre-install hook közvetlenül a `dpkg` előtt ismét összeveti a valós action listát a manifesttel, és `--no-download --no-remove` mellett csak a rögzített helyi fájlokat engedi át.

Az APT állapotgép `PREPARED → APPLYING → APPLIED → VALIDATING → COMMITTED`; minden nem végleges, már mutáló állapot `ROLLING_BACK → ROLLED_BACK` ágon áll vissza. Minden vault-fájl, állapot és active marker fájl+könyvtár `fsync` után, atomi cserével kerül lemezre. Egy második runtime journal még a módosítások előtt rögzíti a régi és a candidate tool-release-t, az API/worker, valamint az APT-timerek előző állapotát. Emiatt a csomagcommit és a tool-symlink aktiválása közötti megszakítás sem hozhat létre hibrid runtime-ot.

SIGKILL esetén az updater `ExecStopPost`, reboot után pedig a szolgáltatások és az `apt-daily` előtt futó recovery unit veszi fel a deployment lockot. Runtime/APT journal esetén előbb leállítja az APT-timereket és kivárja a már futó APT/dpkg oneshot természetes végét; APT-only crash esetén még ez előtt tartósan szintetizálja a hiányzó runtime pre-state-et. Ezután leállítja az esetleg futó runtime-ot, visszaállítja a symlinket és az összes régi `.deb` fájlt, ellenőrzi a pontos verziókat, az APT/dpkg konzisztenciát és a doctort, majd csak sikeres helyreállítás után engedi az API/worker indulását. Sikertelen rollback `RECOVERY_REQUIRED` állapotban blokkolva hagyja őket.

Az `apt-daily.service` és az `apt-daily-upgrade.service` külön systemd dependency drop-int kap: mindkettő csak sikeres BDEncode recovery után indulhat. Ezért egy félbemaradt `dpkg`-helyreállítás alatt az általános automatikus APT sem módosíthatja tovább a csomagállapotot.

A külön verziózott VapourSynth tool-release és a benne fordított natív libbluray scanner a csomagtranzakcióval együtt validálódik. Plugin/VMAF/valós x264+x265 codec-smoke/doctor teszt előzi meg az atomi symlink-aktiválást. A commit csak API healthcheck és a worker DB-/lock-inicializálása után kiküldött systemd `READY=1` jelzés után történik; hiba esetén előbb a régi symlink, majd a régi APT stack áll vissza, és azon is lefut a doctor. Folyamatban lévő job alatt nincs megszakítás vagy hot swap.

Az installer a saját mutációi előtt fix célpontlistás, tartós snapshotot publikál `/var/lib/bdencode/install-transactions` alatt. Ez lefedi az app/tool pointereket, a változó systemd unitokat, a napi updater scriptet, az APT source/pin/drop-in fájlokat, a credential drop-int, a configot és az nginx route-ot; külön megőrzi mindkét natív APT-timer aktív és engedélyezett állapotát is. Az `apt-get` előtt a timerek leállnak, a már futó APT/dpkg oneshot természetesen kifut, és a media pin már a csomagművelet előtt aktív. Az installer minden `apt-get` gyermekét külön `/run/lock/bdencode-installer-apt.lock` alatt futtatja. Recoverykor előbb ez, majd a natív frontend/backend/cache/list lockok szabadulását kell kivárni; félbemaradt konfiguráció javítási kísérlete után a runtime csak üres `dpkg --audit` és sikeres `apt-get check` mellett indulhat. A recovery helperk, a stabil API/worker recovery drop-inek és az install-watchdog a rollbacken kívül maradnak. Az `OBSERVING` fázisban csak az API és a rögzített timerek állhatnak le a queue/APT ellenőrzéséhez, ezért busy queue vagy ekkori crash nem szakítja meg a workert; a teljes célpont-rollback csak a tartós `PREPARED` döntés után engedélyezett. A fájlok visszaállítása vagy a validált candidate commitja után külön `services-pending` marker marad addig, amíg a kívánt service-startok sorba nem kerültek. Normál hiba ugyanazt az idempotens állapotgépet futtatja, SIGKILL után pedig a négy install/runtime/APT markert figyelő `bdencode-install-recovery.path` azonnal, rebootkor a recovery gate folytatja.

A kézi installer futó `SCANNING`, `UPLOADING`, számítási szakasz vagy
`NEEDS_REVIEW` alatt nem aktivál új kiadást. A `QUEUED`, `AWAITING_SELECTION` és
még el nem indított `READY` munkák tartósan telepítésbiztosak; a deployment lock
megakadályozza, hogy az ellenőrzés és az aktiválás között valamelyik worker-sáv
új munkát foglaljon. `UPLOAD_FAILED` után az új worker ugyanabból az atomi
checkpointból csak a feltöltési szakaszt folytathatja.
