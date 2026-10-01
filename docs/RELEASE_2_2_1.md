# BDEncode 2.2.1 kiadási jegyzet

Biztonsági finomítás a 2.2.0-hoz; a működés és az API nem változik. Ez egyben az első kiadás, amelyet a 2.2.0-ban telepített napi kiadáskereső felügyelet nélkül telepíthet.

## Változások

- **A `deployment.lock` megnyitása rootként symlink-biztos.** A `bdencode-update-recover` (minden időzített futás előtt és induláskor, rootként) korábban a `state/deployment.lock` fájlt `>` átirányítással nyitotta meg, és `touch`-csal hozta létre. A mappát a sandboxolt worker is írja, ezért egy odarakott symlinken keresztül tetszőleges fájlt lehetett volna csonkítani vagy létrehozni. Most a szkript elutasítja a symlinket a megnyitás előtt és után is, és `>>`-t használ, amely nem csonkít. Ugyanez igaz a kézi eszközfrissítőre (`bdencode-daily-update`), amelynek `chown`-jai ráadásul `-h`-val futnak.
- **A frissítő letöltése a worker számára írhatatlan helyre kerül.** A kiadáskereső eddig a `~/encode/cache/release-update/` alá klónozott, amelyet a worker is írhat. Mostantól a `~/.cache/bdencode-release-update/` alá megy, ahová a worker (`ProtectHome=read-only`) nem írhat, így nem módosíthatja a letöltést az ellenőrzés és a telepítő indítása között.

## Frissítés

A 2.2.0-ra telepített rendszer a napi időzítővel magától frissít (README 11.1.), amikor a `v2.2.1` tag megjelenik és a sor üres. Kézi telepítésnél a README 11.2. szerint járj el.

## Ellenőrzőlista

1. `GET /api/v1/capabilities` `backend_version`: `2.2.1`.
2. `cat /var/lib/bdencode/release-update/status.json`: `installed` vagy `up_to_date`, és a `release-update.log` végén a telepítés naplója.
3. `systemctl is-active bdencode-api bdencode-worker bdencode-update.timer`.
