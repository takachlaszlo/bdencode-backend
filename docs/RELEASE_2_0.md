# BDEncode 2.0 kiadási jegyzet

Verzió: `2.0.0`

A 2.0 a kódolási, muxolási és minőség-ellenőrzési szabályok nagy, fail-closed frissítése. Ha a forrás bizonyítékai hiányosak, a sávbesorolás kétértelmű vagy valamelyik hard gate nem teljesül, a munka `NEEDS_REVIEW` állapotba kerül; a rendszer nem készít csendben kétes release-t.

## Frissítés és migráció

> [!IMPORTANT]
> Frissítés előtt fejezd be az aktív encode-ot. A 2.0 újraellenőrzi a jóváhagyott tervet és a kész kimenetet, ezért egy régi checkpoint csak akkor használható tovább, ha az új szabályoknak is megfelel.

- Az új selection formátum `schema_version: 2`. A backend az 1-es sémájú, már sorban álló munkákat továbbra is beolvassa és 2-es effektív tervvé normalizálja.
- Régi, 1-es sémájú x264 tervben a `chroma_qp_offset: 0` a korábbi x264-kompenzáció miatt effektív `-2` értékre migrál. A 2-es séma már közvetlenül az effektív értéket tárolja.
- A `comparison_pair_count` érvényes tartománya `20–50`, az alapérték `24`. A korábbi `3–5` értéket tartalmazó egyedi konfigurációt frissíteni kell. A `comparison_frames_per_type` kulcs még betölthető, de az új mintavétel nem használja.
- Minden megtartott feliratot explicit `full` vagy `forced` értékkel kell besorolni a `subtitle_kind` mezőben. A forced jelzőnek egyeznie kell ezzel a besorolással.
- A kimeneti névnek tiszta encode-névnek kell lennie: BD-n `*.1080p.BluRay.x264[-GROUP]`, UHD-n `*.2160p.UHD.BluRay.x265[-GROUP]`. Az aláhúzás, a `COMPLETE.BLURAY`/`BDMV`/forráskodek-tag, a valótlan `MULTi`, illetve a kimenetben nem létező DTS-HD MA, FLAC vagy E-AC-3 hirdetése blokkoló hiba.
- Pontosan egy megtartott hangsáv lesz alapértelmezett. Ha nincs kijelölve, a rendszer az első eredeti/fő mixet választja; megtartott eredeti mix mellett dub nem lehet alapértelmezett.
- A régi `.bdencode-owner.json` 1-es séma ugyanahhoz a jobhoz még elfogadott, majd személyes jobazonosító nélküli 2-es sémára íródik át.

A selection pontos alakját az [API contract](API.md#selection), az üzemeltetési frissítést a [README](../README.md#11-frissítés) írja le.

## Kódoló- és muxpolitika

- Az x264 kimenet képkockasebességből számított, körülbelül 10 másodperces GOP-ot és 1 másodperces minimum keyframe-távolságot kap (`24000/1001` esetén `240/24`, 25 fps-nél `250/25`).
- A kompatibilis x264 High@4.1 terv automatikus `62500/78125` kb/s VBV-korlátot kap. A referencia-frame számot a tényleges, crop utáni makroblokk-geometria és a Level 4.1 DPB-határa szabja meg; teljes 1080p-n az 5 ref ezért 4-re csökkenhet.
- Az x264 kimenet PQ/HLG transferrel fail-closed tiltott; statikus HDR10 továbbra is kizárólag 10 bites x265 Main 10 módban, forrásazonos mastering/CLL/FALL metaadattal készülhet.
- Az x264 effektív chroma QP offset `-2`. A grain profil egységes alapértékei: `qcomp=0.75`, `aq-strength=0.65`, `deblock=-2:-2`, `psy-rdoq=0.15`, ha ezeket a felhasználó nem írta felül.
- Az IVTC tényleges kimeneti rátája a forrás `4/5`-e, a safe-bob hibrid módé a kétszerese; ezt használja a GOP-, level- és időzítési terv is.
- A Blu-ray LPCM 16/24 bites mélysége megmarad a referencia-remuxban és bekerül az effektív hangpolitikába.
- A worker a referencia, az encode-olt videó és minden sidecar kezdő PTS-ét megméri. A mux sávonkénti sync offsettel, a kodek-primingot is figyelembe véve közös nulla-idővonalat készít; a végső MKV start time és relatív sávkezdet külön gate.
- A videó mindig default track. A sávok nyelve, neve, default/forced jelzője és sorrendje a végső konténerazonosítással visszaellenőrződik.

## Új hard gate-ek

### Forrás és crop

- Hiányzó videóméret, képkockasebesség vagy érvénytelen címhossz review-t kér.
- A referencia-remux és a forrás videó bitfolyama fail-fast integritásvizsgálaton megy át; packet corruption, PES/timestamp és parser/demux hibával az encode nem indul el.
- A crop-QC a teljes címet szekvenciálisan dekódolja, és a `cropdetect` minden képkockát megvizsgál a stabil, illetve változó képarány kereséséhez. A modális crop mellett a teljes menet konzervatív burkolata is kötelező, ezért egy rövid köztes full-frame/IMAX rész sem vágható le automatikusan.
- A kiválasztott crop nem hagyhat legalább 8 px stabil fekete sávot, és a detektált aktív képbe legfeljebb 2 px biztonsági margóval vághat bele.

### Videó és comparison

- A végső MKV teljes videó- és hangdekódolása, kodek-/profil-/level-/pixel-format-/szín-/HDR10- és track-topológia-ellenőrzése továbbra is kötelező.
- Az alapértelmezett comparison 24, a címen elosztott source/encode pár: 8 I-, 8 P- és 8 B-frame. A GOP-ba történő keresés 12 másodperces prerollt használ; más frame-típus észrevétlenül nem helyettesítheti a kért kategóriát.
- A PNG csak vizuális bizonyíték és annotált release-melléklet. Az SSIM/PSNR a natív YUV/Y4M síkokon készül, nem az RGB-vé alakított képen.
- Blokkoló küszöbök: minden SSIM legalább `0.93`, SSIM-átlag legalább `0.95`; minden PSNR legalább `35 dB`, PSNR-átlag legalább `38 dB`; a B-frame SSIM-átlaga legfeljebb `0.03` értékkel maradhat el a P-frame átlagtól.
- A veszteséges encode videócsomagjainak összes payloadja legalább `0.1%`-kal kisebb kell legyen a forrásénál. A konténerméret és a mellékletek ezt a mérést nem torzíthatják.
- A referencia pontos képkockaszáma, a végső MKV videópacket-száma, a packet-timeline-ból mért végső videóhossz és a playlist időtartama legfeljebb két képkocka toleranciával egyezhet. Minden videó-PTS-nek egyedinek kell lennie és legfeljebb 1 ms eltéréssel a racionális CFR-rácsra kell esnie.
- A mux előtt és után a tömörített videó, valamint minden audio- és felirat-sidecar payload SHA-256 hashének bitazonosnak kell lennie. Emellett a teljes packet-sorrend `PTS/DTS/duration/size` lenyomata is egyezik; csak a mux által szándékosan alkalmazott közös sync offset normalizálható ki. Copy sávnál ugyanez a lánc már a reference → sidecar kinyerést is ellenőrzi.
- A comparison teljes hard időkerete 30 perc; túllépésnél a rendszer review-t kér.

### Hang és felirat

- Minden hangnál teljes sávos EBU R128/true-peak és `astats` elemzés készül. Az új, romló vagy nem igazolt clipping, valamint minden NaN vagy Inf minta blokkol; a dekódolt PCM SHA-256-tal igazolt, lossless módon örökölt clipping és a denormal minta külön auditfigyelmeztetés.
- Veszteséges átkódolásnál a kimeneti true peak nem lehet `0 dBTP` felett. Ha a forrás legalább `-1 dBTP`, a növekedés legfeljebb `0.3 dB` lehet.
- Copy és FLAC esetén megmarad a dekódolt PCM-hash és topológia gate; veszteséges céloknál a kodek, bitráta, 48 kHz, csatornaszám és időzítés az effektív presethez mérődik. A hossz bizonyítéka mindig a kiválasztott audiósáv teljes packet-tailje; a konténer teljes időtartama nem használható helyette.
- Minden nem-COPY audió teljes, countolt decoded-frame menetet kap. A `PTS + nb_samples/sample_rate` mintakurzor frame-enként kizárja a belső rést és átfedést; a source → sidecar és source → final összminta- és normalizált végpont-egyezés külön hard gate. A nagy nyers frame-riportok csak a privát work könyvtárban élnek a checkpoint folytatásáig, sikeres lezáráskor törlődnek.
- A felirat-sidecar kinyerése fail-fast, majd packet count-, start time- és duration-próbát kap. Forced besorolás review-t kér, ha egyszerre több mint 500 eseménye van és a cím több mint 50%-át lefedi; hiányos mérésből nem állítható forced flag. A végső MKV minden feliratsávja külön, teljes `show_frames` dekódolási vizsgálaton is átmegy; üres vagy hibás időzítésű esemény nem fogadható el.

Az ellenőrzési rétegek részletes működése az [Architecture](ARCHITECTURE.md#video-evidence) dokumentumban található.

## Publikus kimenet és adatvédelem

A `completed/<release>/` könyvtár publikus release-csomagja most csak az alábbiakat tartalmazza:

- a végleges MKV-t;
- a `comparison/` vizuális bizonyítékait, metrikáit és BBCode-ját;
- az `analysis/audio-comparison.json` fájlt és az audió spektrumképeket;
- a rejtett, 2-es sémájú tulajdonosi rekordot, benne a kimeneti névvel és az MKV SHA-256 hashével.

Az MKV nem tartalmaz csatolmányt, `BDENCODE_JOB`/`SETTINGS` globális taget vagy encode naplót. A publikus mappába nem kerül job UUID, forrás- vagy hostútvonal, parancssor, nyers/tisztított napló, teljes manifest, MediaInfo/MKVInfo vagy forrásanalízis. Ezek a privát job-munkatérben és az alkalmazás artifact-tárában maradnak auditálhatóan; az újrapróbálkozások stderr naplói külön `attempt-NN` fájlban őrződnek meg.

A publikálás előtt a worker friss SHA-256-tal újraellenőrzi a mux-, audio-QC- és comparison-checkpoint minden nyilvános kimenetét, majd ugyanazokat a producer által rögzített méret+hash értékeket használja a staging másolás előtt és után is. A metrika- és spektrumfájlok kizárólag a manifest pontos név+hash rekordja alapján kerülhetnek a csomagba; glob vagy utólag becsúsztatott fájl nem. A job/completed gyökerek, minden dinamikus alkönyvtár, az owner rekord, az MKV, valamint a stdout/stderr/audit/progress célok symlink/junction útvonala fail-closed.

Ez breaking változás minden olyan külső automatizmusnak, amely korábban a `completed` mappából olvasott naplót vagy manifestet, illetve az MKV-ba ágyazott `encode.log` fájlt várta. Ilyen integrációhoz az API artifact-végpontját vagy a privát job-munkateret kell használni.
