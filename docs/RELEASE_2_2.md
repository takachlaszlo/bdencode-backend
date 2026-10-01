# BDEncode 2.2 kiadási jegyzet

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
| Kiadáskeresés és automatikus frissítés | `bdencode-update.timer`, `/etc/bdencode/release-update.toml` | a napi timer csak új `vX.Y.Z` kiadást keres; újabb tagnél felügyelet nélkül, a kiadás saját, visszagörgethető telepítőjével frissít (README 11.1.) |
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
- A `bdencode-update.service` a `bdencode-release-update` segédet futtatja a korábbi eszközfrissítő (`bdencode-daily-update`) helyett; az apt-csomagokat és a médiaeszközöket a napi timer már nem frissíti. A régi eszközfrissítő telepítve marad, kézzel indítható. Az eddig telepített 2.1.0 napi frissítője nem tölti le az alkalmazást, ezért az automatikus kiadásfrissítés bekapcsolásához ezt a kiadást egyszer kézzel kell telepíteni (README 11.2.); utána a timer már maga keresi és telepíti az újabb kiadásokat.
- Új fájlok: `/etc/bdencode/release-update.toml` (a telepítő egyszer hozza létre, utána az üzemeltetőé; nem része a rollback-snapshotnak), `/usr/local/libexec/bdencode-release-update` (része a snapshotnak) és `/var/lib/bdencode/release-update/` (`status.json`, `release-update.log`). Az eltávolító mindet törli.

## Valódi eszközös próbából származó javítások

A kiadást egy mesterséges 720p HDR10 „lemezen”, valódi eszközökkel is végigpróbáltuk (részletek lent, az ismert korlátoknál). Ez négy dolgot derített ki, amelyeket még a kiadás előtt javítottunk:

- **A kódolás minden pixelértéke módosult (ffmpeg 7).** A kódolóparancs `vspipe | ffmpeg -f yuv4mpegpipe -i - … -colorspace … -color_range …` alakú. A VapourSynth Y4M-fejléce nem hordoz színtulajdonságot, a kimenetre viszont a parancs színmátrixot és tartományt kér, ezért az ffmpeg 7.1.5 beiktat egy skálázót, amely a jelöletlen bemenetet (BT.601-nek feltételezve) a kért mátrixra alakítja, RGB-n keresztül („YUV color matrix differs for YUV->YUV, using intermediate RGB to convert”). Mért következmény a forrás és a kódolt kép között, bármilyen CRF mellett: 8 bites BT.709-es címnél 35,0 dB PSNR, BT.2020 HDR10-nél 26,7 dB; a telített színek fényessége is eltolódott (egy narancs sáv lumája 839 helyett 888). A javítás után ugyanez 62,3 dB, illetve 60,4 dB. A comparison natív-YUV PSNR-kapuja ezért buktatta a telített képkockákat (mintánként 26–34 dB). A javítás: az `EncoderSettings.ffmpeg_color_input_args()` ugyanazokat a színopciókat a Y4M-olvasó *bemeneti* opcióiként is megadja, így a skálázó nem csinál semmit; a kimeneti címkék változatlanok maradnak. A `tests/test_encode_color_tags.py` valódi ffmpeg-gel, x264-gyel és x265-tel ellenőrzi (PSNR > 50 dB); a javítatlan kódon a BT.709-es eset 34,7 dB-lel elbukik. Az érintettséget csak ffmpeg 7.1.5-tel (Debian 13) mértük; régebbi ffmpeg-verziókat nem próbáltunk.
- **10 bites (UHD HDR10) forrásnál a comparison nem tudott lefutni.** Az FFmpeg Y4M-kimenete `-strict -1` nélkül minden 10 bites pixelformátumot elutasít (`'yuv420p10le' is not an official yuv4mpegpipe pixel format`, kilépőkód 234). A 2.0 óta meglévő natív-YUV comparison mindkét Y4M-író parancsa (`extract_y4m_at_timestamp_command` és a referenciaoldali `_reference_y4m_pipeline`) ezt nem kapta meg, ezért UHD forrásnál a szakasz bukott. 8 bites Blu-raynél nem jelentkezett. Mindkét parancs megkapta a `-strict -1` kapcsolót; a `tests/test_y4m_ten_bit.py` valódi ffmpeg-gel is ellenőrzi (a javítatlan kódon elbukik).
- **Az automatikus CRF próbáinak pontozása feleslegesen lassú volt.** A libvmaf egy szálon futott, és a QC-ben használt PSNR/SSIM/MS-SSIM jellemzőket is számolta, pedig a keresésnek csak a VMAF kell. Azonos bemeneten (432 képkocka, 720p) egy szálon az összes jellemzővel 114 s, nyolc szálon csak VMAF-fal 2,5 s volt, bitre azonos pontszámmal. A `bdencode-vmaf` új `--threads`, `--vmaf-only` és `--fifo-root` kapcsolókat kapott (alapértékei a régi viselkedést adják), a próbák legfeljebb 8 szálat használnak.
- **A VMAF-pontozás névvel ellátott csöveit (FIFO) a job fájában hozta létre**, ezért a job tárhelyszámlálója a pontozás alatt elutasította a fát (`409 storage tree contains an unsupported filesystem entry`), és a tárhelykártya „nem olvasható” volt. A csövek most a `<data_root>/cache/vmaf` alatt jönnek létre; valódi futásban a pontozás alatt a végpont végig 200-at adott.

Emellett a `worker.auto-crf` esemény üzenete megmagyarázza, ha a keresés a megengedett legmagasabb CRF-et választotta, mert a cél ott is teljesült (`max_crf_reached`), vagy ha a találat a célnál a tűrésnél jobban jobb (`best_effort`).

## Ismert korlátok

- A Dolby Vision megtartás **kísérleti**. Az MKV-be kerülő `dvcC`/`dvvC` konfigurációs rekord létrejötte az mkvmerge/FFmpeg verziótól függ; ha hiányzik, a QC kapu megállítja a jobot. A profil 7 → 8.1 átalakítás a lemezről kiolvasott referencia RPU-tartalmától függ; hiányzó RPU esetén a képkockaszám-ellenőrzés bukik, és felülvizsgálat jön.
- A `hdr10plus_tool` és a `dovi_tool` nem része az automatikus telepítésnek.
- A VMAF-alapú CRF-keresés POSIX named pipe-okat használ (a worker Linux/WSL2 alatt fut); ezeket a `<data_root>/cache/vmaf` mappában hozza létre, mert a job fájában a tárhelyszámláló elutasítaná őket. A pontozás csak a VMAF-ot számolja, több libvmaf-szálon (720p-n mérve 114 s helyett 2,5 s, azonos pontszámmal). A próbakódolás ideje UHD-nál lassú x265 presetnél így is hosszú lehet; erre való a `probe_preset`.
- A lejátszó kivonatai `ffmpeg`-et igényelnek a szerveren. Valódi ffmpeg 7.1-gyel, egy mesterséges 720p HDR10 forráson kipróbálva egy 8 s-os kivonat kb. 3 s alatt készül el (HDR10 → SDR H.264/AAC, BT.709 jelölés, `faststart`). UHD forrással még nem mértük; ott akár fél percig is tarthat.
- A statisztika forrásmérete csak azoknál a munkáknál áll rendelkezésre, amelyeket ez a kiadás fejezett be; régebbi munkáknál a megtakarítás üres.
- A visszaállítás szándékosan csak parancssorból, leállított szolgáltatásokkal lehetséges; a webes felület csak mentést készít és listáz.
- A PowerShell-tesztek a GitHub Linux-legein (`pwsh`) és a Windows-legen (PowerShell 7 és 5.1) is lefutnak; a fejlesztői gépen a WSL-ben nincs `pwsh`, ott kimaradnak. A Linuxon eltérő környezeti feltételt (`LOCALAPPDATA`) a tesztek külön kezelik.
- A Windows CI-leg Python 3.13-mal fut: Windowson 3.12-ig az `os.fchmod` hiánya és a durva `time.time()` óra miatt a csak Linuxon használt telepítő- és worker-tesztek platformokozta okból buknak (lásd a README 15.4. pontját).
- Az új képességek valódi eszközös végigpróbája részleges. Egy mesterséges 720p HDR10 „lemezen” (Debian 13 WSL2: ffmpeg 7.1.5, x265, VapourSynth/BestSource, libvmaf, mkvmerge, és a webes felület böngészőben) a teljes csővezeték (automatikus CRF, kódolás, mux, QC, 24 képkockás comparison) `COMPLETED` állapotig lefutott; az elérhetetlen cél felülvizsgálati útja, a változó képarány valódi `cropdetect`-tel, az adatbázis-mentés és -visszaállítás másolaton, valamint a lejátszó és a pixelnézet is valódi eszközökkel működött. Ebben a próbában a lemezbeolvasást és a libbluray-remuxot a szintetikus mester váltotta ki (ugyanazt a fájlt adja, mint a remux). **Még nem próbáltuk:** valódi lemezt, UHD-felbontást (ott a próbakódolás és a comparison sokkal hosszabb), valamint a `hdr10plus_tool` és a `dovi_tool` használatát (a tesztgépen nincsenek fent, és a Debian x265 HDR10+ nélkül készül), vagyis a HDR10+/Dolby Vision megtartás valódi kinyerése és kódolása csak egységtesztekkel és hamis futtatóval lefedett. A telepített 2.1.0-s kiadást a próba nem érintette.
- Az automatikus kiadásfrissítést egység- és integrációs tesztek fedik (valódi git tükör, valódi folyamatindítás és időtúllépés, a telepítő érintett sorai hamis `sudo`-val, hamis telepítő); valódi WSL-gépen a kiadáskeresés rootként, systemd alatt is lefutott (`up_to_date`), az újabb tag felügyelet nélküli telepítését viszont éles gépen még nem próbáltuk ki. Kézi ellenőrzés: `sudo /usr/local/libexec/bdencode-release-update check`.
- Az automatikus telepítés jelszó nélküli `sudo`-t igényel a telepítő felhasználónak (a Windows-telepítő beállítja); anélkül a frissítő csak jelzi az új kiadást. Aláírt tag ellenőrzése nincs: aki a beállított repository írási jogát megszerzi, a telepítéssel kódot futtathat a gépen (README 11.1.).

## Frissítés utáni ellenőrzőlista

1. Ellenőrizd a `GET /api/v1/capabilities` `backend_version` mezőjében a `2.2.0` verziót, a `GET /api/v1/health` `schema_version` mezőjében pedig továbbra is a `2` értéket (a séma verziója nem változott).
2. Futtasd a `bdencode doctor --json` parancsot. Az új `dynamic_hdr` szakasz nem blokkoló: csak azt mutatja, hogy a HDR10+/Dolby Vision megtartáshoz szükséges külső eszközök és az x265-build képességei elérhetők-e.
3. A *Rendszer* oldalon a „Mentés most” gombbal készíts adatbázismentést, és ellenőrizd, hogy megjelenik a listában. A visszaállítás csak parancssorból lehetséges.
4. Az új képességek alapértelmezetten ki vannak kapcsolva. Az automatikus CRF-et és a zajszűrést először egy rövid tesztjobbal próbáld ki; a dinamikus HDR megtartását csak a `hdr10plus_tool` és a `dovi_tool` telepítése után.

## További dokumentáció

- [Felhasználói és telepítési útmutató](../README.md)
- [API contract](API.md)
- [Architektúra és biztonsági határok](ARCHITECTURE.md)
- [BDEncode 2.1 kiadási jegyzet](RELEASE_2_1.md)
- [BDEncode 2.0 történeti kiadási jegyzet](RELEASE_2_0.md)
