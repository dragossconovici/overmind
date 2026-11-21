# Overmind — XAUUSD

Strumento personale di analisi dell'oro: bias Weekly / Daily / H4, confidenza % trasparente,
zone SMC (order block, FVG, liquidità), macro (FRED), posizionamento dei fondi (CFTC COT)
e spiegazione in italiano generata da Gemini. Costo: zero.

```
GitHub Actions (ogni 4h) ──► engine/ (Python, solo standard library)
                               ├─ prezzi: Twelve Data (spot) → fallback Yahoo GC=F
                               ├─ macro: FRED (DFII10, DGS2, DTWEXBGS)
                               ├─ smart money: CFTC COT (managed money)
                               ├─ struttura/zone/punteggio (deterministico)
                               └─ Gemini: solo spiegazione testuale
                            ──► site/data/analysis.json + history.json
Cloudflare Pages ──► site/ (HTML/JS statico)
```

## Avvio in locale

```bash
copy .env.example .env      # poi inserisci le chiavi
python -m unittest discover -s tests
python -m engine.run
python -m http.server 8765 --bind 127.0.0.1 --directory site
```
Apri http://127.0.0.1:8765

## Chiavi gratuite

| Variabile | Dove | Obbligatoria |
|---|---|---|
| `FRED_API_KEY` | fred.stlouisfed.org → My Account → API Keys | sì (macro) |
| `GEMINI_API_KEY` | aistudio.google.com → Get API key | no (solo testo IA) |
| `TWELVEDATA_API_KEY` | twelvedata.com (piano Basic gratuito) | no (prezzi spot) |

Senza una chiave il sito funziona lo stesso: la fonte risulta "Non configurata" nel banner
e i fattori corrispondenti vengono esclusi (la confidenza scende di conseguenza).

## Messa online

1. Crea un repository **pubblico** su GitHub (minuti di Actions illimitati, necessari per
   controllare il calendario ogni 15 minuti) e carica questa cartella. Le chiavi restano
   nei Secrets: non sono mai nel codice né visibili ai visitatori.
2. Repository → Settings → Secrets and variables → Actions → aggiungi i 3 secret sopra.
   (Opzionale: variabile `GEMINI_MODEL` per cambiare modello.)
3. Actions → `analyze` → **Run workflow** per la prima esecuzione.
4. Cloudflare → Workers & Pages → Create → Pages → collega il repository.
   Build command: *(vuoto)* · Output directory: `site`.
5. L'Action controlla ogni 15 minuti: fa l'analisi completa ogni ~4 ore e circa 20 minuti
   dopo ogni notizia USD ad alto impatto; altrimenti termina in pochi secondi senza pubblicare.

## Eventi ad alto impatto

Fonte: export JSON settimanale di ForexFactory (eventi USD "High"). Per ogni evento:
scenario condizionale (dato sopra le attese → rialzista/ribassista per l'oro e perché),
inclinazione di Overmind con confidenza limitata al 65%, reazione reale dell'oro a 1h/4h,
esito ✓/✗ e accuratezza complessiva. Dopo l'uscita, la reazione del prezzo diventa il fattore
"Notizia recente" nel punteggio Daily/H4 (per 24h). Il feed non contiene il dato effettivo:
conta la reazione del mercato. Un evento visto per la prima volta dopo l'uscita non viene
valutato (nessuna previsione registrata prima).

## Come si legge la confidenza

Ogni fattore vota da −2 a +2 con un peso diverso per timeframe (vedi `engine/scoring.py`).
`confidenza = copertura dati × (60% accordo tra i fattori + 40% forza dei voti)`.
Sotto il 40% il sito mostra "segnali in conflitto". Finché il track record non è abbastanza
lungo, la % misura l'accordo tra i fattori, non una probabilità di successo.

## Sicurezza (cosa è stato fatto e testato)

- **Nessun backend esposto**: il sito è statico, non c'è server da attaccare né database.
- **Chiavi API** solo nei GitHub Secrets / `.env` (ignorato da git). Mai nel sito.
  Gli errori HTTP non includono mai URL o header; ogni messaggio passa da `redact()`;
  l'Action blocca la pubblicazione se trova una chiave nei file di output.
  Testato con chiavi finte: nessuna fuga.
- **Client HTTP**: solo HTTPS, solo host in allowlist (anche sui redirect), timeout,
  limite dimensione risposta. Test contro SSRF (metadata 169.254.x, porte, credenziali nell'URL).
- **XSS**: tutto il contenuto (anche il testo IA) è inserito con `textContent`, mai `innerHTML`.
  CSP restrittiva (`script-src 'self'` + solo TradingView, niente inline). Testato iniettando
  payload `<img onerror>` / `<script>` in ogni campo: nessuna esecuzione.
- **Robustezza**: risposte API malformate testate con fuzzing (3.000 risposte casuali × 4 fonti):
  zero crash. Una sezione con dati corrotti non blocca il resto della pagina.
- **Calendario**: titoli e valori del feed validati con whitelist di caratteri (anche perché
  finiscono nel prompt dell'IA: niente prompt injection via titoli); feed malformati testati
  con fuzzing (3.000 risposte casuali): zero crash.
- **Output IA** validato: schema JSON, tipi, lunghezze, caratteri di controllo/bidi rimossi.
- **Supply chain**: zero dipendenze Python; libreria grafico (Lightweight Charts 4.2.0) inclusa
  in locale e verificata contro il pacchetto npm (sha256 `46fc6953…dd28c2bdf`);
  GitHub Action fissata per SHA; permessi del workflow minimi.
- **Header HTTP** (`site/_headers`): CSP, frame-ancestors none, nosniff, no-referrer, HSTS.

## Stato delle fonti sul sito

Ogni fonte ha uno stato: OK, Parziale (dati salvati dell'ultima esecuzione riuscita),
Non disponibile, Non configurata. Il banner in alto segnala anche se l'Action è ferma
(dati più vecchi di 5 ore) o se mancano i prezzi (viene mostrata l'ultima analisi valida).
