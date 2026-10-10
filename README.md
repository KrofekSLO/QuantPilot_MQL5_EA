# QuantPilot Collector v4 — MT5 → Hetzner

Posodobljen zbiralnik zaprtih M1 sveč, razmika, volumna in indikatorjev. MT5 ostane na Windows VPS, API in SQLite na Hetznerju. Zbiralnik ne odpira poslov.

**Obvezno posodobi oba dela: MQL5 in API. v4 uporablja protokol `/v2`, zato ni združljiv z API-jem v3.**

## Kaj je spremenjeno

- Kazalec je v bazi na strežniku. EA ga prebere ob zagonu, približno vsakih 60 sekund in po napaki. Lokalnih globalnih spremenljivk `QP3_*` ne uporablja.
- Privzeti začetni uvoz počaka na vseh 3.000 zahtevanih zaprtih sveč. Začetni čas se nato fiksira na strežniku; delni prvi prenos ne more samodejno prestaviti začetka naprej.
- Po izpadu nadaljuje od točnega časa potrjene sveče. Omejitev 3.000 sveč velja za privzeti začetni uvoz, ne za dolžino kasnejšega izpada. Obnova je še vedno odvisna od razpoložljive zgodovine posrednika in nastavitev MT5.
- Če potrjene sveče v zgodovini ni, zbiralnik čaka oziroma opozori. Ne preskoči samovoljno na zadnjih 3.000 sveč.
- Pri osmih simbolih in časovniku 1 s pride simbol običajno na vrsto približno vsakih 8 s. To ni zajamčena zakasnitev: sinhroni HTTP in nalaganje zgodovine lahko obhod podaljšata. Zbira zaprte M1 sveče, ne vseh tickov.
- Neveljavna ali prestara kotacija se pošlje kot `null`, zato ne blokira veljavnih sveč.
- ATR14, RSI14, EMA20, EMA50 in EMA200 se berejo iz indikatorjev MT5 za isti simbol in isto M1 svečo. RSI in EMA uporabljata `PRICE_CLOSE`.
- Preverjanje že poslane zgodovine v privzetem oknu zadnjih 3.000 sveč po paketih popravlja naknadne spremembe in manjkajoče zapise. Ne premakne glavnega kazalca.
- API zavrača NaN, Infinity, napačen OHLC, neurejene sveče, napačen ključ in napačen kazalec. Sveče in napredek se zapišejo v eni transakciji.
- Na voljo so signal delovanja, statistika, branje podatkov, nadzorni skript in konsistentna varnostna kopija SQLite.

## Vsebina paketa

| Datoteka | Namen |
|---|---|
| `mt5/QuantPilotCollector_v4.mq5` | Celotna izvorna koda EA |
| `server/app.py` | API protokola v2 |
| `server/backup.py` | Spletna varnostna kopija SQLite, tudi ob podatkih v WAL |
| `server/monitor.py` | Preverjanje pričakovanih simbolov in zaustavljenega uvoza |
| `server/export_legacy.py` | Izvoz ohranjenih tabel v3 v CSV |
| `docker-compose.yml` | Nov strežnik s Caddy za HTTPS |
| `docker-compose.existing-proxy.yml` | Obstoječi HTTPS posredniški strežnik brez drugega Caddy |
| `docs/PROTOKOL.md` | Pogodba EA/API in obnova po napakah |
| `docs/PREVERJANJE.md` | Rezultati opravljenih testov in preizkusi v MT5 |
| `tests/` | Avtomatizirani testi API-ja, izbire sveč in operativnih skriptov |

## 1. Nov Hetzner strežnik

Predpogoj: Docker Engine in vtičnik Docker Compose. Ustvari zapis DNS A za svojo dejansko domeno. Če ima domena AAAA, mora tudi IPv6 pravilno voditi na strežnik. Vrata 80/443 morajo biti dostopna za HTTPS in izdajo certifikata.

Razširi paket v `/opt/quantpilot-collector-v4` in izvedi:

```bash
cd /opt/quantpilot-collector-v4
cp .env.example .env
openssl rand -hex 32
nano .env
chmod 600 .env
mkdir -p backups
chmod 700 backups
```

V `.env` vpiši:

- `QP_DOMAIN`: dejanska domena brez `https://` in brez poti;
- `QP_API_KEY`: generiran 64-mestni šestnajstiški ključ;
- `QP_EXPECTED_SOURCE`: enaka vrednost kot `InpSourceId` v MT5;
- `QP_EXPECTED_SYMBOLS`: točna imena vseh simbolov pri tvojem posredniku;
- `QP_DATA_VOLUME`: ime trajnega podatkovnega volumna.

Privzeta domena `forex.example.com` ni konfigurirana za uporabo. EA jo namenoma zavrne. API se brez veljavnega ključa ne zažene. Dovoljeni ključ ima 32–128 znakov: črke ASCII, številke, `_` in `-`.

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Preveri svojo domeno, npr. po zamenjavi vzorčne domene:

```bash
curl --fail https://TVOJA-DEJANSKA-DOMENA/health
```

API vrata 8000 niso javno objavljena. Caddy posreduje HTTPS do API-ja. `/health` preveri dostop do baze, ne potrjuje svežine tržnih podatkov ali razpoložljivega prostora za nov zapis.

## 2. Če Hestia, CloudPanel ali Nginx že zaseda vrata 80/443

Uporabi drugo konfiguracijo **namesto** osnovne, ne kot dodatno datoteko `-f` poleg nje:

```bash
docker compose -f docker-compose.existing-proxy.yml config --quiet
docker compose -f docker-compose.existing-proxy.yml up -d --build
```

Obstoječi HTTPS posredniški strežnik naj za tvojo domeno posreduje na `http://127.0.0.1:8000`. API je objavljen samo na lokalnem vmesniku. Primer bloka v obstoječi konfiguraciji Nginx:

```nginx
location / {
    client_max_body_size 512k;
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_connect_timeout 5s;
    proxy_read_timeout 15s;
}
```

TLS, domenski virtualni strežnik in certifikat ostanejo v tvojem obstoječem upravljalniku. Zgornji blok ni celotna konfiguracija virtualnega strežnika. Če uporabljaš to različico, pri vseh nadaljnjih ukazih `docker compose` dodaj `-f docker-compose.existing-proxy.yml`.

## 3. Namestitev v MetaTrader 5

1. Prijavi MT5 v ustrezen račun pri posredniku.
2. V `Market Watch` preveri točna imena simbolov; pripone in začetna pika so pomembne.
3. V `Tools → Options → Charts → Max bars in chart` nastavi vsaj 100.000 za običajno uporabo in primeren presežek za indikatorje. Za daljši zgodovinski uvoz potrebuješ več; posrednik mora podatke tudi dejansko ponujati.
4. Odpri `File → Open Data Folder` in kopiraj datoteko `.mq5` v `MQL5/Experts`.
5. Pritisni F4, odpri datoteko v MetaEditorju in F7 za prevod. Preveri dnevnik prevajanja. Datoteka `.ex5` v paketu ni priložena, ker dejanski prevod tukaj ni bil izveden.
6. V `Tools → Options → Expert Advisors → Allow WebRequest for listed URL` dodaj svojo domeno s `https://`.
7. EA pripni na **en** graf. Bere vse simbole iz seznama, ne le simbola tega grafa.
8. Nastavi `InpApiBase` na HTTPS izvor, npr. `https://TVOJA-DEJANSKA-DOMENA`, brez `/v2/mt5/bars`.
9. Nastavi isti API-ključ, isti vir in seznam simbolov kot na strežniku.
10. Za začetni preizkus omogoči `InpVerbose=true`. Spremljaj zavihka Experts/Journal in statistiko strežnika.

MT5 in Windows VPS morata ostati vključena. `WebRequest` v Strategy Testerju ne deluje; EA ga zavrne že ob inicializaciji.

## 4. Pomembne nastavitve EA

| Nastavitev | Privzeto | Pomen |
|---|---:|---|
| `InpTimerSeconds` | 1 | En simbol na dogodek časovnika |
| `InpHistoryBars` | 3000 | Celotno zahtevano začetno okno zaprtih M1 sveč |
| `InpBootstrapFrom` | 0 | Opcijsko: točen čas začetka obstoječe M1 sveče v času posrednika |
| `InpBatchBars` | 200 | Največ sveč v enem prenosu; do 500 |
| `InpHttpTimeoutMs` | 5000 | Čakalni čas ene HTTP zahteve |
| `InpStateRefreshSeconds` | 60 | Redno branje dejanskega napredka iz baze |
| `InpHeartbeatSeconds` | 30 | Signal delovanja, če ni novih sveč |
| `InpMaxBackoffSeconds` | 120 | Zgornja meja zamika po neuspelih zahtevah |
| `InpRequireAllSymbols` | true | Manjkajoči zahtevani simbol ustavi inicializacijo |
| `InpSendQuote` | true | Veljavna bid/ask kotacija ob prenosu ali signalu delovanja |
| `InpQuoteMaxAgeSeconds` | 120 | Največja dovoljena starost kotacije |
| `InpSendIndicators` | true | ATR14, RSI14, EMA20/50/200 za M1 |
| `InpReconcileBars` | 3000 | Okno preverjanja že poslane zgodovine; 0 izključi preverjanje |
| `InpReconcileIntervalSeconds` | 60 | Interval enega popravljalnega paketa na simbol |

Za nove instrumente z manj kot 3.000 svečami ustrezno zmanjšaj `InpHistoryBars`. Zbiralnik ne zmanjšuje okna sam in ne preskoči naprej zgolj zato, ker se zgodovina še nalaga.

`InpBootstrapFrom` velja samo za nov oziroma še neinicializiran vir/simbol. Ne prestavi začetka obstoječega toka. Za ločen starejši uvoz uporabi nov `InpSourceId`, prilagodi nadzor in izberi **dejanski obstoječi čas odprtja M1 sveče**. Izbrani datum mora obstajati za vsak simbol na seznamu; če se seje razlikujejo, uporabi ločene zbiralnike z različnimi viri. Datum ni UTC, razen če to dejansko velja za posrednikovo časovno vrsto.

`InpSourceId` naj enolično označuje namestitev in cenovni vir, npr. `roboforex-demo-main`. Številka računa se ne pošilja. Dveh različnih računov/terminalov ne zaganjaj z istim virom. Ob spremembi računa EA ustavi časovnik; ponovno ga pripni z ustreznim virom. Ob spremembi podatkov instrumenta API zahteva drug vir.

## 5. Prehod z v3

1. Ustavi EA v3. Shrani njegove nastavitve in ohrani stari paket za povrnitev.
2. Če API v3 že deluje, najprej konsistentno varnostno kopiraj njegovo bazo. Pri starem API-ju lahko novi `backup.py` začasno kopiraš v vsebnik in ga zaženeš z `python /tmp/backup.py --directory /data/v3-backups`. Kopijo nato prenesi iz vsebnika na varno mesto.
3. Z `docker inspect` preveri dejansko ime starega volumna. V `.env` uporabi njegovo ime kot `QP_DATA_VOLUME`, če želiš iste fizične podatke ohraniti v isti bazi. Privzeti `quantpilot_data_v4` je **nov** volumen.
4. Stari projekt ustavi z `docker compose down`, **brez `-v`**. Nato zaženi v4 z izbranim volumnom. Ne zaganjaj dveh Caddy na istih vratih.
5. v4 ustvari ločene tabele `streams_v4`, `bars_v4`, `quotes_v4`, `qp_meta`; starih tabel `bars` in `quotes` ne spreminja.
6. v3 vrstic ne poveže samodejno z v4 kazalcem: vir v3 je bil ime posrednikovega strežnika, v4 pa ima izrecno identiteto vira in lahko pošilja indikatorje. Nov uvoz v4 začne od nastavljene zgodovine ali datuma.
7. Po potrebi stare podatke izvozi:

```bash
docker compose exec -T api python export_legacy.py --output /backup/legacy-v3.csv
```

Izvoz je na voljo samo, če je izbrana baza res vsebovala tabele v3. Obstoječe izhodne CSV datoteke skript ne prepiše. Za povrnitev ustavi v4 in vrni stari API/EA ter ohranjeni volumen; ne briši podatkovnih volumnov.

## 6. Nadzor delovanja

```bash
docker compose exec -T api python monitor.py
```

Izhodna koda 0 pomeni, da so pričakovani simboli registrirani, signali delovanja sveži in ni ustavljenega uvoza; 1 opozorilo; 2 napako API-ja ali konfiguracije. Skript ne pošilja e-pošte ali drugih sporočil. Za obstoječi nadzor ga lahko kličeš na minuto.

Podrobna statistika je na zaščitenem `/v2/stats`, podatki na `/v2/bars`. Ključ je v glavi `X-API-Key`. Iz notranjosti vsebnika lahko statistiko pogledaš brez vpisovanja ključa v ukaz:

```bash
docker compose exec -T api python -c 'import os,json,urllib.request; r=urllib.request.Request("http://127.0.0.1:8000/v2/stats",headers={"X-API-Key":os.environ["QP_API_KEY"]}); print(json.dumps(json.load(urllib.request.urlopen(r)),indent=2))'
```

Za naknadno preverjanje lukenj primerjaj prebrane sveče z izvozom istega simbola in istega obdobja iz MT5. Razlika med časoma sveč sama po sebi ni dokaz izgube podatkov: trgovalne seje in minute brez tickov lahko povzročijo legitimne vrzeli.

## 7. Varnostna kopija in obnova

```bash
docker compose exec -T api python backup.py --keep 14
```

Kopije so v lokalni mapi `backups`. Skript uporabi SQLite Backup API, preveri `quick_check`, ustvari samostojno datoteko brez odvisnosti od WAL, nato obdrži zadnjih 14 kopij. Ne kopiraj samo aktivne datoteke `.db`, saj so lahko potrjeni zapisi še v WAL.

Primer dnevnega opravila na strežniku (po tvoji namestitvi v navedeno mapo):

```cron
15 2 * * * cd /opt/quantpilot-collector-v4 && docker compose exec -T api python backup.py --keep 14 >> /var/log/quantpilot-backup.log 2>&1
```

Opravilo ni samodejno nameščeno. Za drugo konfiguracijo popravi ukaz `docker compose`. Lokalna kopija ne zaščiti pred izgubo celotnega VPS: kopije prenesi še na drugo lokacijo.

Obnova: ustavi EA in API, izbrano preverjeno kopijo vrni v podatkovni volumen kot `quantpilot.db` in odstrani ostanke `quantpilot.db-wal`/`quantpilot.db-shm` **samo pri ustavljenem API-ju**. Pred menjavo shrani obstoječe datoteke za povrnitev. Nato zaženi API in EA. EA znova prebere stanje ter nadaljuje od kazalca obnovljene baze, če posrednik še ponuja potrebno zgodovino.

## 8. Omejitve, ki ostajajo

- Dejanski prevod MQL5 in test na tvojem MT5/posredniku tukaj nista bila izvedena. Najprej uporabi demo račun in postopke v `docs/PREVERJANJE.md`.
- Pridobiva podatke, ki jih ponuja posrednik; ne more dokazati popolnosti njegovega vira.
- Timestamps `t`, `latest_closed_t`, `time_msc` so surovi MT5 podatki. `received_at` in strežniški signali delovanja so Unix UTC. Zgodovinska normalizacija časovnega pasu/DST ni implementirana.
- `volume` je `tick_volume`; `real_volume` je ločeno polje in je pri številnih OTC instrumentih 0. Razmik je v točkah instrumenta, ne nujno v pipih.
- Indikatorji so vrednosti MT5, ne neodvisno ponovno izračunane vrednosti. Ogrevanje in spremembe podlage lahko vplivajo nanje; neveljavne/manjkajoče vrednosti so `null`, ne izmišljene ničle. API pri popravku z `null` ohrani predhodno veljaven indikator, če obstaja.
- Privzeto naknadno preverjanje zajema zadnjih 3.000 sveč, ne vseh let zgodovine. Pri 200 svečah/paket in 60 s/paket potrebuje približno 15–16 minut za obhod okna na simbol; ob novi zgodovini, napakah ali zaostanku lahko več. Starejše luknje potrebujejo poseben uvoz oziroma obnovo.
- Če je baza popolnoma izgubljena, se brez varnostne kopije privzeto znova uvozi le začetno okno. Za starejšo obnovo nastavi datum uvoza in zagotovi razpoložljivo zgodovino.
- Kazalec se ne premakne mimo neveljavne sveče ali manjkajočega sidra. Tak primer zahteva odpravo napake vira, nalaganje zgodovine ali zavestno izbiro novega vira/začetka.
- `/health` je preverjanje dostopa do baze. Za celotno zbiranje uporabljaj `/v2/stats` in `monitor.py`.
- Ni avtomatskega trgovanja, tick arhiva, obdelave novic, normalizacije UTC ali zagotovila uspešnosti strategij.

## 9. Lokalni testi Python

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r server/requirements.txt -r tests/requirements.txt
python -m pytest tests -q
```

Rezultati in ločitev med Python testi, simulacijo ter dejanskim preverjanjem MT5 so opisani v `docs/PREVERJANJE.md`.
