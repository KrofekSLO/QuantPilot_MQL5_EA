# Preverjanje različice 4.00

Datum: 10. 10. 2026.

## Kaj je bilo dejansko izvedeno

| Preverjanje | Rezultat | Omejitev |
|---|---|---|
| Avtomatizirani testi Python | 37 testov uspešno | Dejanski FastAPI/Pydantic/SQLite, testni HTTP odjemalec; ne terminal MT5 |
| Zagon Uvicorn in zahteve po lokalnem HTTP | Uspešno | Pravi proces, avtentikacija, inicializacija, prenos in ACK; brez zunanjega HTTPS |
| Varnostna kopija aktivne baze WAL | Uspešno | Preverjen prenos potrjenih zapisov, samostojnost kopije in hramba |
| Simulacija izbire sveč | Uspešno | Neodvisen model pozicij/sidra v Python; ne izvrševanje MQL5 |
| Leksični pregled MQL5 | Uspešno | Zaključeni nizi/komentarji, ujemanje oklepajev, pregled funkcij; ni prevajalnik |
| Pregled podpisov MQL5 | Opravljen | Primerjava z uradno dokumentacijo; ni dejanski prevod |
| Sintaksa Python in YAML | Uspešno | Ne pomeni uspešno zgrajenih vsebnikov |
| Prevod v MetaEditorju | Ni izveden | MetaEditor ni na voljo |
| MT5 z dejanskim posrednikom | Ni izveden | Ni povezave do uporabnikovega terminala |
| Docker/Caddy/TLS na Hetznerju | Ni izveden | Docker in zunanji strežnik nista na voljo |

Pri testih je bilo eno opozorilo o zastarelem vzdevku `BlockingPortal` v odvisnosti testnega odjemalca. Ni napaka testa ali zbiralnika.

Testirano okolje Python: 3.12; FastAPI 0.115.12; Pydantic 2.11.4; Uvicorn 0.34.2; pytest 8.3.5; httpx 0.28.1. Datoteki requirements vsebujeta te neposredne različice. Preostale posredne odvisnosti in sliki Docker nimajo zaklenjenih zgoščenih vrednosti.

## Pokriti scenariji

- veljavne sveče, indikatorji, metapodatki in branje iz baze;
- izgubljena potrditev po COMMIT, ponovitev in branje dejanskega kazalca;
- prepoved preskoka fiksnega začetka prvega paketa;
- inicializacija praznega toka ter ohranitev fiksnega začetka;
- nadaljevanje skozi daljši časovni presledek oziroma vikend;
- neveljavni OHLC, čas, volumen, razmik in indikatorji;
- NaN, Infinity in -Infinity vrnejo 422 brez premika kazalca;
- neveljavna kotacija in uspešen prenos istih sveč brez kotacije;
- veljavna kotacija in preprečevanje podvojitev kotacij;
- popravljanje izbrisane/popravljene potrjene sveče brez premika kazalca;
- prepoved popravljanja zunaj potrjenega intervala;
- ohranitev veljavnega indikatorja, če je pri popravku nova vrednost null;
- zamenjana identiteta baze in ponovna inicializacija;
- obvezna avtentikacija vseh zasebnih poti;
- sprememba identitete cenovnega vira ter ločen vir;
- napaka med transakcijo razveljavi tudi predhodne sveče in kazalec;
- dva sočasna pošiljatelja ne moreta dvakrat premakniti istega kazalca;
- podvojene/neurejene sveče in sveča po prijavljeni zaprti sveči;
- status zaostanka in zastarel signal delovanja;
- ponovni zagon ohrani identiteto baze in ne spreminja tabel v3;
- model začetnega uvoza in nadaljevanja pri paketih različnih velikosti;
- odsotno sidro in premik pozicij ob novi minuti;
- varnostna kopija WAL, integriteta, hramba in izvoz v3;
- nadzor pričakovanih, manjkajočih, zastarelih in ustavljenih virov.

## Preizkus, ki ga je treba opraviti v MT5

1. V MetaEditorju prevedi `.mq5` s F7. Preglej vse napake in opozorila. Cilj je prevod brez napak; Python testi tega ne potrjujejo.
2. Začni na demo računu. Uporabi vsaj dva simbola, nato vseh osem. Potrdi točna imena instrumentov.
3. Vklopi `InpVerbose=true`. Preveri, da API registrira vse pričakovane simbole, uvoz napreduje ter doseže zadnjo zaprto M1 svečo.
4. V MT5 izvozi vsaj 100 zaporednih zaprtih M1 sveč za vsak simbol. Primerjaj njihove čase/OHLC/tick volume/spread z `/v2/bars`. Čase primerjaj kot podatke posrednika.
5. Indikatorje primerjaj z MT5 Data Window za iste M1 čase: ATR14, RSI14/Close, EMA20/50/200/Close. Ne primerjaj z drugače nastavljenim indikatorjem ali trenutno svečo.
6. Na svežem testnem viru omeji razpoložljivo zgodovino oziroma izberi večji zahtevani začetni obseg. Preveri `waiting_history` in da se `last_t` ne premakne. Nato omogoči celotno zgodovino in preveri pravilen začetni čas.
7. Za nekaj minut ustavi API. MT5 mora pokazati HTTP napako, uvoz pa po vrnitvi API-ja nadaljevati od potrjenega kazalca. Primerjaj interval s podatki MT5.
8. Zapri in ponovno odpri MT5. Isti vir mora nadaljevati iz strežniške baze brez novih podvojitev.
9. Preveri obdobje čez zaprtje seje/vikend. EA ne sme zahtevati izmišljene sveče za vsako koledarsko minuto.
10. Na testni kopiji baze izbriši eno svečo znotraj okna preverjanja. Po obhodu mora biti obnovljena; glavni kazalec se pri popravku ne premakne.
11. Na ločenem testnem volumnu zaženi prazno bazo. EA mora zaznati novo stanje in znova inicializirati začetni uvoz. Ne briši produkcijske baze za ta test.
12. Preveri manjkajoče sidro: če zgodovina posrednika ne vsebuje potrjenega časa, mora biti status `history_anchor_missing` in kazalec ostati nespremenjen. Nato naloži potrebno zgodovino in preveri nadaljevanje.
13. Preveri `monitor.py`: izpuščen pričakovani simbol ali ustavljen terminal mora povzročiti opozorilo. Normalno zaprta trgovalna seja ob svežem signalu delovanja ne sme biti označena kot izguba sveč.
14. Izvedi varnostno kopijo in preizkus obnove na ločeni bazi, preden se zaneseš nanjo v produkciji.

Za sprejem potrebujemo dejanski prevod, ujemanje vzorca sveč/indikatorjev ter uspešen preizkus prekinitve in ponovnega zagona. Zanesljivosti cenovnega vira posrednika ali zgodovinske normalizacije UTC ti testi ne dokazujejo.

## Uradni viri, uporabljeni pri statičnem pregledu

- https://www.mql5.com/en/docs/series/copyrates
- https://www.mql5.com/en/docs/series/ibarshift
- https://www.mql5.com/en/docs/series/seriesinfointeger
- https://www.mql5.com/en/docs/series/copybuffer
- https://www.mql5.com/en/docs/series/barscalculated
- https://www.mql5.com/en/docs/indicators/iatr
- https://www.mql5.com/en/docs/indicators/irsi
- https://www.mql5.com/en/docs/indicators/ima
- https://www.mql5.com/en/docs/network/webrequest
- https://www.mql5.com/en/docs/event_handlers/ontimer
- https://www.mql5.com/en/docs/common/gettickcount64
- https://www.mql5.com/en/docs/constants/structures/mqlrates
- https://caddyserver.com/docs/caddyfile/directives/request_body
