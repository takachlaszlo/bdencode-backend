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

A napi timer közös deployment lockkal előbb lezárja az új job-claim lehetőségét, és csak bizonyítottan idle queue esetén állítja le az API/worker szolgáltatást. Az APT-indexelés külön, Debian main/bookworm/bookworm-updates/bookworm-security forráslistát használ; proposed-updates, backports és third-party repository nem kerülhet a médiacsomag-tervbe. A kezelt source-csomagokat globális negatív pin védi, ezért unattended-upgrades, általános `apt upgrade` vagy más rutin APT-frissítés sem válthatja le a verziójukat; csak a tranzakció izolált preferences-scope-ja kerüli meg ezt a pint. A root által szándékosan kért eltávolítás nem tartozik e garancia körébe.

Módosítás előtt tartós, root-only tranzakció készül `/var/lib/bdencode/apt-transactions` alatt. A szimulált terv minden csomagjának új `.deb` fájlja előre letöltődik, a telepített régi példány pedig `dpkg-repack` vaultba kerül. Package/version/architecture/source/SHA-256 eltérés, új telepítés, eltávolítás, held vagy Essential/Protected csomag fail-closed eredmény. Az APT v3 pre-install hook közvetlenül a `dpkg` előtt ismét összeveti a valós action listát a manifesttel, és `--no-download --no-remove` mellett csak a rögzített helyi fájlokat engedi át.

Az APT állapotgép `PREPARED → APPLYING → APPLIED → VALIDATING → COMMITTED`; minden nem végleges, már mutáló állapot `ROLLING_BACK → ROLLED_BACK` ágon áll vissza. Minden vault-fájl, állapot és active marker fájl+könyvtár `fsync` után, atomi cserével kerül lemezre. Egy második runtime journal még a módosítások előtt rögzíti a régi és a candidate tool-release-t, az API/worker, valamint az APT-timerek előző állapotát. Emiatt a csomagcommit és a tool-symlink aktiválása közötti megszakítás sem hozhat létre hibrid runtime-ot.

SIGKILL esetén az updater `ExecStopPost`, reboot után pedig a szolgáltatások és az `apt-daily` előtt futó recovery unit veszi fel a deployment lockot. Runtime/APT journal esetén előbb leállítja az APT-timereket és kivárja a már futó APT/dpkg oneshot természetes végét; APT-only crash esetén még ez előtt tartósan szintetizálja a hiányzó runtime pre-state-et. Ezután leállítja az esetleg futó runtime-ot, visszaállítja a symlinket és az összes régi `.deb` fájlt, ellenőrzi a pontos verziókat, az APT/dpkg konzisztenciát és a doctort, majd csak sikeres helyreállítás után engedi az API/worker indulását. Sikertelen rollback `RECOVERY_REQUIRED` állapotban blokkolva hagyja őket.

Az `apt-daily.service` és az `apt-daily-upgrade.service` külön systemd dependency drop-int kap: mindkettő csak sikeres BDEncode recovery után indulhat. Ezért egy félbemaradt `dpkg`-helyreállítás alatt az általános automatikus APT sem módosíthatja tovább a csomagállapotot.

A külön verziózott VapourSynth tool-release és a benne fordított natív libbluray scanner a csomagtranzakcióval együtt validálódik. Plugin/VMAF/valós x264+x265 codec-smoke/doctor teszt előzi meg az atomi symlink-aktiválást. A commit csak API healthcheck és a worker DB-/lock-inicializálása után kiküldött systemd `READY=1` jelzés után történik; hiba esetén előbb a régi symlink, majd a régi APT stack áll vissza, és azon is lefut a doctor. Folyamatban lévő job alatt nincs megszakítás vagy hot swap.

Az installer a saját mutációi előtt fix célpontlistás, tartós snapshotot publikál `/var/lib/bdencode/install-transactions` alatt. Ez lefedi az app/tool pointereket, a változó systemd unitokat, a napi updater scriptet, az APT source/pin/drop-in fájlokat, a credential drop-int, a configot és az nginx route-ot; külön megőrzi mindkét natív APT-timer aktív és engedélyezett állapotát is. Az `apt-get` előtt a timerek leállnak, a már futó APT/dpkg oneshot természetesen kifut, és a media pin már a csomagművelet előtt aktív. Az installer minden `apt-get` gyermekét külön `/run/lock/bdencode-installer-apt.lock` alatt futtatja. Recoverykor előbb ez, majd a natív frontend/backend/cache/list lockok szabadulását kell kivárni; félbemaradt konfiguráció javítási kísérlete után a runtime csak üres `dpkg --audit` és sikeres `apt-get check` mellett indulhat. A recovery helperk, a stabil API/worker recovery drop-inek és az install-watchdog a rollbacken kívül maradnak. Az `OBSERVING` fázisban csak az API és a rögzített timerek állhatnak le a queue/APT ellenőrzéséhez, ezért busy queue vagy ekkori crash nem szakítja meg a workert; a teljes célpont-rollback csak a tartós `PREPARED` döntés után engedélyezett. A fájlok visszaállítása vagy a validált candidate commitja után külön `services-pending` marker marad addig, amíg a kívánt service-startok sorba nem kerültek. Normál hiba ugyanazt az idempotens állapotgépet futtatja, SIGKILL után pedig a négy install/runtime/APT markert figyelő `bdencode-install-recovery.path` azonnal, rebootkor a recovery gate folytatja.
