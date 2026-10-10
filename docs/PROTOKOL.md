# Protokol QuantPilot v4 / API v2

Vse zasebne poti zahtevajo `X-API-Key`. HTTPS je na posredniškem strežniku; API sam posluša na zasebnem vmesniku. Največ 500 sveč/paket. Sheme ne sprejemajo neznanih polj.

## Identiteta

Vsak zahtevek vsebuje `source`, `symbol`, `broker`, `timeframe="M1"`, `digits` in `point`. `source` je izrecna oznaka enega vira in ni številka računa. Imena simbolov in posrednikov so omejena po dolžini in ne smejo vsebovati kontrolnih znakov. Sprememba posrednika, decimalnih mest ali velikosti točke ob istem viru/simbolu vrne 409.

## POST /v2/mt5/state

Dodatno polje `initial_start_t`, privzeto 0. Prvi zahtevek registrira vir/simbol, vendar pri 0 še ne določi začetka. EA šele po potrjenem celotnem začetnem oknu ali izrecnem datumu pošlje čas prve sveče. Potem začetek ostane fiksen.

Odgovor:

```json
{"accepted":true,"db_id":"0123456789abcdef0123456789abcdef","start_t":1700000040,"last_t":0,"initialized":true}
```

`last_t=0` pomeni, da ni bila potrjena še nobena sveča. Baza ima trajno naključno `db_id`; nova prazna baza dobi drugo identiteto. Ob obnovi iste baze iz kopije se kazalec na strežniku lahko vrne nazaj tudi ob enakem `db_id`.

## POST /v2/mt5/bars

Dodatno: `db_id`, `expected_last_t`, `latest_closed_t`, `bars`, `quote`.

Polja sveče: `t`, `o`, `h`, `l`, `c`, `volume`, `real_volume`, `spread`; opcijska `atr14`, `rsi14`, `ema20`, `ema50`, `ema200`.

- Časi so strogo naraščajoči, brez podvojitev; čas M1 je poravnan na minuto.
- Cena je končna in pozitivna. OHLC mora biti skladen.
- Sveče morajo biti po potrjenem kazalcu in znotraj nastavljenega začetka.
- Prvi paket se mora začeti točno z `start_t`.
- `expected_last_t` se mora ujemati s strežniškim stanjem.
- Vse sveče morajo biti največ do odjemalčeve zadnje zaprte sveče. Strežnik ne uporablja svoje UTC ure kot nadomestila za čas posrednika.
- EA s sidrno svečo in celotnim zahtevanim številom preveri, da pri izbiri ne preskoči podatkov. API sam nima dostopa do posrednika in ne more ugotoviti, ali je presledek med dvema časoma normalen ali pomeni manjkajočo svečo.

Podatki in novi kazalec se zapišejo v eni transakciji z `BEGIN IMMEDIATE`. Potrditev se vrne po `COMMIT`:

```json
{"accepted":true,"db_id":"0123456789abcdef0123456789abcdef","last_t":1700000100,"accepted_count":2}
```

EA preveri identiteto baze, točen potrjeni čas in število sveč. Napredka ne shrani zgolj na podlagi HTTP 200.

## Izgubljena potrditev in več pošiljateljev

Če se paket potrdi v bazi, odgovor pa izgubi, ponovitev s starim `expected_last_t` vrne 409. EA zato ponovno prebere `/state` in nadaljuje od potrjenega stanja. Podatki se ne podvojijo. Za transakcijsko zaščito pri sočasnem pisanju skrbi baza, vendar je podprt namen en zbiralnik na vir; dva neodvisna vira morata imeti različni oznaki.

Pri 401 preveri ključ, pri 404 ujemanje različice API-ja, pri 409 stanje/identiteto vira, pri 422 vhodne podatke. Pri 503 preveri bazo, prostor in dovoljenja. EA ponavlja z naraščajočim zamikom do nastavljene meje, po HTTP napaki pa stanje ponovno prebere.

## POST /v2/mt5/reconcile

Enaka shema kot za `/bars`, vendar so dovoljene samo sveče od `start_t` do že potrjenega `last_t`. Zahtevek ne premakne kazalca in ne spremeni časa zadnjega novega uvoza. Posodobi cene/volumne/razmik ter veljavne indikatorje. `null` ne izbriše predhodno veljavnega indikatorja.

EA prednostno pošilja nove/neposlane sveče. Ko jih dohiti, v nastavljivem oknu po paketih krožno ponovno pošilja potrjeno zgodovino. To popravi manjkajoč zapis v bazi ali pozneje popravljeno svečo pri posredniku, če je znotraj tega okna.

## POST /v2/mt5/heartbeat

Dodatno `db_id`, `latest_closed_t` (lahko 0, če podatek ni znan), `status`, `quote`. Stanja:

- `ok` — trenutno ni znanega zaostanka;
- `backfill` — pošiljanje zgodovine še napreduje;
- `waiting_history` — podatki še niso pripravljeni;
- `history_anchor_missing` — točna potrjena začetna/sidrna sveča ni na voljo;
- `indicator_wait` — indikatorji še niso pripravljeni;
- `invalid_bar` — neveljavna sveča;
- `disconnected` — MT5 ni povezan s posrednikom;
- `history_changed` — rezervirano stanje za zunanje odjemalce pri spremembi podlage.

Heartbeat ne premika kazalca sveč. Veljavne kotacije je mogoče shranjevati tudi, kadar ni novih sveč. Strežnik ne sprejme trditve `ok`, če prijavljena zadnja sveča kaže zaostanek.

## GET /v2/stats in GET /v2/bars

Statistika vključuje pričakovano stanje vsakega registriranega vira/simbola, število zapisov, prvi/zadnji surovi čas, `last_seen`, `last_ingested`, starost signala delovanja, `stale`, metapodatke in prijavljen časovni zaostanek. Časovni zaostanek ni število manjkajočih sveč.

Branje sveč sprejme `source`, `symbol`, `after` in `limit` (do 5.000). Rezultat je urejen po času; naslednji klic uporabi `next_after`. Strani niso zamrznjen posnetek in se lahko med tekočim popravljanjem spremenijo.

## Varnostna kopija in v3

Tabele v4 so ločene od v3. Kazalcev v3 se ne prevzame brez preverjenega uvoza. Varnostna kopija zajame celotno SQLite bazo, tudi stare tabele, če obstajajo. Pri popolni izgubi baze potrebujemo kopijo ali ponovno nastavljeno začetno zgodovino; protokol ne rekonstruira samodejno vseh izgubljenih let podatkov.
