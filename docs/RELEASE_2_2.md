# BDEncode 2.2 – kódolás és minőség (tervezet)

Ez a kiadás négy, egymástól független és alapértelmezetten kikapcsolt minőségi képességet ad. A 2.0/2.1 selection-, QC- és artifact-szabályai változatlanok; egyik újdonság sem érinti azokat a jobokat, amelyek nem kérik.

## Újdonságok

| Képesség | Kapcsoló | Részletek |
|---|---|---|
| Automatikus CRF | `video.auto_crf.enabled` | VMAF-pontozott mintakódolásokból választ CRF-et; próbánként checkpoint; elérhetetlen célnál felülvizsgálat |
| Zaj- és szemcseprofilok | `settings.noise_reduction`, `GET /profiles/{encoder}/noise-profiles` | kódolóoldali zajcsökkentés és szemcsemegtartó csomagok; a referencia érintetlen |
| Változó képarány | automatikus | képarányprofil és eseményjelzés; a legszélesebb vászon biztonságos automatikus megtartása |
| HDR10+ / Dolby Vision | `video.dynamic_hdr` | külső eszközökkel kinyert metaadat, képkocka-pontos ellenőrzés, a kész MKV kötelező bizonyítása |

Ezek mellett a kiadás a kezelőfelületet és az üzemeltetést is bővíti:

| Képesség | Hol | Részletek |
|---|---|---|
| Beépített lejátszó és pixelnézet | *Lejátszó* fül, *Nagyítás és pixelnézet* | kész MKV böngészőbarát kivonatai (H.264/AAC, HDR→SDR); a comparison képek egy görgethető konténerben, nagyítva és húzva vizsgálhatók |
| Statisztika | *Statisztika* oldal, `GET /statistics` | megtakarítás, VMAF/SSIM/PSNR, sebesség fájlonként és összesítve, CSV-vel; hiányzó bizonyíték `null`, nem becslés |
| Profilkönyvtár | `/profile-library` | hordozható, ellenőrzött profilok mentése, exportja és importja |
| Adatbázis-védelem | `bdencode db-*`, `/system/backups` | migráció előtti és ütemezett online mentés, `schema_migrations`, offline visszaállítás |
| Windows-telepítő tesztek és CI | `tests/test_windows_install_powershell.py`, `ci.yml` | a valódi argumentumutak lefutnak PowerShell 5.1-ben és 7-ben; a teljes tesztsor Windowson is fut |

A használatot a [README 7.4.1–7.4.5. és 9.5–9.6. pontja](../README.md#741-automatikus-crf-vmaf-cél), a belső szerkezetet az [ARCHITECTURE „Minőségi szakaszok”](ARCHITECTURE.md#minőségi-szakaszok), az API-mezőket az [API.md](API.md#minőségi-opciók-a-video-objektumban) írja le.

## Kompatibilitás

- Az EncoderSettings új `noise_reduction` mezőjének alapértéke 0, és 0 esetén a kódoló parancssora bájtra azonos a korábbival. Az effektív beállítások szótára ugyanakkor új kulcsot kap, ezért a frissítés előtt már lezárult `video-encode` checkpoint hash-e megváltozik; az installer számítási szakaszban lévő jobnál amúgy sem aktivál új kiadást, így folyamatban lévő munkát ez nem érint.
- A `ReferenceScriptPlan` `sample_windows` mezője csak mintaszkripteknél jelenik meg a script-rekordban, ezért a meglévő reference-script checkpointok érvényesek maradnak.
- Az `encode_pipeline_commands` új `extra_video_params` paramétere elhagyható; a `validate_hdr10_side_data` új `allowed_dynamic` paramétere alapértéke üres, vagyis a dinamikus HDR továbbra is tiltott.
- A `GET /capabilities` `dolby_vision_retention` értéke `true` (`dolby_vision_retention_status: "experimental"`), és új `dynamic_hdr_modes`, `auto_crf`, `auto_crf_defaults` mezők jelentek meg.
- A `bdencode doctor` új, nem blokkoló `dynamic_hdr` szakaszt ad.
- Új konfigurációs kulcsok: `backup_interval_hours` (alapérték 24, 0 = kikapcsolva) és `backup_keep_scheduled` (14). A telepítő tesztkörnyezet-izolációja (`-u BDENCODE_BACKUP_...`) ezeket is ismeri.
- Az adatbázis új `schema_migrations` táblát kap az idempotens v2 blokkban; a `schema_version` továbbra is 2, ezért az installer sémaellenőrzései és a visszagörgetés változatlanok. Egy régebbi (2.0/2.1) backend figyelmen kívül hagyja az új táblát.
- Új worker-oldali fájl: `analysis/source-size.json` (a statisztikához). Ez privát, nem kerül a completed fába.
- Az új mutáló útvonalak (`POST /system/backups`, a profilkönyvtár és a kivonatok `POST`/`DELETE` kérései) ugyanazt a same-origin mutációvédelmet kapják, mint a többi `/api/v1` mutáció; külön jogosultsági szint nincs bevezetve.

## Ismert korlátok

- A Dolby Vision megtartás **kísérleti**. Az MKV-be kerülő `dvcC`/`dvvC` konfigurációs rekord létrejötte az mkvmerge/FFmpeg verziótól függ; ha hiányzik, a QC kapu megállítja a jobot. A profil 7 → 8.1 átalakítás a lemezről kiolvasott referencia RPU-tartalmától függ; hiányzó RPU esetén a képkockaszám-ellenőrzés bukik, és felülvizsgálat jön.
- A `hdr10plus_tool` és a `dovi_tool` nem része az automatikus telepítésnek.
- A VMAF-alapú CRF-keresés POSIX named pipe-okat használ (a worker Linux/WSL2 alatt fut), UHD-nál lassú x265 presetnél hosszú lehet; erre való a `probe_preset`.
- A lejátszó kivonatai `ffmpeg`-et igényelnek a szerveren, és egy kivonat UHD forrásnál akár fél percig is készülhet; az egységtesztek hamis futtatóval fedik le a parancsépítést, a gyorsítótárat, a hibakódokat és a range-kiszolgálást, valódi `ffmpeg`-gel még nem történt kipróbálás.
- A statisztika forrásmérete csak azoknál a munkáknál áll rendelkezésre, amelyeket ez a kiadás fejezett be; régebbi munkáknál a megtakarítás üres.
- A visszaállítás szándékosan csak parancssorból, leállított szolgáltatásokkal lehetséges; a webes felület csak mentést készít és listáz.
- A Linuxon futó PowerShell-tesztek (CI) a fejlesztői gépen nem voltak futtathatók; a Windows PowerShell 5.1-es futás igen, és a Linuxon eltérő környezeti feltételt (`LOCALAPPDATA`) a tesztek külön kezelik.
- Az új képességek hardveres és eszközös végigpróbája (valódi lemez, valódi x265/VapourSynth/libvmaf/dovi_tool) még nem történt meg; a logika, a parancsépítés és a kapuk egységtesztekkel és hamis futtatóval lefedettek.
