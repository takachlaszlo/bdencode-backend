# API contract

Minden endpoint prefixe `/api/v1`. Nginx alatt a külső prefix `/encoder`, tehát például `/encoder/api/v1/health`.

## Job létrehozása

```json
{
  "source_path": "/srv/media/Example.Disc",
  "name": "Example",
  "disc_type": "AUTO",
  "content_type": "FILM",
  "priority": 0,
  "settings": {}
}
```

A `source_path` csak a konfigurált source root alatt lehet. `work_path` csak a jobs root, `output_path` csak a completed root alatt engedélyezett.

Az új job `QUEUED` állapotból az egyetlen előkészítési sávban `SCANNING`
állapotba léphet akkor is, ha egy másik job éppen kódol, muxol, QC-t vagy
comparisont végez. Több scan eredménye várhat egyszerre `AWAITING_SELECTION`
állapotban. A jóváhagyott selection `READY` állapotot jelent; innen prioritás,
majd létrehozási idő szerint kerül az egyetlen soros encode sávba. Új encode
csak az előző teljes lezárása után indul.

## Job vezérlése

A 2.1-ben a pipeline `state` és a kezelői `control_state` két külön állapot. A job válasz az alábbi vezérlési mezőket is tartalmazza:

```json
{
  "state": "ENCODING",
  "control_state": "PAUSE_REQUESTED",
  "control_revision": 4,
  "control_requested_at": "2026-08-16T12:00:00Z",
  "control_message": "karbantartás",
  "allowed_operations": ["cancel"]
}
```

A `control_state` értékei: `RUNNING`, `PAUSE_REQUESTED`, `PAUSED`, `CANCEL_REQUESTED`. Az `allowed_operations` backendből származik; a kliens ezt használja a gombok megjelenítéséhez, de a szerver a tranzakcióban újra ellenőrzi az állapotot és a sávfoglalást.

### Pause, folytatás és cancel

```text
POST /jobs/{id}/pause
POST /jobs/{id}/continue
POST /jobs/{id}/cancel
```

Mindhárom kérés törzse opcionális, alakja:

```json
{
  "expected_control_revision": 4,
  "message": "kezelői megjegyzés"
}
```

Sikeres elfogadáskor `202 Accepted` és az aktuális `Job` érkezik. Az `expected_control_revision` optimista concurrency guard; elavult értéknél `409`. Aktív job pause/cancel kérése először csak tartós request. A worker a futó folyamatok leállítása és a részleges kimenet biztonságos takarítása után írja vissza a `PAUSED`, illetve `CANCELLED` visszaigazolást. Tétlen job pause/cancel művelete azonnal visszaigazolható.

Csak a visszaigazolt `PAUSED` job engedi el a hozzá tartozó scan- vagy encode-sávot. A `POST /continue` változatlan pipeline-állapotból és checkpointokból folytat, és `409` választ adhat, ha időközben egy másik job foglalta el a sávot.

> [!IMPORTANT]
> A `POST /jobs/{id}/resume` továbbra is a `NEEDS_REVIEW` állapot jóváhagyására szolgáló régi végpont, nem pause-folytatás. A `DELETE /jobs/{id}` API v1 kompatibilitási alias, amely cancel műveletet végez; végleges jobtörléshez a `/purge` végpontot kell használni.

### Tárhely-előnézet és takarítás

```text
GET /jobs/{id}/storage
```

A válasz kategóriánkénti byte-számot, teljes méretet, `workspace_status`, `cleanup_allowed` és `release_present` mezőt ad. A mérés link/reparse pont vagy root-határ sérülése esetén fail-closed hibával leáll.

```text
POST /jobs/{id}/cleanup
```

```json
{
  "scope": "temporary",
  "expected_version": 17
}
```

A cleanup jelenleg csak `COMPLETED` jobon és kizárólag `temporary` scope-pal engedélyezett. Karanténon keresztül eltávolítja a privát ideiglenes `work` tartalmat, majd friss tárhelyriportot ad; a completed release-t nem módosítja. Az `expected_version` opcionális, de interaktív kliensnek ajánlott elküldenie.

### Job és completed release törlése

```text
DELETE /jobs/{id}/purge?expected_version=17&preserve_release=true
```

Csak `COMPLETED`, `FAILED` vagy `CANCELLED` job törölhető. A `preserve_release` értéke kizárólag `true` lehet: a művelet eltávolítja a job rekordját, privát munkaterületét és külső kimenetel nélküli privát release kitjeit, de a completed release-t mindig megőrzi. `UNKNOWN`, `PUBLISHED`, nem `REJECTED` qBittorrent-receipt vagy publication receipt esetén a job purge megtagadja az auditrekord elvesztését; előbb a külön, erősen megerősített completed-release törlést kell választani. A backend az összes preparation `{id: version}` snapshotját a job törlésének tranzakciójában újraellenőrzi. Siker: `204 No Content`.

A publikus completed release szándékos törlése külön végpont:

```text
DELETE /jobs/{id}/release
```

```json
{
  "confirmation": "Release.Name",
  "expected_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "force_if_seeded": false,
  "preparation_versions": {
    "0123456789abcdef0123456789abcdef": 6
  }
}
```

A `confirmation` az MKV fájlnév kiterjesztés nélküli stemje, az `expected_sha256` az aktuális MKV hash. A `preparation_versions` kötelező, pontos snapshot: kulcskészletének és minden verziójának egyeznie kell a jobhoz tartozó összes preparation aktuális állapotával; preparation nélküli jobnál az értéke `{}`. A backend ezt ugyanabban az adatbázis-tranzakcióban ellenőrzi újra, amelyben a preparation rekordokat törli és a completed-release-deleted audit eseményt rögzíti. Egy közben létrehozott vagy módosult preparation `409` konfliktust okoz még a fájlrendszer végleges leválasztása előtt.

`force_if_seeded: true` szükséges, ha a release qBittorrentbe kerülhetett vagy bármely preparation `UNKNOWN` kimenetelű; ez különösen az `ADDED_AND_RECHECKING` és az `UNKNOWN` qBittorrent-receiptre vonatkozik. Aktív `PREPARING`, `SEEDING_CHECK`, `SEEDING` vagy `PUBLISHING` művelet mellett a törlés tiltott. Az `UNKNOWN` és `PUBLISHED` auditrekord nem törölhető egyszerű preparation- vagy jobtörléssel; csak ez a teljes, megerősített completed-release törlés távolíthatja el. Siker: `204 No Content`.

## Selection

A worker scanje után a `POST /jobs/{id}/selection` payload `selection` objektuma határozza meg a playlistet, szűrést és minden sáv műveletét. A backend nem talál ki nyelvet vagy sávszerepet alacsony confidence esetén.

A webes felület mentés előtt ugyanazt az objektumot a `POST /jobs/{id}/selection/validate` végpontra küldi. Ez a valódi plannerrel normalizálja az effektív x264/x265 profilt, cropot és filtert, visszaadja az FFmpeg videóargumentumokat és a figyelmeztetéseket, de nem módosítja a jobot, az eseménynaplót vagy a fájlrendszert. Sikeres ellenőrzés után a változatlan selection a `POST /jobs/{id}/selection` végponton hagyható jóvá; az `expected_version` mindkét kérésnél megakadályozza a stale felülírást.

```json
{
  "selection": {
    "schema_version": 2,
    "playlist_id": "00800",
    "angle": 1,
    "output_name": "Movie.2026.1080p.BluRay.x264.mkv",
    "video": {
      "detail_level": "advanced",
      "temporal_filter": "progressive",
      "crop": {"left": 0, "top": 138, "right": 0, "bottom": 138},
      "settings": {"crf": 17.5, "preset": "slow"}
    },
    "tracks": [
      {
        "stream_id": "audio:4352",
        "action": "copy",
        "language": "eng",
        "name": "English DTS-HD MA 5.1",
        "default": true,
        "forced": false,
        "order": 0
      },
      {
        "stream_id": "audio:4353",
        "action": "flac",
        "language": "hun",
        "name": "Hungarian FLAC 2.0",
        "default": false,
        "forced": false,
        "order": 1
      },
      {
        "stream_id": "subtitle:4608",
        "action": "copy",
        "language": "eng",
        "name": "English Forced",
        "default": false,
        "forced": true,
        "subtitle_kind": "forced",
        "order": 2
      }
    ],
    "upload_images": true,
    "image_upload_provider": "auto"
  }
}
```

Az `image_upload_provider` értéke `auto`, `imgbb`, `catbox` vagy `freeimage`.
Automatikus módban a sorrend ImgBB → Catbox → Freeimage, és csak az első
sikeres kép előtt engedélyezett a váltás. Kézi választásnál nincs failover.

Új kliensnek `schema_version: 2` értéket kell küldenie. A backend az 1-es sémájú, már sorban álló selectiont kompatibilitásból elfogadja és 2-es effektív tervvé migrálja; egy régi BD/x264 terv explicit `chroma_qp_offset: 0` értéke ekkor effektív `-2` lesz.

Minden audio- és feliratsávhoz explicit művelet szükséges. Audiónál: `copy`, `flac`, `ac3`, `eac3`, `dts` vagy `omit`; feliratnál csak `copy` vagy `omit`. Minden megtartott felirathoz explicit `subtitle_kind` (`full` vagy `forced`) kell, és a `forced` flagnek ezzel egyeznie kell. Az audio célok determinisztikusak: AC-3 640 kb/s, E-AC-3 1024 kb/s, DTS core 1536 kb/s, mind 48 kHz-en és legfeljebb 5.1 csatornával. DTS-HD forrás `dts` céljánál a beágyazott core újrakódolás nélkül kerül kiemelésre. Az effektív presetek a `GET /api/v1/capabilities` válasz `constraints.audio_transcode_presets` mezőjében is olvashatók. `temporal_filter`: `progressive`, `ivtc_tff`, `ivtc_bff`, `bwdif_tff`, `bwdif_bff`, `hybrid_safe_bob_tff`, `hybrid_safe_bob_bff`.

Az opcionális `dual_type_match` alapértéke `true`: progresszív timeline esetén az I/P/B pár csak akkor fogadható el, ha ugyanazon presentation frame az eredeti és a kész bitstreamben is ugyanabba a kategóriába tartozik. IVTC/deinterlace után a tömörítetlen referencia nem rendelkezik forrás-bitstream képtípussal, ezért ott a kategória a kész encode típusa, az index/PTS-egyezés továbbra is kötelező.

Ha a videó-bitfolyamból hiányzik a színprimerek, az átviteli karakterisztika vagy a mátrix jelölése, a validate végpont `422` választ ad `source_color_confirmation_required` kóddal, a hiányzó mezőkkel és – csak egyértelmű HD SDR BD vagy HDR10 UHD esetén – biztonságos javaslattal. A felhasználó a teljes `video.settings.color` objektummal erősítheti meg a `primaries`, `transfer`, `matrix`, `range` és `chroma_location` értékeket. A backend a lemezről ténylegesen kiolvasott érték felülírását továbbra is elutasítja; ez címkézés, nem színkonverzió.

Ha a job `NEEDS_REVIEW`, a korrigált teljes `selection` ugyanerre az endpointra küldhető. A backend ilyenkor mindig `READY` állapotból játssza újra a függőség-ellenőrzést; egy késői review nem párosíthat régi videót új beállítás-manifesttel.

Nyelv nélkül megtartott audiónál a worker a reference remuxból hat, filmen elosztott CPU-only beszédmintát elemez. Csak magas bizalmú konszenzust alkalmaz automatikusan; konfliktus, kevés beszéd, ismeretlen PGS vagy hiányzó modell esetén a job review-ba kerül, és kézi ISO 639-2/BCP 47 override szükséges.

## Release-előkészítés

A release API csak konfigurált workspace gyökerekkel használható, és csak olyan `COMPLETED` jobot fogad el, amelynek egyetlen, tulajdonosi rekorddal, artifact SHA-256-tal és tényleges fájlhashsel egyező MKV-ja van. A root-only trackerprofil privát passkeyt tartalmazó announce URL-t is tárolhat, ezért nem publikus és nem tekinthető titokmentesnek. A credential nélküli, fix dupe/publish endpoint URL-ekhez tartozó tokenek és a qBittorrent hitelesítési adatai systemd credentialből töltődnek. A beállítást a [README release-konfigurációs fejezete](../README.md#53-trackerprofil-és-qbittorrent-beállítása) ismerteti.

### Profilok

```text
GET /release-profiles
```

A válasz alakja `{"items": [...], "count": n}`. Az elemek publikus policymezőket, kép- és darabméret-korlátokat, feature flag-eket és `profile_digest` értéket tartalmaznak; endpointot, announce URL-t vagy credentialnevet nem adnak vissza.

### Előkészítés létrehozása és lekérése

```text
POST /jobs/{job_id}/release-preparations
```

```json
{
  "profile_id": "example",
  "metadata": {
    "schema_version": 1,
    "release_name": "Movie.2026.1080p.BluRay.x264-GROUP",
    "title": "Movie",
    "year": 2026,
    "edition": null,
    "imdb_id": "tt1234567",
    "tmdb_id": 12345,
    "category": "MOVIE",
    "source_media": "BluRay",
    "resolution": "1080p",
    "video_codec": "x264",
    "audio_codecs": ["DTS-HD MA", "FLAC"],
    "languages": ["eng", "hun"]
  }
}
```

A `release_name` értékének pontosan egyeznie kell a completed MKV stemjével. Siker: `201 Created`.

A metadata `schema_version` mezője elhagyható, alapértéke `1`; az `edition`, `imdb_id` és `tmdb_id` opcionális, a többi példabeli metadata mező kötelező.

```text
GET /jobs/{job_id}/release-preparations
GET /release-preparations/{preparation_id}
```

A jobhoz tartozó lista közvetlen JSON tömb. Egy `ReleasePreparationView` fő mezői: `id`, `job_id`, `state`, `profile_id`, `profile_digest`, `metadata`, logikai `payload_path`, `payload_size`, `payload_sha256`, `kit_ready`, `manifest_sha256`, `torrent_infohash`, `torrent_sha256`, `dupe_receipt`, `qbittorrent_receipt`, `publication_receipt`, `error`, `version`, `created_at`, `updated_at`. Belső fájlrendszerútvonalat nem ad vissza.

### Validate, build és export

```text
POST /release-preparations/{id}/validate
POST /release-preparations/{id}/build
POST /release-preparations/{id}/export
```

Mindhárom kérés törzse:

```json
{"expected_version": 3}
```

A validate nem módosító preflight: újraellenőrzi a payloadot, profile digestet, comparison képeket és – ha már létezik – az upload kit manifestjét. A build privát v1 torrentet és manifesttel kötött upload kitet készít. A torrent pontosan ezt az egy payloadot írja le:

```text
Release.Name/Release.Name.mkv
```

Napló, comparison, NFO vagy más sidecar nem része a torrent payloadnak. Az export a manifesthez kötött torrentet stabil, korlátozott méretű olvasással memóriába tölti, majd a hash, infohash, payloadútvonal és payloadméret ismételt ellenőrzése után közvetlenül ezekből a byte-okból készít `application/x-bittorrent` választ. A válasz `Cache-Control: private, no-store, max-age=0`, `Pragma: no-cache` és `X-Content-Type-Options: nosniff` fejlécet kap; nincs késői fájlmegnyitás vagy fájlútvonalra épülő streaming. Az announce URL passkeyt tartalmazhat, ezért az exportált torrent titok. A privát upload kit nem a completed release-ben, hanem a konfigurált `release-kits/<id>/` területen található.

### Dupe check és seed-előkészítés

```text
POST /release-preparations/{id}/dupe-check
POST /release-preparations/{id}/seed
```

Mindkét kérés törzse:

```json
{"expected_version": 4}
```

A dupe check csak `READY` kitből indul. Közvetlenül a hálózati kérés előtt újraellenőrzi a preparationhöz kötött trackerprofil teljes digestjét, valamint a completed payload tulajdonosi rekordját, közvetlen útvonalát, méretét és SHA-256 hashét. A tracker válasza receiptként rögzül: `CLEAR` esetén az állapot `READY_TO_PUBLISH`, találatnál `NEEDS_REVIEW`, bizonytalan kimenetnél `UNKNOWN`. Az upload a tárolt receiptben nem bízik meg önmagában: közvetlenül az exkluzív publikálási claim előtt új távoli dupe checket futtat, és csak ugyanahhoz a profilhoz, metadata digesthez és manifesthez kötött, friss `CLEAR` eredménnyel folytatja.

A seed művelet a távoli hívás előtt ugyanígy újraellenőrzi a profile digestet és a teljes completed payload-kötést. A manifestből stabilan beolvasott torrent byte-jait és az elvárt infohasht adja át qBittorrentnek; job–profil–infohash szintű tranzakciós claim kizárja a párhuzamos, ekvivalens preparationök duplikált addját. Ezután a torrentet a completed roothoz kötött save path-tal, leállítva adja qBittorrenthez, majd teljes rechecket kér; a hálózati lease idején az állapot `SEEDING`. Nem indítja el a torrentet. A három receipt-kimenet: `ADDED_AND_RECHECKING`, `REJECTED`, `UNKNOWN`. Az elvárttól eltérő receipt-infohash is `UNKNOWN`. Sikeres `ADDED_AND_RECHECKING` után nincs automatikus ismételt add. Bizonytalan kimenetnél az automatikus retry tiltott, mert a torrent felvétele vagy a recheck ténylegesen megtörténhetett.

### Explicit jóváhagyású publikálás

```text
POST /release-preparations/{id}/upload
X-BDEncode-Manifest: <manifest_sha256>
X-Remote-User: <reverse-proxy-authenticated-operator>
```

```json
{
  "expected_version": 5,
  "manifest_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

A manifest header és a body manifest hashének egyeznie kell. `approved_by` nincs a request bodyban: az auditált jóváhagyó kizárólag a megbízható reverse proxy által felülírt, Basic Authból származó `X-Remote-User` identity lehet. Közvetlen loopback automatizálásnak ugyanezt az egy, érvényes fejlécet explicit kell küldenie; több vagy hibás identity elutasításra kerül. A kérés csak azonos originről fogadható el; `Sec-Fetch-Site: cross-site` vagy eltérő `Origin` esetén `409`.

Az engedély rövid életű, az aktuális profile/manifest és a közvetlenül az upload során lekért friss dupe receipt hármashoz kötött. A publish közvetlenül a távoli side effect előtt újraellenőrzi a profile digestet és a completed payload teljes tulajdonosi/útvonal/méret/hash kötését. A job és trackerprofil összes preparationjére kiterjedő tranzakciós claim miatt egyszerre csak egy aktív, sikeres vagy bizonytalan publikálási kimenet lehet. Távoli timeout vagy más bizonytalan eredmény `UNKNOWN`, és nem indít automatikus újrafeltöltést.

### Előkészítés törlése

```text
DELETE /release-preparations/{id}?expected_version=6
```

A művelet a privát upload kitet csak a manifest teljes újraellenőrzése után törli. `PREPARING`, `SEEDING_CHECK`, `SEEDING` vagy `PUBLISHING` állapotban nem engedélyezett. `UNKNOWN`, `PUBLISHED`, nem `REJECTED` qBittorrent-receipt vagy bármely publication receipt esetén az auditrekord szintén nem törölhető ezen a végponton. Ez nem törli sem a jobot, sem a completed release-t.

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

Az aktív operation lease-ek: `PREPARING`, `SEEDING_CHECK`, `SEEDING` és `PUBLISHING`. API-szolgáltatás indulásakor a félbemaradt `PREPARING` állapot `FAILED` lesz, és a hozzá tartozó árva build-staging könyvtár karanténon keresztül eltávolítható. A félbemaradt `SEEDING_CHECK`, `SEEDING` és `PUBLISHING` állapot `UNKNOWN` lesz, mert a távoli hatás nem bizonyítható; recovery egyik hálózati műveletet sem ismétli meg.

Minden módosító művelet az aktuális `version` értéket várja. `409` után a kliensnek újra kell olvasnia a rekordot; ugyanazt a távoli műveletet nem szabad vakon megismételnie.

## Adatbázisséma és kompatibilitás

A 2.1 backend SQLite `schema_version = 2` sémát használ. Első megnyitáskor az 1-es sémát tranzakciósan egészíti ki a jobvezérlés mezőivel, majd idempotensen létrehozza a release-előkészítés és receipt-események, valamint a crash-safe maintenance operationök és célpontclaim-ek tábláit, továbbá a `PAUSED` lane policyhez tartozó indexeket. A meglévő job pipeline-állapotok és selectionök megmaradnak; az új control alapértéke `RUNNING`, revisionje `1`.

Az alkalmazás az ismeretlen, nem 1-es vagy 2-es sémát megtagadja. Frissítés előtt készíts konzisztens mentést az adatbázisról; a 2.1 által már megnyitott adatbázist ne próbáld 2.0 backenddel írni. A `GET /health` válasz `schema_version` mezője a tényleges adatbázissémát mutatja.

## Events

Az események növekvő integer cursorral olvashatók:

```text
GET /events?job_id=<id>&after_id=123
```

Ez alkalmas a későbbi frontend polling/SSE adapteréhez. A raw subprocess output nem kerül API event payloadba; csak stage, progress és sanitizált hibaösszefoglaló.
