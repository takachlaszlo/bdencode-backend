# API contract

Minden endpoint prefixe `/api/v1`. Nginx alatt a külső prefix `/encoder`, tehát például `/encoder/api/v1/health`.

## Job létrehozása

```json
{
  "source_path": "/home/accofil/storage/Example.Disc",
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

## Events

Az események növekvő integer cursorral olvashatók:

```text
GET /events?job_id=<id>&after_id=123
```

Ez alkalmas a későbbi frontend polling/SSE adapteréhez. A raw subprocess output nem kerül API event payloadba; csak stage, progress és sanitizált hibaösszefoglaló.
