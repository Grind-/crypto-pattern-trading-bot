# Crypto Pattern AI — Architektur

## Übersicht

Die Anwendung besteht aus zwei spezialisierten Claude-Instanzen, die dieselbe
Subscription/API-Verbindung nutzen aber völlig getrennte Verantwortlichkeiten haben.

```
┌─────────────────────────────────────────────────────────────┐
│                        claude-proxy                         │
│  (claude.exe + OAuth-Subscription — gemeinsame Verbindung)  │
└──────────────────┬──────────────────────┬───────────────────┘
                   │                      │
         ┌─────────▼──────────┐  ┌───────▼────────────┐
         │   Trading Agent    │  │    News Agent       │
         │  claude_analyst.py │  │  news_analyst.py    │
         │  (pro User-Call)   │  │  (zentral, 1×/Std.) │
         └────────────────────┘  └────────────┬───────┘
                                               │ schreibt
                                  ┌────────────▼──────────────┐
                                  │  knowledge/news/           │
                                  │  intelligence.json         │
                                  │  (geteilt, alle User)      │
                                  └────────────┬──────────────┘
                                               │ liest (alle)
                                  ┌────────────▼──────────────┐
                                  │   Trading Agent Prompts    │
                                  │   (User A, User B, ...)    │
                                  └───────────────────────────┘
```

**Schlüsselprinzip:** Der News Agent läuft **einmal zentral** — unabhängig davon,
wie viele User die App nutzen. Seine Ergebnisse sind ein gemeinsam genutzter
Dienst für alle Trading-Agent-Instanzen. Kein User-spezifischer Code, keine
User-Credentials. Nur der Platform-Proxy wird verwendet.

---

## Trading Agent (`app/claude_analyst.py`)

**Rolle:** Quantitativer Handelsspezialist

**Systemrole:**
> „You are an expert quantitative cryptocurrency trading analyst (Trading Agent).
> Your responsibilities: analyse technical indicators and price action to identify
> high-probability trading patterns; generate precise BUY/SELL signals with clear
> reasoning; design, run, and evaluate backtesting simulations; execute live trades
> via the Binance API."

**Zuständigkeiten:**
- Backtesting-Simulationen ausführen und bewerten
- Live-Trading-Signale auf Basis technischer Indikatoren generieren (RSI, MACD, Bollinger Bands, Volumen)
- Marktscans durchführen und das beste Symbol für Live-Trading auswählen
- Trades via Binance API ausführen (Buy/Sell/Hold)
- Muster aus abgeschlossenen Simulationen lernen (`synthesize_learnings`)
- Patterns aus User-Lernbereichen in den Core promoten

**Kontextquellen (werden parallel geladen):**
1. Knowledge Base (Patterns des Users + Core-Patterns)
2. Live-Marktdaten (Fear & Greed + RSS-Headlines via `news_fetcher`)
3. News Intelligence (stündliche Analyse des News Agents)

---

## News Agent (`app/news_analyst.py`)

**Rolle:** Marktintelligenz-Analyst

**Systemrole:**
> „You are a cryptocurrency market intelligence analyst (News Agent).
> Your sole responsibility is to analyse news, social sentiment, and market data
> to identify emerging opportunities and risks across Binance USDC trading pairs.
> You do NOT execute trades, run simulations, or evaluate technical indicators —
> that is the Trading Agent's job."

**Zuständigkeiten:**
- Stündlicher Hintergrundjob (startet 30s nach App-Start)
- Sammelt Daten in zwei Phasen parallel:

  **Phase 1 — Strukturierte APIs:**
  - **Fear & Greed Index** (alternative.me)
  - **RSS-Feeds** (CoinTelegraph, CoinDesk, Decrypt, CryptoPotato)
  - **CoinGecko Trending** (Top-10-Coins der letzten 24h)

  **Phase 2 — Echte Internet-Recherche:**
  - **Google News RSS-Suche** — 3–5 parallele Suchanfragen:
    - "crypto market news today"
    - "bitcoin price news"
    - "ethereum news"
    - Für die Top-3-Trending-Coins je eine gezielte Suche
  - **Reddit r/CryptoCurrency** — Hot Posts mit Scores (Community-Sentiment)
  - Ergebnisse werden dedupliziert und nach Quelle strukturiert

- Bewertet alle Quellen gleichwertig mit Claude und erzeugt strukturierte Intelligence:
  - Markt-Sentiment (bullish/bearish/neutral)
  - Top 2–5 Chancen mit Katalysator, Konfidenz und Zeitrahmen
  - Warnungen (Regulation, Makro-Events, Überhitzung)
  - Schlüssel-Headlines
- Speichert Ergebnis in `knowledge/news/intelligence.json` (inkl. `sources_used`-Liste)
- Gibt Kontext an Trading Agent weiter (max. 6h alt, sonst ignoriert)
- Jede Opportunity trägt die Quellenangabe (`source: "Google News / Reddit / RSS"`)

**Kein Zugriff auf:**
- Binance-API (kein Trading)
- Knowledge Store (Patterns)
- Simulationsdaten

---

## Knowledge Store — 3-Tier-Architektur (`app/knowledge_store.py`)

```
knowledge/
  core/
    patterns.json        Read-only — nur per Admin-Promotion beschreibbar
                         Enthält: global_rules, interval_notes, symbol_patterns
  users/
    {username}/
      patterns.json      Pro-User-Patterns — Claude schreibt nur hier
      sim_log.json       Simulationslog des Users (max. 100 Einträge)
  news/
    intelligence.json    Stündliche News-Agent-Analyse (wird automatisch überschrieben)
```

**Schreibrechte:**
| Wer | Wohin |
|---|---|
| Trading Agent (nach Simulation) | `users/{username}/` |
| News Agent (stündlich) | `knowledge/news/` |
| Admin-Promote-Endpoint | `core/` |
| Niemals automatisch | `core/` direkt |

**Promotion-Workflow:**
1. User simuliert → Trading Agent schreibt Patterns in `users/{username}/`
2. Admin triggert `POST /api/admin/knowledge/promote` mit `{type:"rules"}` oder `{type:"symbol"}`
3. Claude mergt User-Patterns in `core/` — dauerhaft für alle sichtbar

---

## Datenpersistenz

| Daten | Speicherort | Überlebt Neustart |
|---|---|---|
| Nutzerkonten | SQLite (`data/crypto_pattern_ai.db`) | ✅ (Volume-Mount) |
| Live-Trading-Zustand | SQLite (`live_states`-Tabelle) | ✅ |
| Binance API-Keys | SQLite (`users.binance_api_key/secret`) | ✅ |
| Simulationen | SQLite (`simulations` + `simulation_details`) | ✅ |
| Muster/Patterns | JSON (`knowledge/users/{user}/`) | ✅ (Volume-Mount) |
| Core-Knowledge | JSON (`knowledge/core/`) | ✅ |
| News Intelligence | JSON (`knowledge/news/intelligence.json`) | ✅ |

---

## Konfigurierbare Algorithmus-Parameter

Alle drei Parameter werden in `live_states` gespeichert und beim Auto-Resume wiederhergestellt:

| Parameter | Default | Beschreibung |
|---|---|---|
| `min_confidence` | 55 % | BUY-Signal wird zu HOLD, wenn Claudes Konfidenz darunter liegt |
| `sl_atr_mult` | 1.5 | Stop-Loss = sl_atr_mult × ATR / Einstandspreis |
| `tp_atr_mult` | 2.5 | Take-Profit = tp_atr_mult × ATR / Einstandspreis |

**Voting-Matrix BUY-Schwellen (regime-spezifisch):**

| Regime | BUY-Schwelle |
|---|---|
| BULL_TREND | ≥ 0.8 |
| RANGING | ≥ 1.0 |
| BEAR_TREND | ≥ 1.2 |
| HIGH_VOLATILITY | geblockt (999) |

---

## Auto-Resume nach Neustart

Beim Start (`_auto_resume_all`):
1. Liest alle User-Datensätze aus SQLite
2. Prüft `live_states.was_running = True`
3. Lädt Binance-Keys aus `users.binance_api_key/secret` (Fallback: `live_states`)
4. Validiert Keys gegen Binance-API
5. Startet `_live_loop` als asyncio-Task mit gespeicherten Algo-Parametern
6. Bei Validierungsfehler: `was_running = False` setzen, Keys bleiben erhalten

---

## API-Endpunkte (Auswahl)

| Endpunkt | Beschreibung |
|---|---|
| `GET /api/news/intelligence` | Letzte News-Agent-Analyse |
| `POST /api/news/refresh` | News-Cycle manuell anstoßen (Admin) |
| `GET /api/live/credentials` | Gespeicherte Binance-Key-Info (kein Plaintext) |
| `GET /api/admin/knowledge/status` | Knowledge-Store-Übersicht |
| `POST /api/admin/knowledge/promote` | Patterns in Core promoten |
| `POST /api/live/start` | Live-Trading starten |
| `POST /api/live/stop` | Live-Trading stoppen (Keys bleiben) |

---

## Container-Setup

```yaml
services:
  claude-proxy:    # Claude-Subscription-Bridge
  crypto-pattern-ai:  # FastAPI-App (Port 7891 → 8080)
```

Volumes:
- `data/` → SQLite-Datenbank
- `knowledge/` → JSON-Knowledge-Store (alle Tiers)
