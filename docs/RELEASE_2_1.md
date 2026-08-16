# BDEncode 2.1 kiadási jegyzet

A BDEncode 2.1.0 a napi üzemeltetést teszi teljes értékűvé: a jobok most már tartósan szüneteltethetők és folytathatók, a foglalt tárhely biztonságosan felmérhető és takarítható, a kész encode-ból pedig ellenőrzött torrent és privát tracker upload kit készíthető. A 2.0 selection-, QC- és artifact-szabályai változatlanul érvényesek; a [2.0 kiadási jegyzet](RELEASE_2_0.md) történeti háttérként megmarad.

## Legfontosabb változások

- Külön, tartós `control_state` és `control_revision` a pause/cancel request–ack protokollhoz.
- A visszaigazolt `PAUSED` job elengedi a scan- vagy encode-sávot; folytatáskor azt tranzakciósan újra meg kell szereznie.
- Jobonkénti tárhely-előnézet, `COMPLETED` ideiglenes cleanup és karanténalapú törlés.
- A job törlése mindig megőrzi a publikus completed release-t; annak törlése külön, név-, hash- és pontos preparation-verzió-snapshot megerősítéses művelet.
- Szigorú trackerprofilok, titkosított systemd credentialök és privát v1 torrent.
- Manifesttel kötött upload kit, dupe check, qBittorrent paused add + full recheck és explicit jóváhagyású tracker publish.
- Távoli bizonytalanságnál `UNKNOWN`, automatikus side-effect retry nélkül.
- Automatikus SQLite v1 → v2 migráció.

## Tartós jobvezérlés

A pipeline állapota (`QUEUED`, `SCANNING`, `ENCODING`, `QC` stb.) és a kezelői vezérlés most két külön tengely. A vezérlés lehetséges értékei:

| `control_state` | Jelentés |
|---|---|
| `RUNNING` | A job fut vagy a saját pipeline-sorára vár. |
| `PAUSE_REQUESTED` | Az API tartósan rögzítette a kérést; a worker még állítja le a szakaszt. |
| `PAUSED` | A worker igazolta, hogy nem maradt futó szakaszfolyamat; a lane felszabadult. |
| `CANCEL_REQUESTED` | A leállítás és a részleges kimenet takarítása folyamatban van. |

A pause és cancel aktív jobnál request/ack protokoll. A process runner a teljes process groupot leállítja, szükség esetén TERM után KILL-lel, majd eltávolítja a nem hiteles részleges outputot. Csak ezután commitolja a worker a visszaigazolást. A művelet ezért nem feltétlenül azonnali, viszont service restart után is folytatható, és az adatbázis soha nem állítja idő előtt, hogy a folyamat megállt.

A pause megtartja az aktuális pipeline-állapotot és az érvényes checkpointokat. A félbehagyott szakasz folytatáskor újrafuthat. A `PAUSE_REQUESTED` még foglalja a lane-t; csak a `PAUSED` engedi el. Ha közben másik job foglalja el a sávot, a folytatás `409` választ ad, és később biztonságosan újrapróbálható.

A `control_revision` elkülönül a gyakran változó job `version` értéktől. A frontend az `expected_control_revision` elküldésével védekezik az elavult gombnyomások ellen. A backend által számított `allowed_operations` jelzi az aktuálisan megengedett kezelői műveleteket.

Kompatibilitási megjegyzések:

- a pause folytatása: `POST /jobs/{id}/continue`;
- a régi `POST /jobs/{id}/resume` továbbra is `NEEDS_REVIEW` elfogadás;
- a régi `DELETE /jobs/{id}` cancel alias marad, nem végleges törlés;
- `FAILED` jobhoz retry, `CANCELLED` jobhoz restart továbbra is külön művelet.

## Tárhely és életciklus

A `GET /jobs/{id}/storage` kategóriánként megmutatja a privát workspace és a publikus release méretét. A bejárás nem követ symlinket, junctiont vagy más reparse pontot, ellenőrzi a root-határokat és identitásváltozásnál fail-closed leáll.

A `POST /jobs/{id}/cleanup` jelenleg csak `COMPLETED` jobon, `scope: "temporary"` értékkel használható. A privát, nagy ideiglenes munkafájlokat tartós maintenance intent után karanténba választja le. A domain DB-commit és a journal `COMMITTED` állapota egy tranzakció, ezért processzhalál után a recovery a nem commitolt leválasztást visszaállítja, a commitoltat pedig idempotensen véglegesíti. Az MKV, a publikus comparison és más completed sidecar változatlan marad.

A végleges törlés két külön biztonsági tartomány:

1. A `DELETE /jobs/{id}/purge?...&preserve_release=true` terminális jobot, privát workspace-et és külső kimenetel nélküli privát release kitjeit töröl, a completed release-t mindig megőrzi.
2. A `DELETE /jobs/{id}/release` kizárólag `COMPLETED` job publikus release-ét törli, ha a kezelő megadja a pontos release-nevet, az aktuális MKV SHA-256 hashét és a job összes preparation rekordjának pontos `{id: version}` snapshotját. Ezt a szerver a törlés DB-tranzakciójában ismét összeveti az aktuális halmazzal. Seedelt vagy `UNKNOWN` kimenetnél külön `force_if_seeded` jóváhagyás kell.

Az `UNKNOWN`, `PUBLISHED`, nem `REJECTED` qBittorrent-receipt és publication receipt auditrekord nem törölhető egyszerű preparation- vagy jobtörléssel; csak a teljes, külön megerősített completed-release törlés távolíthatja el. Az eredeti Blu-ray forrás egyik művelet célpontja sem lehet. Aktív release build, dupe check, `SEEDING` vagy publish blokkolja a kapcsolódó törlést.

## Release-előkészítés

Az új munkafolyamat csak sikeres `COMPLETED` encode-ból indul. A backend létrehozáskor és validate/build előtt ellenőrzi:

- pontosan egy regisztrált MKV output van;
- az MKV a completed root közvetlen `Release/Release.mkv` struktúrájában található;
- a fájl tényleges mérete és SHA-256 hash-e egyezik az artifacttal;
- a 2-es sémájú owner rekord ugyanazt a nevet és hasht rögzíti;
- a release metadata neve pontosan egyezik az MKV stemjével;
- a trackerprofil digestje és a comparison manifestből választott encode-oldali képek változatlanok.

A dupe check, qBittorrent add/recheck és tracker publish közvetlenül minden távoli kérés előtt ismét ellenőrzi a preparationhöz kötött teljes profile digestet és a completed payload owner rekordját, közvetlen útvonalát, méretét és SHA-256 hashét. A create/build idején sikeres preflight önmagában nem jogosít későbbi hálózati side effectre.

A fő lépések:

1. **Create** – profilhoz, metadata digesthez és payload SHA-256-hoz kötött preparation rekordot készít.
2. **Validate** – nem módosító preflightot futtat, a már elkészült kitet is újrahash-eli.
3. **Build** – privát v1 torrentet, MediaInfót, NFO-t, BBCode-ot, ellenőrzött screenshotokat, checksumokat és kanonikus manifestet készít.
4. **Export** – a manifesthez kötött torrentet stabil, korlátozott méretű olvasással memóriába tölti, újraellenőrzi, majd `private, no-store` válaszként adja le kézi használatra.
5. **Dupe check** – a konfigurált, fix tracker endpointon fut; csak az aktuális manifesthez tartozó `CLEAR` receipt nyitja meg a publikálási kaput.
6. **Seed** – külön `SEEDING` lease alatt, stabil torrent byte-okkal és expected infohashsel, leállítva hozzáadja a torrentet qBittorrenthez, majd teljes rechecket kér. A globális claim több ekvivalens preparation párhuzamos addját is kizárja; automatikus start nincs.
7. **Upload** – közvetlenül előtte új távoli dupe checket futtat, majd globális job–trackerprofil claim alatt, az aktuális manifest SHA-256, preparation verzió, same-origin header és a trusted reverse proxy `X-Remote-User` identityjével rögzített explicit jóváhagyás mellett publikál. A bodyban nincs kliens által választható `approved_by`.

### Torrent- és kitpolicy

A `.torrent` privát flaget és tracker `source` mezőt tartalmaz. A payload pontosan egy fájl:

```text
Release.Name/Release.Name.mkv
```

A comparison képek, NFO, MediaInfo, BBCode és checksumok nem részei a torrent payloadnak. Ezek a torrenttel és manifesttel együtt a privát `release-kits/<preparation-id>/` könyvtárban maradnak. A kit nem kerül a publikus `completed/<release>/` fába, és az API belső fájlrendszerútvonalat nem szolgáltat ki. Az announce URL személyes passkeyt tartalmazhat, ezért a profil, a torrent és az upload kit egyaránt titkos adat.

### Fail-closed távoli műveletek

A tracker és qBittorrent kliens fix endpointtal, redirect nélkül és host-allowlisttel dolgozik. A dupe/publish endpoint HTTPS, credential-, query- és fragmentmentes URL; hitelesítését külön systemd credential adja. qBittorrent HTTP csak loopback címen lehet. Ha timeout, kapcsolatvesztés vagy nem egyértelmű válasz miatt nem bizonyítható, hogy egy kérés céloldali hatása megtörtént-e, az eredmény `UNKNOWN`.

Az `UNKNOWN` nem átmeneti „próbáld újra” hiba. A preparation állapot vagy receipt automatikus retryt tilt, mert a vak ismétlés duplikált tracker release-t vagy bizonytalan qBittorrent állapotot okozhat. A kezelőnek előbb a távoli szolgáltatásban kell ellenőriznie az eredményt. API-induláskor a félbemaradt `PREPARING` lease `FAILED` lesz és az árva build-staging kitakarítható; a félbemaradt `SEEDING_CHECK`, `SEEDING` vagy `PUBLISHING` lease `UNKNOWN` állapotba kerül, automatikus hálózati ismétlés nélkül. Az `UNKNOWN` és `PUBLISHED` terminális, megőrzendő auditállapot.

### Release állapotgép

```text
NOT_PREPARED / FAILED → PREPARING → READY
                                      ├→ SEEDING_CHECK → READY_TO_PUBLISH
                                      │          └─────→ NEEDS_REVIEW
                                      └→ SEEDING ──────→ READY
READY_TO_PUBLISH ──────────────────────→ SEEDING ──────→ READY_TO_PUBLISH
READY_TO_PUBLISH ──────────────────────→ PUBLISHING ───→ PUBLISHED
bármely bizonytalan távoli kimenet ────────────────────→ UNKNOWN
```

## Profilok és credentialök

Az aktív trackerprofil alapértelmezett helye:

```text
/etc/bdencode/release-profiles.json
```

A telepítő üres profildokumentumot helyez el; minta a repository [release-profiles.example.json](../config/release-profiles.example.json) fájlja. A root-only JSON policyt, darabméret-határokat, képszámot, fix dupe/publish endpointot és host-allowlistet, opcionális qBittorrent base URL-t, valamint HTTPS announce URL-t tartalmaz. Az announce útvonala vagy queryje privát passkeyt tartalmazhat, ezért a teljes profil titok. Ezzel szemben a dupe/publish endpoint URL-je kötelezően credential-, query- és fragmentmentes.

A titkok titkosított systemd credential fájlok. Alapértelmezett qBittorrent nevek:

```text
qbittorrent-username
qbittorrent-password
```

A tracker token nevét a profil `tracker.credential_name` mezője adja meg. A telepítő által ismert `tracker-aither-api-token` automatikusan bekerül a kezelt API `credential.conf` drop-inba. Egyedi trackercredential külön, üzemeltető által kezelt `bdencode-api.service.d/tracker-local.conf` drop-inba kerüljön; a kezelt `credential.conf` fájlt frissítés felülírja. Az `uninstall.sh --purge-credentials` pontosan hat fix credentialt töröl: `imgbb-api-key`, `catbox-userhash`, `freeimage-api-key`, `qbittorrent-username`, `qbittorrent-password` és `tracker-aither-api-token`. Egyedi credentialt és `tracker-local.conf` fájlt nem. A fájlok előállítását és jogosultságait a [README 5. fejezete](../README.md#5-képfeltöltő-és-release-szolgáltatások-beállítása) írja le.

## API-változások röviden

Új jobműveletek:

```text
POST   /api/v1/jobs/{id}/pause
POST   /api/v1/jobs/{id}/continue
POST   /api/v1/jobs/{id}/cancel
GET    /api/v1/jobs/{id}/storage
POST   /api/v1/jobs/{id}/cleanup
DELETE /api/v1/jobs/{id}/purge
DELETE /api/v1/jobs/{id}/release
```

Új release-műveletek:

```text
GET    /api/v1/release-profiles
POST   /api/v1/jobs/{job_id}/release-preparations
GET    /api/v1/jobs/{job_id}/release-preparations
GET    /api/v1/release-preparations/{id}
POST   /api/v1/release-preparations/{id}/validate
POST   /api/v1/release-preparations/{id}/build
POST   /api/v1/release-preparations/{id}/export
POST   /api/v1/release-preparations/{id}/dupe-check
POST   /api/v1/release-preparations/{id}/seed
POST   /api/v1/release-preparations/{id}/upload
DELETE /api/v1/release-preparations/{id}
```

A completed release törlési body most kötelező `preparation_versions` snapshotot is kér. A publish bodyból kikerült az `approved_by`; az approver kizárólag a trusted reverse proxy által felülírt `X-Remote-User` fejlécből származik. A pontos request/response alakok, revision/version guardok és publish headerek a [teljes API contractban](API.md) találhatók.

## Adatbázis-migráció

A backend adatbázissémája 2-es verzióra emelkedik. Első megnyitáskor a v1 adatbázis `BEGIN IMMEDIATE` tranzakcióban megkapja a vezérlési oszlopokat; a meglévő jobok `control_state=RUNNING`, `control_revision=1` alapértékkel maradnak meg. Ezután idempotensen létrejönnek a pause lane-policy új indexei, a release-preparation és eseménytáblák, valamint a durable maintenance journal és egyedi célpontclaim táblái.

A támogatott Linux installer az API/worker leállítása után, de a candidate `init-db` előtt a SQLite backup API-val konzisztens, root-only adatbázismentést készít, és annak digestjét a telepítési tranzakcióhoz köti. Ha a candidate health/doctor ellenőrzése megbukik, vagy a telepítés a tartós `HEALTHY` döntés előtt megszakad, a rollback először ezt az adatbázismentést állítja vissza és ellenőrzi, utána az előző app pointert és konfigurációt, és csak ezután indíthatja a régi szolgáltatásokat. A rollback csak akkor engedi elindulni az előző backendet, ha a visszaállított séma azzal bizonyítottan kompatibilis; ellenkező esetben recovery-required blokkolás marad. Tartós `HEALTHY` után a recovery már a validált candidate commitját finalizálja, nem rollbackel. Így a 2.0 backend nem indul el egy már v2-re migrált live adatbázissal.

Kézi mentés továbbra is ajánlott fontos frissítés előtt. A 2.1 csak az 1-es és 2-es sémát nyitja meg; ismeretlen verziót megtagad. Miután egy, a tranzakciós installeren kívüli 2.1 folyamat megnyitotta az adatbázist, 2.0 backenddel ne írj bele. A migráció eredménye a `GET /api/v1/health` `schema_version` mezőjében ellenőrizhető. Windows telepítéshez és frissítéshez a támogatott repository ág `main`.

## Frissítés utáni ellenőrzőlista

1. Ellenőrizd a `GET /api/v1/capabilities` `backend_version` mezőjében a `2.1.0` verziót, a `GET /api/v1/health` `schema_version` mezőjében pedig a `2` értéket.
2. Futtasd a `bdencode doctor --json` parancsot.
3. Ellenőrizd egy tétlen tesztjob pause/continue/cancel gombjait és a `control_revision` növekedését.
4. Nyisd meg egy befejezett job tárhely-előnézetét; a cleanup előtt ellenőrizd, hogy a publikus release külön kategória.
5. Release-integráció használatakor töltsd ki a profilfájlt, telepítsd a credentialöket, majd indítsd újra az API szolgáltatást.
6. Első tracker publish előtt exportáld és vizsgáld meg kézzel a torrentet és az upload kitet. qBittorrentben csak sikeres recheck után indítsd el a seedelést.

## További dokumentáció

- [Felhasználói és telepítési útmutató](../README.md)
- [API contract](API.md)
- [Architektúra és biztonsági határok](ARCHITECTURE.md)
- [BDEncode 2.0 történeti kiadási jegyzet](RELEASE_2_0.md)
