# BDEncode – Blu-ray és UHD Blu-ray kódoló rendszer

A BDEncode egy böngészőből kezelhető, sorba rendezett kódolórendszer érintetlen Blu-ray és UHD Blu-ray lemezkönyvtárakhoz. A forrásokat átvizsgálja, segít kiválasztani a megfelelő filmváltozatot, hangsávokat és feliratokat, majd elkészíti a videót, a hangot, az MKV-t, a minőség-ellenőrzést, a comparison képeket és a részletes naplókat.

Ez a dokumentum szándékosan részletes. Olyan felhasználónak is végigkövethető, aki még nem használt Linuxot, WSL-t, Git-et vagy parancssort.

> [!IMPORTANT]
> A BDEncode fejlesztés alatt áll. Első használatkor érdemes egy rövidebb vagy kevésbé fontos lemezzel próbát végezni, és az elkészült MKV-t lejátszással is ellenőrizni.

> [!NOTE]
> A 2.1 tartós pause/folytatás/cancel vezérlést, tárhely-karbantartást és ellenőrzött torrent/release-előkészítést vezet be. Frissítés előtt olvasd el a [BDEncode 2.1 kiadási jegyzetet](docs/RELEASE_2_1.md). A [2.0 kiadási jegyzet](docs/RELEASE_2_0.md) történeti dokumentumként továbbra is elérhető.

## Tartalomjegyzék

- [1. Mire képes a rendszer?](#1-mire-képes-a-rendszer)
- [2. Fontos fogalmak](#2-fontos-fogalmak)
- [3. Telepítés Windows 10/11-re](#3-telepítés-windows-1011-re)
- [4. Telepítés Debian szerverre](#4-telepítés-debian-szerverre)
- [5. Képfeltöltő és release szolgáltatások beállítása](#5-képfeltöltő-és-release-szolgáltatások-beállítása)
- [6. A telepítés ellenőrzése](#6-a-telepítés-ellenőrzése)
- [7. Első kódolás lépésről lépésre](#7-első-kódolás-lépésről-lépésre)
- [8. A várólista és az állapotok](#8-a-várólista-és-az-állapotok)
- [9. Naplók, elemzések és comparison](#9-naplók-elemzések-és-comparison)
- [10. Gyakori hibák és javításuk](#10-gyakori-hibák-és-javításuk)
- [11. Frissítés](#11-frissítés)
- [12. Eltávolítás Linux vagy szerver esetén](#12-eltávolítás-linux-vagy-szerver-esetén)
- [13. Eltávolítás Windows esetén](#13-eltávolítás-windows-esetén)
- [14. Haladó üzemeltetési tudnivalók](#14-haladó-üzemeltetési-tudnivalók)
- [15. Fejlesztés és tesztelés](#15-fejlesztés-és-tesztelés)

## 1. Mire képes a rendszer?

A főbb funkciók:

- normál Blu-ray AVC, VC-1 vagy MPEG-2 forrás feldolgozása x264/AVC kimenettel;
- UHD Blu-ray HEVC forrás feldolgozása x265/HEVC kimenettel, statikus HDR10 megtartásával;
- film, koncert, anime és sorozatlemez kezelése;
- több filmváltozat vagy playlist esetén grafikus választás;
- hangsávok és feliratok kiválasztása, nyelvének felülbírálása;
- sávonként `copy`, `flac`, `ac3`, `eac3`, `dts` vagy `omit` hangművelet;
- rögzített minőségi hangprofilok: AC-3 640 kb/s, E-AC-3 1024 kb/s és DTS core 1536 kb/s, 48 kHz-en, legfeljebb 5.1 csatornával;
- DTS-HD forrásnál lehetőség szerint újrakódolás nélküli DTS core kinyerés, TrueHD vagy más forrásnál ellenőrzött DTS-kódolás;
- Blu-ray LPCM esetén a Matroska által nem támogatott bitstream-copy helyett FLAC/AC-3/E-AC-3/DTS átalakítás vagy elhagyás;
- Kezdő, Haladó és Profi beállítási szint;
- egyszerre egy teljes kódolás, miközben további lemezek előkészíthetők és sorba állíthatók;
- legfeljebb a gép logikai CPU-kapacitásának beállított hányadát használó worker;
- tartós pause-kérés, worker-visszaigazolás és biztonságos folytatás a webes felületről;
- megszakított vagy hibás munka biztonságos folytatása;
- hibás vagy megszakított munka teljes törlése az ideiglenes fájlokkal együtt;
- tárhely-előnézet és a befejezett jobok ideiglenes munkaterületének célzott takarítása;
- teljes dekódolási video QC, valamint codec-, profil-, szín- és HDR10-ellenőrzés;
- veszteségmentes hangnál PCM-hash, veszteséges hangnál célkodek-, bitráta-, mintavétel-, csatorna- és időzítés-ellenőrzés;
- alapértelmezetten 24, a címen elosztott, veszteségmentes PNG comparison képpár;
- I-, P- és B-frame összehasonlítás azonos képtípusok között;
- a képeken forrásmegjelölés, képkockaszám és frame-típus;
- hangosság-, fázis- és spektrális hangelemzés;
- ImgBB, Catbox és Freeimage képfeltöltés, hibánál tartalék szolgáltatóval;
- BBCode készítése;
- MPLS/CLPI/PMT nyelvi adatok összesítése, ismeretlen hangnál választható CPU-s beszédfelismerési segítség és bizonytalanságnál kézi ellenőrzés;
- privát nyers és tisztított napló; a publikus MKV nem kap naplót vagy más csatolmányt, a comparison külön sidecar marad;
- privát v1 torrent, upload kit, dupe check, qBittorrentbe leállítva felvétel és explicit jóváhagyású tracker-feltöltés;
- napi automatikus alkalmazás- és eszközfrissítés.

A rendszer nem támogatja:

- a 3D Blu-ray megtartását;
- a Dolby Vision és a HDR10+ dinamikus metaadat megtartását;
- egyszerre több teljes encode futtatását;
- GPU-s kódolást. A rendszer CPU-val dolgozik, ezért kijelző vagy videokártya nélküli szerveren is használható.

## 2. Fontos fogalmak

### Forrásmappa

Az a mappa, ahol az érintetlen lemezek találhatók. Egy teljes lemez általában `BDMV` és `CERTIFICATE` mappát tartalmaz. A telepítő és a BDEncode ezt a forrást nem törli és nem módosítja.

### Munkamappa

Az alkalmazás saját területe. Linuxon alapértelmezetten:

```text
~/encode
```

Itt található az alkalmazás, a várólista adatbázisa, az ideiglenes fájlok, a naplók és az elkészült munkák.

### WSL2

A Windows Subsystem for Linux lehetővé teszi Linux programok futtatását Windows alatt. A Windows-telepítés során a BDEncode egy Debian WSL2 környezetben fut; a weboldalt továbbra is a Windows böngészőjéből kell megnyitni.

### Job vagy munka

Egy kiválasztott lemezhez tartozó teljes feldolgozás. Egy job tartalmazza a forrást, a playlistet, a sávválasztást, a kodekbeállításokat, a munkafájlokat és az eredményeket.

### Előkészítés és teljes feldolgozás

Az előkészítés a lemez gyorsabb beolvasása és a beállítások összeállítása. Ez egy másik encode futása közben is elvégezhető. A teljes feldolgozás a videókódolástól a comparison befejezéséig tart, és egyszerre csak egy ilyen folyamat futhat.

## 3. Telepítés Windows 10/11-re

### 3.1. Követelmények

Szükséges:

- 64 bites Windows 10 2004 vagy újabb, illetve Windows 11;
- rendszergazdai jogosultság;
- bekapcsolható hardveres virtualizáció;
- működő internetkapcsolat a telepítés alatt;
- legalább 100 GB szabad hely ajánlott a Windows rendszermeghajtón a WSL számára; teljes lemezes munkákhoz ennél lényegesen több is kellhet;
- külön elegendő hely a források és a kész fájlok számára;
- egy helyi meghajtóbetűjeles forrásmappa, például `D:\Filmek`.

> [!NOTE]
> A közvetlen `\\szerver\megosztas` UNC útvonalat a Windows-telepítő nem fogadja el. Az egykattintásos telepítéshez helyi meghajtóbetűjeles mappát használj. Hálózati forrást haladó módon, a WSL alatt kell felcsatolni és a Linux-telepítőnek átadni.

### 3.2. A projekt letöltése

#### Egyszerű módszer: ZIP

1. Nyisd meg a projekt GitHub-oldalát.
2. Kattints a **Code**, majd a **Download ZIP** lehetőségre.
3. Csomagold ki egy állandó helyre, például:

   ```text
   C:\BDEncode
   ```

4. Ne futtasd közvetlenül a ZIP-fájlból.

#### Git használatával

Ha a Git már telepítve van, nyiss PowerShellt, és futtasd:

```powershell
cd C:\
git clone --branch main https://github.com/takachlaszlo/bdencode-backend.git BDEncode
cd C:\BDEncode
```

### 3.3. A telepítő elindítása

1. Nyisd meg a kicsomagolt projekt `install` mappáját.
2. Kattints jobb gombbal a `windows-install.cmd` fájlra.
3. Válaszd a **Futtatás rendszergazdaként** lehetőséget.
4. Az UAC kérdésnél válaszd az **Igen** gombot.

Parancssorból ugyanez:

```powershell
cd C:\BDEncode
.\install\windows-install.cmd
```

A telepítő:

1. ellenőrzi vagy telepíti a WSL2-t;
2. szükség esetén bekapcsolja a virtualizációs Windows-összetevőket;
3. telepíti a Debian disztribúciót;
4. létrehozza a Linux felhasználót;
5. megkéri a forrásmappa kiválasztására;
6. telepíti a médiaprogramokat és a BDEncode-ot;
7. létrehozza a háttérben futó szolgáltatásokat;
8. létrehozza az asztali parancsikonokat;
9. megnyitja a webes felületet.

Az első telepítés a médiaprogramok fordítása miatt hosszabb ideig tarthat. Ne zárd be az ablakot csak azért, mert néhány percig nem jelenik meg új sor.

### 3.4. Ha újraindítást kér

Ez az első WSL-telepítéskor normális.

1. Nyomj Entert.
2. Indítsd újra a számítógépet.
3. Jelentkezz vissza ugyanabba a Windows-fiókba.
4. A telepítő automatikusan folytatódik.
5. Ha nem indulna el, futtasd ismét rendszergazdaként a `windows-install.cmd` fájlt. A telepítő felismeri a korábban elkezdett állapotot.

### 3.5. A forrásmappa kiválasztása

A mappaválasztóban azt a gyökérmappát add meg, amely alatt a lemezek külön almappákban találhatók. Példa:

```text
D:\Filmek\Film.Egy\BDMV
D:\Filmek\Film.Ketto\BDMV
```

Ebben az esetben a kiválasztandó gyökér:

```text
D:\Filmek
```

A Windows `D:\Filmek` útvonala WSL alatt jellemzően `/mnt/d/Filmek` formában jelenik meg. Ezt a telepítő automatikusan átalakítja.

### 3.6. Sikeres telepítés

Siker esetén a telepítő többek között ezt írja ki:

```text
BDEncode Windows/WSL installation is healthy.
Web: http://localhost:8787/encoder/
```

A kezelőfelület címe:

```text
http://localhost:8787/encoder/
```

Az asztalon két parancsikon jelenhet meg:

- **BDEncode** – megnyitja a webes kezelőfelületet;
- **BDEncode elkészült filmek** – megnyitja az elkészült fájlok Windowsból elérhető mappáját.

### 3.7. Fontos Windows útvonalak

| Tartalom | Hely |
|---|---|
| Telepítési napló | `%LOCALAPPDATA%\BDEncode\install.log` |
| WSL-életben tartó szkript | `%LOCALAPPDATA%\BDEncode\keepalive.ps1` |
| Debian WSL adatai | `%LOCALAPPDATA%\BDEncodeWSL\Debian` |
| Weboldal | `http://localhost:8787/encoder/` |
| WSL-en belüli munkaterület | `/home/<linux-felhasznalo>/encode` |
| WSL-en belüli kész munkák | `/home/<linux-felhasznalo>/encode/completed` |

## 4. Telepítés Debian szerverre

### 4.1. Követelmények

- Debian 12 `bookworm` vagy Debian 13 `trixie`;
- normál felhasználói fiók;
- a felhasználó használhassa a `sudo` parancsot;
- működő internetkapcsolat;
- nginx, ha a webes felületet reverse proxyn keresztül akarod használni;
- alapértelmezetten `~/storage` forrásmappa és `~/encode` munkamappa.

Ne `root` felhasználóként futtasd a telepítőt. A telepítő maga kér `sudo` jogosultságot azokhoz a lépésekhez, amelyekhez szükséges.

### 4.2. Csatlakozás és alapcsomagok

Jelentkezz be SSH-val, majd futtasd külön sorokban:

```bash
sudo apt-get update
sudo apt-get install -y git tmux
```

### 4.3. A projekt letöltése

```bash
cd ~
git clone --branch main https://github.com/takachlaszlo/bdencode-backend.git
cd ~/bdencode-backend
```

Ha a mappa már létezik:

```bash
cd ~/bdencode-backend
git fetch origin
git switch main
git pull --ff-only
```

### 4.4. Alapértelmezett telepítés

Az alapértelmezett útvonalak:

- forrás: `~/storage`;
- munka és adatok: `~/encode`;
- CPU-korlát: 80%;
- belső API: `127.0.0.1:8796`.

Telepítés:

```bash
cd ~/bdencode-backend
bash install/install.sh
```

### 4.5. Egyedi forrás- vagy munkamappa

Példa `/storage` forrással és a felhasználó saját `~/encode` munkamappájával:

```bash
cd ~/bdencode-backend
BDENCODE_SOURCE_ROOT=/storage \
BDENCODE_DATA_ROOT="$HOME/encode" \
BDENCODE_CPU_PERCENT=80 \
bash install/install.sh
```

Fontos:

- az útvonal legyen abszolút;
- a futtató felhasználónak olvasnia kell a forrást;
- a munkamappába írnia is kell;
- a telepítő megtagadja a nem biztonságos, túl tág vagy szimbolikus linkekkel félreérthető célokat;
- a forrás és a munkamappa ne fedje egymást.

### 4.6. Telepítés tmux alatt

SSH-kapcsolat megszakadásakor a `tmux` munkamenet tovább él:

```bash
tmux new -s bdencode-install
cd ~/bdencode-backend
bash install/install.sh
```

Leválás a futó munkamenetről: nyomd meg a `Ctrl+B`, majd a `D` billentyűt.

Visszacsatlakozás:

```bash
tmux attach -t bdencode-install
```

### 4.7. Swizzin és nginx

Swizzin telepítésnél a telepítő felismeri a szokásos nginx-struktúrát, és létrehozza az `/encoder/` útvonalhoz szükséges konfigurációt. Külső elérésnél használd a szerver HTTPS-címét, például:

```text
https://sajat-domain.example/encoder/
```

A `127.0.0.1:8796` belső API-portot nem kell közvetlenül kitenni az internetre.

## 5. Képfeltöltő és release szolgáltatások beállítása

A comparison képek feltöltéséhez a rendszer az alábbi szolgáltatókat ismeri:

1. ImgBB;
2. Catbox;
3. Freeimage.

Nem kötelező mindhármat beállítani, de legalább kettő ajánlott. Ha az első szolgáltató átmenetileg hibázik, a rendszer megpróbálhatja a következőt.

### 5.1. A titkok kezelésének szabályai

- API-kulcsot ne írj a README-be, Git commitba vagy képernyőképre.
- Ne add meg nyíltan parancssori argumentumként, mert bekerülhet a shell előzményeibe.
- A BDEncode titkosított systemd credential fájlokat használ.
- A három credential neve: `imgbb-api-key`, `catbox-userhash`, `freeimage-api-key`.

### 5.2. Biztonságos, interaktív beállítás Debian alatt

Az alábbi függvény bekéri a titkot úgy, hogy a begépelt érték nem látszik. Másold be egyszer a teljes blokkot:

```bash
install_bdencode_secret() {
    credential_name="$1"
    credential_dir="$HOME/.config/bdencode"
    temporary_file="$(mktemp)"
    mkdir -p "$credential_dir"
    chmod 700 "$credential_dir"
    read -r -s -p "$credential_name értéke: " credential_value
    printf '\n'
    printf '%s' "$credential_value" > "$temporary_file"
    unset credential_value
    systemd-creds encrypt \
        --name="$credential_name" \
        "$temporary_file" \
        "$credential_dir/$credential_name.cred"
    rm -f "$temporary_file"
    chmod 600 "$credential_dir/$credential_name.cred"
}
```

Ezután csak azt futtasd, amelyik szolgáltatáshoz van azonosítód:

```bash
install_bdencode_secret imgbb-api-key
install_bdencode_secret catbox-userhash
install_bdencode_secret freeimage-api-key
```

Végül futtasd újra a telepítőt, hogy a szolgáltatások biztosan megkapják a credential fájlokat:

```bash
cd ~/bdencode-backend
bash install/install.sh
```

Windows alatt előbb lépj be a Debian környezetbe:

```powershell
wsl -d Debian
```

Ezután a megjelenő Linux parancssorban használd a fenti Linux-parancsokat.

### 5.3. Trackerprofil és qBittorrent beállítása

A 2.1 release-előkészítése alapértelmezetten ki van kapcsolva: a telepítő egy üres, root által kezelt profilfájlt hoz létre. Az aktív fájl helye:

```text
/etc/bdencode/release-profiles.json
```

Kiindulási mintának a repository [release-profiles.example.json](config/release-profiles.example.json) fájlját használd. Másold át az aktív helyre, majd állítsd be a tracker saját adatait. A teljes profilfájlt titokként kezeld: root által olvasható konfiguráció, amely policyt, hálózati beállítást és privát tracker announce URL-t is tartalmazhat:

- stabil `profile_id`, megjelenítési név és torrent `source` token;
- HTTPS announce URL-ek; a tracker személyes passkeyt tehet az útvonalba vagy a querybe, ezért ezek nem tekinthetők credential nélkülinek;
- darabméret- és képszám-korlátok;
- fix, credential nélküli HTTPS dupe-check és publish endpoint, külön host-allowlisttel;
- opcionális qBittorrent base URL és host-allowlist. Titkosítatlan HTTP csak loopback címen engedélyezett.

API-token, qBittorrent-felhasználónév vagy jelszó soha ne kerüljön a JSON-ba. A dupe/publish API hitelesítési titka és a qBittorrent hitelesítési adatai a 5.2. pontban bemutatott `systemd-creds` eljárással készüljenek. Az alapértelmezett qBittorrent credentialnevek:

```bash
install_bdencode_secret qbittorrent-username
install_bdencode_secret qbittorrent-password
```

A tracker token credentialnevének pontosan egyeznie kell a profil `tracker.credential_name` mezőjével. A telepítő a fix `tracker-aither-api-token` nevet automatikusan átadja az API szolgáltatásnak. Egyedi trackercredentialhoz ne módosítsd a telepítő által kezelt `credential.conf` fájlt, mert frissítéskor felülíródik. Hozz létre külön, üzemeltető által kezelt drop-int:

```ini
# /etc/systemd/system/bdencode-api.service.d/tracker-local.conf
[Service]
LoadCredentialEncrypted=egyedi-tracker-token:/home/FELHASZNALO/.config/bdencode/egyedi-tracker-token.cred
```

Ezután futtasd a `sudo systemctl daemon-reload` parancsot, indítsd újra a `bdencode-api.service` szolgáltatást, majd ellenőrizd a rendszert a `bdencode doctor --json` paranccsal. A `tracker-local.conf` szándékosan nem telepítő által kezelt fájl; az üzemeltető felelőssége a karbantartása és eltávolítása.

### 5.4. Release-előkészítés biztonsági modellje

A művelet csak sikeresen befejezett, tulajdonosi rekorddal és SHA-256 hashsel kötött MKV-ból indulhat. A torrent privát v1 torrent, és pontosan egy payloadot ír le:

```text
Release.Name/Release.Name.mkv
```

A comparisonból csak ellenőrzött, encode-oldali képek kerülnek az upload kitbe. A torrent, MediaInfo, NFO, BBCode, checksumok, upload-kérés és képek az alkalmazás privát `release-kits/<preparation-id>/` területén maradnak; nem kerülnek a publikus completed mappába. Az announce URL miatt a kit és maga a torrent is titkos, root/alkalmazás által védett adat. Exportkor a backend a manifesthez kötött torrentet stabilan, korlátozott méretben memóriába olvassa, újraellenőrzi, és `private, no-store` válaszban adja át; a HTTP-válasz már nem egy később újranyitott fájlra hivatkozik.

A qBittorrent-integráció leállítva adja hozzá a torrentet, majd teljes rechecket kér; a torrentet nem indítja el automatikusan. A backend az ellenőrzött torrent byte-jait és elvárt infohashét adja át, és job–profil–infohash szintű kizárólagos claim akadályozza meg, hogy két párhuzamos preparation ugyanazt a távoli add műveletet kétszer indítsa el. Sikeres felvétel után nincs automatikus seed-retry. Trackerfeltöltéskor a korábbi receipt önmagában nem elég: közvetlenül az upload claim előtt új távoli dupe checknek kell ugyanahhoz a manifesthez kötött `CLEAR` eredményt adnia. Jobonként és trackerprofilonként egyszerre csak egy aktív, sikeres vagy bizonytalan publikálási kimenet lehet. A dupe check, a qBittorrent-felvétel és a publish közvetlenül a távoli kérés előtt újraellenőrzi a trackerprofil digestjét, valamint a completed payload tulajdonosi rekordját, útvonalát, méretét és hashét. Ha egy távoli művelet kimenetele bizonytalan (`UNKNOWN`), a rendszer nem próbálkozik automatikusan újra: előbb a trackerben vagy qBittorrentben kézzel ellenőrizd a tényleges állapotot.

## 6. A telepítés ellenőrzése

### 6.1. Windows

PowerShellben:

```powershell
wsl --list --verbose
Get-ScheduledTask -TaskName "BDEncode WSL"
curl.exe --noproxy "*" http://127.0.0.1:8787/encoder/
```

Elvárt eredmény:

- a `Debian` disztribúció VERSION oszlopa `2`;
- a `BDEncode WSL` ütemezett feladat létezik;
- a `curl` HTML-t kap, nem kapcsolódási hibát.

A szolgáltatások ellenőrzése:

```powershell
wsl -d Debian -- systemctl is-active bdencode-api.service
wsl -d Debian -- systemctl is-active bdencode-worker.service
wsl -d Debian -- systemctl is-active nginx.service
```

Mindháromnál az `active` válasz az ideális.

### 6.2. Debian szerver

```bash
systemctl is-active bdencode-api.service
systemctl is-active bdencode-worker.service
systemctl is-enabled bdencode-update.timer
sudo nginx -t
```

Részletes rendszerdiagnosztika:

```bash
"$HOME/encode/app/current/venv/bin/bdencode" doctor --json
```

Ha egyedi `BDENCODE_DATA_ROOT` értéket használtál, a parancsban a `$HOME/encode` részt cseréld ki arra.

### 6.3. A weboldal nem tölt be azonnal

A telepítés végén a szolgáltatásoknak néhány másodperc kellhet. Várj 10–20 másodpercet, majd frissítsd az oldalt. Ha továbbra sem működik, lásd a [hibaelhárítási fejezetet](#10-gyakori-hibák-és-javításuk).

## 7. Első kódolás lépésről lépésre

### 7.1. Új munka létrehozása

1. Nyisd meg a BDEncode weboldalát.
2. Válaszd az **Új kódolás** gombot.
3. Válaszd ki a forrásmappát.
4. Add meg a tartalomtípust: film, koncert, anime vagy sorozat.
5. Első alkalommal válaszd a **Kezdő** munkamódot.
6. Indítsd el a lemez beolvasását.

A scan még nem indítja el a hosszú videókódolást. Csak feltérképezi a lemezt és előkészíti a választási lehetőségeket.

### 7.2. Playlist és filmváltozat választása

Ha több hasonló hosszúságú playlist található, a rendszer több filmváltozatot jelezhet. Ilyenkor ellenőrizd:

- a játékidőt;
- a fejezetek számát;
- a videófelbontást és képkockasebességet;
- a hangsávok számát;
- hogy moziváltozat, rendezői változat vagy más vágás-e.

Ne csak a legnagyobb playlistet válaszd automatikusan, mert egyes lemezek hamis vagy összefűzött playlisteket tartalmazhatnak.

### 7.3. Hangsávok és feliratok

Minden megtartandó sávnál ellenőrizd:

- nyelv;
- kodek;
- csatornaszám;
- megjegyzés vagy cím;
- alapértelmezett és forced jelző.

Minden megtartott feliratot külön `full` vagy `forced` típusba is be kell sorolni. A 2.0 nem veszi át automatikusan a forrás forced jelzőjét, ha a tartalom besorolása nincs felülvizsgálva.

Ha a lemez nem tartalmaz megbízható nyelvkódot, a rendszer javaslatot adhat, de a felületen kézzel felülbírálható. Bizonytalan esetben rövid mintát kell meghallgatni vagy a feliratot meg kell nyitni.

A hangművelet lehet például:

- eredeti formátum változtatás nélküli megtartása;
- FLAC;
- DTS-HD/TrueHD mag vagy kompatibilis DTS kimenet, ha az eszközök és a kiválasztott profil engedi;
- DTS → AC3;
- más, a felületen felkínált kompatibilis átalakítás.

Az átalakítás veszteséges lehet. Ha nincs kompatibilitási vagy méretprobléma, az eredeti veszteségmentes hangsáv megtartása a legbiztonságosabb.

### 7.4. Videóbeállítások

- Normál BD esetén az alapértelmezett választás x264.
- UHD esetén az alapértelmezett választás x265 és HDR10.
- Dolby Vision réteg nem marad meg.
- 3D tartalom nem támogatott.

Kezdő módban a rendszer biztonságos alapértékeket ad. Haladó és Profi módban több x264/x265 paraméter külön állítható. Ha nem tudod pontosan, mit jelent egy paraméter, hagyd a profil ajánlott értékén.

### 7.5. Terv ellenőrzése és sorba állítása

1. Nézd át a kiválasztott playlistet és sávokat.
2. Add meg a kimeneti fájlnevet.
3. Kattints a **Terv ellenőrzése** gombra.
4. Javítsd a pirossal jelzett kötelező hibákat.
5. Ha csak figyelmeztetés maradt, olvasd el, és szükség esetén erősítsd meg.
6. Hagyd jóvá a tervet.

A jóváhagyott munka **Indításra kész** állapotba kerül. Ha másik encode fut, nem indul el azonnal: szabályosan várakozik a sorban. Közben további lemezeket is beolvashatsz és beállíthatsz.

### 7.6. A kész munka ellenőrzése

Az **Elkészült munkák** között ellenőrizd:

- az MKV meglétét és méretét;
- a MediaInfo és MKV-elemzés mellékletet;
- a videó QC eredményeit;
- a hang spektrumképeit;
- az alapértelmezett 24 comparison képpárt;
- a BBCode fájlt;
- a fő és szakaszonkénti naplókat.

Végül játssz le több részletet a filmből, különösen az elejét, a végét, egy fejezetváltást és több hangsávot/feliratot.

## 8. A várólista és az állapotok

A rendszer kétféle erőforrássávot kezel:

- egy scan/előkészítő sáv, amely a futó encode mellett is használható;
- egy teljes feldolgozási sáv, amelyből egyszerre pontosan egy futhat.

Ezért a helyes munkamenet:

1. az első job kódol;
2. közben a következő lemezt beolvasod;
3. kiválasztod a playlistet, sávokat és paramétereket;
4. jóváhagyod;
5. a job **Indításra kész** állapotban vár;
6. az előző teljes lezárása után automatikusan sorra kerül.

### Állapotok jelentése

| Állapot | Jelentés | Felhasználói teendő |
|---|---|---|
| `QUEUED` | Beolvasásra vár | Nincs |
| `SCANNING` | A lemez feltérképezése folyik | Várj |
| `AWAITING_SELECTION` | Választás vagy beállítás szükséges | Nyisd meg és állítsd be |
| `READY` | Minden jóváhagyva, teljes feldolgozásra vár | Nincs |
| `ENCODING` | Videókódolás folyik | Várj |
| `MUXING` | Az MKV összeállítása folyik | Várj |
| `QC` | Minőség-ellenőrzés folyik | Várj |
| `COMPARISON` | Kép- és hangelemzés készül | Várj; a hard időkeret 30 perc |
| `UPLOADING` | Comparison képek feltöltése folyik | Várj |
| `NEEDS_REVIEW` | Emberi döntés szükséges | Olvasd el a jelzést és folytasd |
| `UPLOAD_FAILED` | A képfeltöltés hibázott | Használd a **Feltöltés újra** gombot |
| `COMPLETED` | A munka teljesen elkészült | Ellenőrizd az eredményt |
| `FAILED` | Egy szakasz hibával leállt | Javítás után **Folytatás a hibától** |
| `CANCELLED` | A munkát megszakították | **Újraindítás** vagy **Munka törlése** |

### Megszakítás, folytatás és törlés

- A pipeline `state` mezője mellett külön `control_state` mutatja a kezelői vezérlést: `RUNNING`, `PAUSE_REQUESTED`, `PAUSED` vagy `CANCEL_REQUESTED`.
- A **Szüneteltetés** először tartós `PAUSE_REQUESTED` kérést ír az adatbázisba. A worker leállítja az adott szakasz folyamatait, biztonságosan eltávolítja a részleges kimenetet, és csak ezután igazolja vissza a `PAUSED` állapotot. Emiatt a gomb hatása nem feltétlenül azonnali, és a félbehagyott lépés folytatáskor újrafuthat.
- Csak a visszaigazolt `PAUSED` job engedi el a scan- vagy encode-sávját. Másik job ekkor sorra kerülhet; ezért a **Folytatás** `409` ütközést adhat, amíg a szükséges sáv foglalt.
- A **Folytatás** a meglévő pipeline-állapotot és az érvényes checkpointokat tartja meg. Ez nem azonos a `NEEDS_REVIEW` állapot jóváhagyásával vagy a `FAILED` job újrapróbálásával.
- A **Megszakítás** aktív folyamatnál `CANCEL_REQUESTED`, majd worker-visszaigazolás után `CANCELLED`. Már tétlen vagy szünetelő jobnál a lezárás azonnal, egy tranzakcióban megtörténhet.
- A **Folytatás a hibától** a meglevő érvényes checkpointokat használja; az **Újraindítás** a `CANCELLED` munkát teszi vissza a feldolgozásba.
- A `control_revision` és a job `version` optimista zárolása megakadályozza, hogy két megnyitott böngészőablak elavult gombnyomása felülírja egymást.

### Tárhely-előnézet, takarítás és törlés

A job tárhelynézete külön mutatja a privát munkaterületet és a publikus completed release-t. A **Takarítás** csak `COMPLETED` jobon, `temporary` scope-pal használható: a nagy ideiglenes `work` tartalmat karanténon keresztül törli, de az elkészült MKV-t és a publikus comparison bizonyítékokat megőrzi. A leválasztás szándéka és pontos célpontjai előbb SQLite maintenance journalba kerülnek; a fájlrendszer-mozgatás és a domain-adatbázis commit közötti processzhalál után az induláskori recovery determinisztikusan visszaállítja a nem commitolt, illetve végleg eltávolítja a commitolt karantént.

A **Munka törlése** csak terminális (`COMPLETED`, `FAILED` vagy `CANCELLED`) jobnál engedélyezett. Eltávolítja a job adatbázis-bejegyzését, privát munkaterületét és olyan release-előkészítési kitjeit, amelyekhez nem tartozik külső kimenetel, de a completed release-t mindig megőrzi. `UNKNOWN`, `PUBLISHED`, sikeres/bizonytalan qBittorrent-receipt vagy publication receipt auditrekordja nem törölhető egyszerű preparation- vagy jobtörléssel.

A publikus release törlése külön, erős megerősítést kérő művelet: a release nevének és az MKV SHA-256 hashének egyeznie kell, továbbá a kliensnek az összes preparation azonosítóját és aktuális verzióját tartalmazó pontos snapshotot is vissza kell küldenie. A backend ugyanabban az adatbázis-tranzakcióban újraellenőrzi ezt a halmazt, mielőtt a release-t és a kapcsolódó auditrekordokat leválasztja. Már seedelt vagy `UNKNOWN` kimenetel esetén külön force jóváhagyás szükséges.

> [!WARNING]
> A job és a release törlése nem visszavonható. Egyik sem módosítja az eredeti Blu-ray forrást; a completed release-t kizárólag a külön **Release törlése** művelet távolíthatja el.

### Torrent és feltöltési csomag készítése

`COMPLETED` jobon a **Release előkészítése** panelen válassz trackerprofilt, töltsd ki a release metaadatait, majd haladj az alábbi ellenőrzött lépéseken:

1. **Validate** – újraellenőrzi az MKV tulajdonosi rekordját, útvonalát, méretét, hashét, a profil digestjét és a comparison képeket.
2. **Build** – elkészíti a privát torrentet és a hash-manifesttel rögzített upload kitet.
3. **Export** – stabilan memóriába olvasott és a manifest/torrent policy szerint újraellenőrzött torrentet tölt le, böngésző- vagy proxycache nélkül.
4. **Dupe check** – a konfigurált tracker fix endpointján ellenőriz; csak `CLEAR` eredmény enged tovább.
5. **Seed** – opcionálisan leállítva hozzáadja qBittorrenthez és teljes rechecket kér. Az indítás továbbra is kézi döntés; a globális claim az ugyanahhoz a torrenthez tartozó párhuzamos addot is kizárja.
6. **Upload** – új, publikálásidejű `CLEAR` dupe check után, az aktuális manifest hashéhez kötött és a reverse proxy hitelesített felhasználóazonosságával auditált explicit jóváhagyással publikál.

A release-előkészítés saját tartós állapotgépet és verziót használ; böngészőfrissítés nem veszíti el a receipt-eket. Szolgáltatásindításkor a félbemaradt `PREPARING` művelet `FAILED` lesz és az árva build-staging kitakarítható, míg a félbemaradt `SEEDING_CHECK`, `SEEDING` vagy `PUBLISHING` művelet `UNKNOWN` állapotba kerül. Ezeket a hálózati műveleteket a rendszer nem ismétli meg automatikusan. `NEEDS_REVIEW`, `FAILED` vagy különösen `UNKNOWN` esetén ne ismételd vakon a távoli műveletet: előbb ellenőrizd a tracker/qBittorrent valós állapotát.

## 9. Naplók, elemzések és comparison

### 9.1. Mi marad meg?

A nagy ideiglenes `work` tartalom sikeres véglegesítés után eltávolítható; ha megmaradt, a tárhelynézetből célzottan takarítható. A publikus `completed/<release>/` csomagban megmaradnak:

- az elkészült MKV;
- a comparison PNG-k, metrikák és BBCode;
- az audió comparison és spektrumképek;
- a kimeneti nevet és MKV-hasht tartalmazó, jobazonosító nélküli tulajdonosi rekord.

A teljes munkanapló, a szakaszonkénti naplók, az alkalmazott x264/x265 és mux beállítások, a MediaInfo/MKV elemzés, a manifest és a részletes QC eredmények a privát job-munkatérben és az alkalmazás artifact-tárában maradnak. Nem kerülnek az MKV-ba vagy a publikus release-mappába.

Hibánál a szükséges munkafájlok szándékosan megmaradnak, hogy a job folytatható legyen. Ha nem akarod folytatni, előbb szakítsd meg vagy zárd le a jobot, majd használd a webes **Munka törlése** műveletet. Ez a completed release-t nem törli.

### 9.2. Videó comparison

A mintavételezett comparison célja nem a teljes film képkockánkénti átvizsgálása. A rendszer:

- alapértelmezetten 24, de konfigurálhatóan 20–50 képpárt készít;
- a címen elosztva azonos időponthoz és azonos frame-típushoz tartozó source/encode képet párosít;
- az alapértéknél 8 I-, 8 P- és 8 B-frame-et választ;
- veszteségmentes PNG-t használ;
- ráírja a forrást, a képkockaszámot és a frame-típust;
- a QC SSIM/PSNR értékeit külön, natív YUV síkokon számolja, ezért az annotáció és az RGB-konverzió nem módosítja a mérését;
- 30 perces teljes hard időkeretet használ, nem végez órákig tartó teljesfilm-metrikát.

Ha az azonos típusú pár technikailag nem állítható elő, a felület ezt egyértelműen jelzi. A strict I/P/B egyezés nem cserélhető le észrevétlenül más képtípusra. A blokkoló küszöböket a [2.0 kiadási jegyzet](docs/RELEASE_2_0.md#videó-és-comparison) sorolja fel.

### 9.3. Hang-összehasonlítás

A rendszer külön kezeli a forrás- és kimeneti hangot. A mellékletek között spektrális képek és technikai adatok találhatók. Veszteséges konverziónál a spektrum különösen hasznos, de önmagában nem bizonyítja a hallható minőséget; szükség esetén hallgatási próba is kell.

### 9.4. VMAF figyelmeztetés

Ha a diagnosztika ezt jelzi:

```text
FFmpeg libvmaf filter missing; the official standalone VMAF CLI will be used
```

az nem telepítési hiba. Az FFmpegből hiányzik a `libvmaf` szűrő, ezért a rendszer a telepített hivatalos önálló VMAF parancssori programot használja.

## 10. Gyakori hibák és javításuk

### 10.1. Hol keresd először a hibát?

Windows telepítési napló:

```text
%LOCALAPPDATA%\BDEncode\install.log
```

Gyors megnyitása PowerShellből:

```powershell
notepad "$env:LOCALAPPDATA\BDEncode\install.log"
```

Debian szolgáltatásnaplók:

```bash
journalctl -u bdencode-api.service -n 200 --no-pager
journalctl -u bdencode-worker.service -n 200 --no-pager
journalctl -u nginx.service -n 100 --no-pager
```

Élő worker-napló:

```bash
journalctl -u bdencode-worker.service -f
```

Kilépés az élő nézetből: `Ctrl+C`.

### 10.2. Hibatáblázat

| Jelenség vagy üzenet | Mit jelent? | Javítás |
|---|---|---|
| A telepítőablak rögtön bezáródik | Régi telepítő vagy korai PowerShell-hiba | Töltsd le a legfrissebb `main` ágat, csomagold ki teljesen, majd futtasd a `windows-install.cmd` fájlt rendszergazdaként. Az új telepítő hiba esetén Enterre vár. |
| „A Linuxos Windows-alrendszer nincs telepítve” | A WSL még nincs engedélyezve | Hagyd, hogy a telepítő bekapcsolja, majd indítsd újra a gépet és futtasd újra. |
| `WSL_E_VM_MODE_INVALID_STATE` | A Debian már létrejött, de a WSL2 konverzió még nem fejeződött be | Indítsd újra a Windowst, majd a friss telepítőt. A jelenlegi telepítő helyreállítja a félkész állapotot. |
| `invalid option name: pipefail` | Windows CRLF sorvég került a Bash szkriptbe | Régi telepítőhiba; frissítsd a repót és futtasd újra. A jelenlegi telepítő normalizálja a sorvégeket. |
| `sudo: python3: command not found` | A minimális Debianban még nincs Python | Régi telepítőhiba; a jelenlegi telepítő előbb telepíti a Python 3-at. Frissíts és futtasd újra. |
| `Command 'man apt(8)' failed with code 1` | A systemd unit ellenőrzése a hiányzó `man` programon bukott el | Régi telepítőhiba; frissíts és futtasd újra. A jelenlegi bootstrap telepíti a szükséges csomagot. |
| A telepítés sikeres, de a `localhost:8787` nem jön be | A WSL vagy nginx még nem fut, az ütemezett feladat hiányzik, vagy proxy zavar be | Várj 20 másodpercet, futtasd a 10.3. fejezet parancsait, majd ellenőrizd a szolgáltatásokat. |
| Átmeneti `curl: (7) Failed to connect` látszik telepítés közben | A health check gyorsabban indult, mint a szolgáltatás | Ha később `HEALTHY`, `COMMITTED` és sikeres webcím jelenik meg, nincs teendő. Ha a végső állapot piros, nézd meg a naplót. |
| `VapourSynth hiba` | A `vspipe` vagy egy szükséges plugin nem tölthető be | Futtasd a `doctor --json` parancsot, frissítsd a telepítést, majd nézd meg a worker naplóját. |
| ImgBB/Catbox/Freeimage credential nincs beállítva | Az adott képfeltöltő nem használható hitelesítve | Állítsd be az 5. fejezet szerint. A kódolás ettől még elkészülhet, de a feltöltés korlátozott vagy hibás lehet. |
| A forrás nem jelenik meg | Rossz gyökérmappa, jogosultsági hiba vagy közvetlen UNC útvonal | Ellenőrizd, hogy a gyökér alatt ténylegesen van `BDMV`, Windows alatt használj meghajtóbetűjelet. |
| `source color metadata is incomplete` | A lemez színinformációja hiányos vagy ellentmondásos | Nyisd meg a tervet, ellenőrizd a scan adatokat és erősítsd meg a helyes színprofilt. UHD-n ne hagyd figyelmen kívül automatikusan. |
| A comparison túl sokáig fut | Hibás vagy régi összehasonlító szakasz, nehezen található frame-pár | Frissítsd a rendszert. Az új gyors comparison időkorlátos. Ha review állapotba kerül, használd a folytatást. |
| `UPLOAD_FAILED` | Egyik képfeltöltő sem fogadta el a képeket | Ellenőrizd az internetet és credentialöket, majd nyomd meg a **Feltöltés újra** gombot. A videót nem kódolja újra. |
| `FAILED` vagy `CANCELLED`, és sok helyet foglal | A folytatáshoz megtartott checkpointok és munkafájlok foglalják a helyet | Folytasd a jobot, vagy válaszd a **Munka törlése** műveletet. Ne törölj kézzel fájlokat futó job alól. |
| Az uninstall azt írja, hogy aktív a várólista | Encode, scan vagy helyreállítás fut | Állítsd le vagy zárd le a munkát a weboldalon, várd meg a rendezett leállást, majd futtasd újra az eltávolítót. |

### 10.3. A Windows weboldal nem nyílik meg

Nyiss rendszergazdai PowerShellt, és futtasd sorrendben:

```powershell
wsl --list --verbose
Get-ScheduledTask -TaskName "BDEncode WSL"
Start-ScheduledTask -TaskName "BDEncode WSL"
wsl -d Debian -- systemctl restart bdencode-api.service bdencode-worker.service nginx.service
curl.exe --noproxy "*" http://127.0.0.1:8787/encoder/
```

Ezután nyisd meg:

```text
http://localhost:8787/encoder/
```

Ha a `Debian` nem szerepel a listában, a telepítés nem fejeződött be. Futtasd újra a legfrissebb Windows-telepítőt.

### 10.4. A szolgáltatás hibás

Windows PowerShellből lekérhető az utolsó 100 sor:

```powershell
wsl -d Debian -- journalctl -u bdencode-api.service -n 100 --no-pager
wsl -d Debian -- journalctl -u bdencode-worker.service -n 100 --no-pager
wsl -d Debian -- journalctl -u nginx.service -n 100 --no-pager
```

Újraindítás:

```powershell
wsl -d Debian -- systemctl restart bdencode-api.service
wsl -d Debian -- systemctl restart bdencode-worker.service
wsl -d Debian -- systemctl restart nginx.service
```

### 10.5. Kevés a szabad hely

Linuxon:

```bash
df -h "$HOME/encode"
du -sh "$HOME/encode/jobs"/* 2>/dev/null
```

Ne töröld kézzel egy aktív vagy folytatandó job belső fájljait. A webes **Munka törlése** ismeri a job pontos határait és az adatbázist is frissíti.

### 10.6. Hibajelentéshez szükséges adatok

Hasznos adatok:

- a hiba pontos szövege;
- a job azonosítója;
- melyik szakaszban állt le;
- a worker napló érintett része;
- a `doctor --json` kimenete;
- a Debian verziója: `cat /etc/os-release`;
- Windows esetén a telepítési napló releváns része.

Titkos API-kulcsot, jelszót vagy teljes credential fájlt ne küldj hibajelentésben.

## 11. Frissítés

### 11.1. Automatikus frissítés

A telepítő létrehoz egy napi systemd timert. Ellenőrzése:

```bash
systemctl list-timers bdencode-update.timer
systemctl status bdencode-update.timer --no-pager
```

A futási idő naponta változhat, mert a rendszer terheléselosztás céljából legfeljebb 45 perces véletlen késleltetést használ. Ha a gép a tervezett időben ki volt kapcsolva, a `Persistent=true` miatt később pótolja a futást.

### 11.2. Kézi frissítés

```bash
cd ~/bdencode-backend
git fetch origin
git switch main
git pull --ff-only
bash install/install.sh
```

Windows alatt a repót Windowsból is frissítheted, majd újrafuttathatod a `windows-install.cmd` fájlt. A telepítő frissítésként kezeli a már létező környezetet; nem kell előtte eltávolítani.

A Windows-telepítő és a kézi frissítési példa egyaránt a `main` ágat használja. A Linux installer a sémamigráció előtt, leállított API és worker mellett a SQLite backup API-val konzisztens, root-only adatbázismentést készít és annak digestjét a telepítési tranzakcióhoz köti. Ha a candidate health/doctor ellenőrzése megbukik, vagy a telepítés a tartós `HEALTHY` döntés előtt megszakad, a rollback előbb ezt az adatbázismentést állítja vissza és ellenőrzi, utána állítja vissza az előző alkalmazáspointert és konfigurációt, és csak ezután indíthatja újra a régi szolgáltatásokat. A már tartós `HEALTHY` állapot utáni megszakítást a recovery a validált candidate commitjának finalizálásával zárja le, nem rollbackkel. Így a régi backend nem kap nála újabb adatbázissémát; sikertelen vagy nem bizonyítható schema-safe visszaállításnál a szolgáltatások blokkolva maradnak kézi helyreállításig.

## 12. Eltávolítás Linux vagy szerver esetén

### 12.1. Mielőtt elkezded

1. Fejezd be vagy szakítsd meg rendezetten az aktív munkát.
2. Mentsd ki azokat az eredményeket, amelyekre szükséged van.
3. Ellenőrizd a tényleges adatmappa és forrásmappa útvonalát.
4. Lépj be ugyanazzal a normál felhasználóval, amellyel telepítettél.
5. Ne futtasd az eltávolítót `root` felhasználóként.

A forráslemezeket az eltávolító soha nem törli.

### 12.2. Csak az alkalmazás eltávolítása, adatok megőrzésével

```bash
cd ~/bdencode-backend
bash install/uninstall.sh
```

Ez eltávolítja:

- a BDEncode systemd szolgáltatásokat és timert;
- a telepített alkalmazás aktív bekötését;
- a frontend és nginx/Swizzin integrációt;
- a rendszerállapothoz tartozó BDEncode fájlokat.

Ez alapértelmezetten megőrzi:

- a queue/job/output adatokat;
- a `~/encode` adatmappát;
- a képfeltöltő credentialöket;
- az eredeti forrásokat;
- az APT-tal telepített csomagokat;
- a Git checkoutot.

Az APT csomagokat azért nem távolítja el automatikusan, mert nem minden régebbi telepítésnél állapítható meg biztonságosan, melyiket használja más alkalmazás is.

### 12.3. Egyedi útvonalas telepítés eltávolítása

Ha a `/etc/bdencode/config.toml` hiányzik, vagy egyértelműen meg akarod adni a helyeket:

```bash
cd ~/bdencode-backend
bash install/uninstall.sh \
    --data-root /home/FELHASZNALO/encode \
    --source-root /storage
```

Több forrásgyökér esetén a `--source-root` többször megadható.

### 12.4. Teljes adatmappa törlése

> [!CAUTION]
> Ez visszavonhatatlanul törli a teljes megadott BDEncode adatmappát, benne a jobokkal, naplókkal, ideiglenes fájlokkal és az ott tárolt kész eredményekkel. Előtte készíts biztonsági mentést.

Az eltávolító szándékosan kétszer kéri ugyanazt a pontos útvonalat:

```bash
cd ~/bdencode-backend
bash install/uninstall.sh \
    --data-root /home/FELHASZNALO/encode \
    --source-root /storage \
    --purge-data \
    --confirm-data-root /home/FELHASZNALO/encode
```

A `--data-root` és `--confirm-data-root` értékének pontosan egyeznie kell. Ez véd a rossz mappa véletlen törlésétől.

### 12.5. Credentialök törlése

A hat, telepítő által ismert fix titkosított credential törlése:

```bash
cd ~/bdencode-backend
bash install/uninstall.sh --purge-credentials
```

A kapcsoló pontosan az `imgbb-api-key`, `catbox-userhash`, `freeimage-api-key`, `qbittorrent-username`, `qbittorrent-password` és `tracker-aither-api-token` credentialt törli. Egyedi nevű trackercredentialt és az üzemeltető által kezelt `tracker-local.conf` drop-int szándékosan nem távolít el; ezeket szükség esetén külön, kézzel kell törölni.

Alkalmazás, adatok és credentialök együttes eltávolításakor a kapcsolókat egy parancsban add meg.

### 12.6. Beépített biztonsági ellenőrzések

Az eltávolító megtagadja a törlést, ha:

- aktív queue vagy helyreállítás fut;
- az útvonal nem abszolút;
- az útvonal túl tág, például `/` vagy a teljes home;
- a cél szimbolikus link vagy mountpoint;
- a forrás és az adatgyökér átfedi egymást;
- a cél más tulajdonoshoz tartozik;
- a megerősítő útvonal eltér.

Ne kerüld meg ezeket az ellenőrzéseket kézi `rm -rf` paranccsal.

## 13. Eltávolítás Windows esetén

Két lehetőség van:

- csak a BDEncode eltávolítása, a Debian WSL megtartásával;
- a teljes, kizárólag BDEncode céljára telepített Debian WSL törlése.

### 13.1. Csak a BDEncode eltávolítása, Debian megtartása

1. Nyiss PowerShellt.
2. Lépj be a Debianba:

   ```powershell
   wsl -d Debian
   ```

3. A Linux parancssorban lépj a repó Windowsból csatolt mappájába. Például `C:\BDEncode` esetén:

   ```bash
   cd /mnt/c/BDEncode
   bash install/uninstall.sh
   exit
   ```

4. Ezután rendszergazdai PowerShellben töröld az életben tartó ütemezett feladatot és a Windows-specifikus nginx fájlt:

   ```powershell
   Stop-ScheduledTask -TaskName "BDEncode WSL" -ErrorAction SilentlyContinue
   Unregister-ScheduledTask -TaskName "BDEncode WSL" -Confirm:$false -ErrorAction SilentlyContinue
   wsl -d Debian -u root -- rm -f /etc/nginx/conf.d/bdencode-wsl.conf
   ```

5. Töröld kézzel az asztali BDEncode parancsikonokat, ha megmaradtak.
6. Ha már nincs szükséged a telepítő naplójára, törölheted a `%LOCALAPPDATA%\BDEncode` mappát.

### 13.2. A teljes BDEncode Debian WSL törlése

> [!CAUTION]
> A `wsl --unregister Debian` visszavonhatatlanul törli a teljes Debian disztribúciót és minden benne lévő fájlt. Csak akkor használd, ha ez a Debian példány kizárólag a BDEncode számára készült. Előbb másold ki a kész munkákat.

1. Ellenőrizd a disztribúció pontos nevét:

   ```powershell
   wsl --list --verbose
   ```

2. Másold ki a szükséges fájlokat a `completed` mappából.
3. Nyiss rendszergazdai PowerShellt, majd futtasd:

   ```powershell
   Stop-ScheduledTask -TaskName "BDEncode WSL" -ErrorAction SilentlyContinue
   Unregister-ScheduledTask -TaskName "BDEncode WSL" -Confirm:$false -ErrorAction SilentlyContinue
   wsl --shutdown
   wsl --unregister Debian
   ```

4. Ellenőrizd, hogy eltűnt:

   ```powershell
   wsl --list --verbose
   ```

5. Ha megmaradtak, kézzel törölhetők:

   ```text
   %LOCALAPPDATA%\BDEncode
   %LOCALAPPDATA%\BDEncodeWSL\Debian
   ```

6. Töröld az asztali parancsikonokat és – ha már nem kell – a letöltött `C:\BDEncode` Git/ZIP mappát.

A Windows meghajtón levő eredeti forrásmappa, például `D:\Filmek`, a Debian unregister műveletétől nem törlődik.

## 14. Haladó üzemeltetési tudnivalók

### 14.1. CPU-korlát

A `BDENCODE_CPU_PERCENT=80` azt jelenti, hogy a worker a gép összes logikai CPU-kapacitásának 80%-át kaphatja. Például 32 logikai CPU-nál a systemd kvóta 2560%.

Ez nem azt jelenti, hogy a Feladatkezelő mindig pontosan 80%-ot mutat. Egyes szakaszok nem használják ki az összes engedélyezett szálat, más háttérfolyamatok pedig szintén fogyaszthatnak CPU-t.

Más limit telepítéskor:

```bash
BDENCODE_CPU_PERCENT=60 bash install/install.sh
```

Az érték 1 és 100 közötti egész szám lehet.

### 14.2. Szolgáltatások

```bash
sudo systemctl status bdencode-api.service --no-pager
sudo systemctl status bdencode-worker.service --no-pager
sudo systemctl restart bdencode-api.service bdencode-worker.service
```

### 14.3. Konfiguráció

A gépszintű konfiguráció helye:

```text
/etc/bdencode/config.toml
```

Módosítás előtt készíts másolatot, és inkább futtasd újra a telepítőt a kívánt környezeti változókkal. Kézi szerkesztésnél egy hibás útvonal vagy jogosultság a workert indulásképtelenné teheti.

A trackerprofilok külön, root által kezelt fájlban vannak:

```text
/etc/bdencode/release-profiles.json
```

A fix dupe/publish endpointokat és host-allowlisteket itt, a hozzájuk tartozó API-titkokat kizárólag titkosított systemd credentialként állítsd be. Az announce URL személyes passkeyt tartalmazhat, ezért magát a root-only profilfájlt és az abból készülő torrentet/upload kitet is titokként kezeld. A részletes lépések az [5.3. fejezetben](#53-trackerprofil-és-qbittorrent-beállítása) találhatók.

### 14.4. Adatbiztonság

- A forrást a BDEncode olvassa, nem módosítja.
- A munkamappában nagy ideiglenes fájlok keletkezhetnek.
- A kész eredményt csak sikeres lezárás után tekintsd véglegesnek.
- Fontos kiadásnál a kész MKV és mellékletei kerüljenek külön mentésbe.
- Futó job alatt ne mozgass vagy törölj kézzel fájlokat.

## 15. Fejlesztés és tesztelés

### 15.1. Python környezet

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
```

Windows PowerShellben:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
pytest
```

### 15.2. Frontend fejlesztés

```bash
cd frontend
npm install
npm run build
```

Fejlesztői szerverhez a projekt `frontend` mappájának csomagszkriptjeit használd. A telepített produkciós frontend a buildelt fájlokat nginx mögül szolgálja ki; a fejlesztői szerver nem helyettesíti a telepített API-t és workert.

### 15.3. Fontos fejlesztői szabály

Tesztadatot vagy API-kulcsot ne commitolj. A valós Blu-ray források helyett kis, mesterséges mintákkal teszteld azokat a funkciókat, amelyekhez nincs szükség teljes lemezre.

---

Ha hibát találsz, először mentsd el a pontos hibaüzenetet és a kapcsolódó naplórészletet. A „nem működik” önmagában kevés; a job állapota, a hibás szakasz és az utolsó parancs általában azonnal megmutatja, hol kell javítani.
