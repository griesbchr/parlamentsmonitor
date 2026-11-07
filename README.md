# 🇦🇹 Parlamentsmonitor

Do Austria's parties vote the way they promised? The Parlamentsmonitor shows every decision
(*Beschluss*) of the Nationalrat in the current legislative period (XXVIII. GP), how each party
voted, and an AI assessment of whether that vote fits the party's 2024 election manifesto — with
the verbatim manifesto passage and the reasons the party gave in parliament.

**Live:** https://griesbchr.github.io/parlamentsmonitor/

![Python](https://img.shields.io/badge/Python-3.10+-green) ![Flask](https://img.shields.io/badge/Flask-3.x-blue) ![Data](https://img.shields.io/badge/Data-CC%20BY%204.0-lightgrey)

---

## Features

- **Overview** — contradictions with the own manifesto per party, latest contradictions
- **Topics** — the parliament's 18 topic areas: how each party voted, what it promised, all decisions
- **Party pages** — contradictions, fulfilled promises, voting partners and a per-topic breakdown
- **Wer stimmt mit wem?** — how often two parties vote alike in contested decisions
- **Bewertung falsch?** — every assessment links to a pre-filled GitHub issue for corrections
- **Decision pages** — plain-language summary, vote result, per-party manifesto check
  (*what the party said · what the manifesto says · AI assessment*), keywords, original documents
- **Filters** — topic, party contradictions, voting stance, session, document type, full-text search
- **Shareable links** — `#/beschluss/<id>`, `#/thema/<slug>`, `#/methodik`
- **Transparency** — every AI output is labelled; the methodology page explains sources and limits

---

## How it works

| Step | Script | What it does |
|---|---|---|
| 1 | `scraper.py` | Decisions, votes per club, tags and documents from the [Parliament Open Data API](https://www.parlament.gv.at/opendata) |
| 2 | `generate_summaries.py` | Short summaries for items without an official *Kurzinformation* (Claude Haiku) |
| 3 | `extract_manifestos.py` | Reads the manifesto PDFs with PyMuPDF: headings by font size, columns, paragraphs |
| 4 | `index_manifestos.py` | Splits the manifestos into ~1,300 verbatim sections and assigns policy areas |
| 5 | `fetch_statements.py` | Parlamentskorrespondenz reports per decision → each party's stated reasons (Claude Sonnet, Batch API) |
| 6 | `analyze_votes.py` | Per vote and party: the 3 most similar manifesto sections (local embeddings, `embeddings.py`) + law text + party reasons → score −2…+2 with explanation (Claude Haiku, Batch API) |
| 7 | `export_static.py` | Writes the site as static JSON + HTML to `site/` and pushes it to GitHub Pages |

Steps 3 and 4 run once (or when the manifestos change). Steps 1, 2, 5, 6 and 7 run weekly via
`weekly_update.py`; each step only processes what is new.

---

## Run locally

The project uses its own virtual environment, so its packages (e.g. fastembed for the
embeddings) don't interfere with other Python projects:

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py       # http://localhost:5000
```

`weekly_update.py` automatically runs the pipeline with `.venv` if it exists. The embedding model
(`intfloat/multilingual-e5-small`, ~470 MB) is downloaded from Hugging Face on first use.
Without fastembed, `analyze_votes.py` falls back to keyword matching (`--retrieval keyword`).

The local app shows the same data as the public site, plus embedded law documents.

### Update the data

```bash
python weekly_update.py            # scrape → summaries → statements → analysis → export + push
```

Requires `ANTHROPIC_API_KEY` (read from the environment or, on Windows, the user environment).
On this machine it runs every Friday night via the Windows Task Scheduler task
`ParlamentMonitor_WeeklyUpdate`.

### Rebuild the manifesto index

```bash
python extract_manifestos.py       # PDFs in manifestos/ → manifestos/*_clean.txt
python index_manifestos.py --reset # → manifesto_sections (clear vote_analyses first)
python analyze_votes.py --batch --reset
```

---

## Project structure

```
├── app.py                  Flask app + JSON API (also used by the static export)
├── templates/index.html    Single-page UI (works with the API or the static JSON files)
├── database.py             SQLite schema & helpers
├── scraper.py              Parliament API pipeline
├── generate_summaries.py   AI summaries for items without one
├── extract_manifestos.py   Layout-aware manifesto PDF extraction
├── index_manifestos.py     Manifesto sections + policy areas
├── fetch_statements.py     Party reasons from the Parlamentskorrespondenz
├── analyze_votes.py        Vote ↔ manifesto assessment
├── embeddings.py           Local embeddings for passage matching
├── export_static.py        Static export for GitHub Pages (incl. link-preview image)
├── weekly_update.py        Runs the weekly pipeline
├── manifestos/             Manifesto PDFs + extracted text (not in the repository)
├── parliament.db           SQLite database (not in the repository, built by the scripts)
└── site/                   Static export – checked out as the gh-pages branch
```

The website is published from the `gh-pages` branch of this repository; `export_static.py --push`
updates it. The code lives on `main`.

### Rebuilding the data from scratch

The database and the manifesto texts are not part of the repository. To rebuild them:

1. Save the 2024 election manifestos as PDFs in `manifestos/`:
   `oevp.pdf`, `spoe.pdf`, `fpoe.pdf`, `gruene.pdf`, `neos.pdf` (published on the parties' websites).
2. Run `weekly_update.py` (scrapes everything since the start of the XXVIII. GP on its first run),
   then `extract_manifestos.py`, `index_manifestos.py` and `analyze_votes.py --batch`.

---

## Visitor statistics

Off by default. To enable cookie-free counting, create a free [GoatCounter](https://www.goatcounter.com)
site and set `GOATCOUNTER_CODE` in `export_static.py`; the Impressum's privacy section adapts automatically.

---

## License

Code: [MIT](LICENSE)
Parliament data: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) — Österreichisches Parlament.
Manifesto quotes: © the respective parties, quoted for the purpose of comparison.
