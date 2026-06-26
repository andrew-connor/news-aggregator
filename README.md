# News Aggregator

A personal, single-user news reader. Four times a day it pulls headlines from eight RSS feeds, stores them, and presents them on a single web page you can open from any device. Once you've seen an article — or hidden it — it never comes back. If a feed breaks, an alert shows up in the page itself rather than failing silently.

It runs entirely on Google Cloud Platform free tiers, so it costs effectively nothing to operate.

## Stack

- **Python 3.11+** with **FastAPI** on **Cloud Run** (source-deployed via buildpacks — no Dockerfile)
- **Firestore** (Native mode) for storage
- **Cloud Scheduler** for the four daily fetches
- **Cloud Logging** behind the in-app `/logs` viewer
- **Cloud Monitoring** for a failure-email safety net
- **Secret Manager** for the `/fetch` token
- `feedparser` for RSS

## How it works

A fetch run is triggered by `GET /fetch` — either by Cloud Scheduler (5am, 9am, 1pm, 5pm Central) or by the "Refresh now" link in the page footer. All eight sources are fetched in parallel; a slow or broken one never holds up the others. For each article the URL is canonicalized (query string and fragment stripped), hashed to a document ID, and written to Firestore only if it's new — that's the never-come-back rule. A source that times out, errors, or returns a feed more than 48 hours stale raises an alert that surfaces on the page, with a 24-hour dedup window so a persistent outage doesn't bury the list.

The main page is a single server-rendered list with a sticky toolbar: hide individual articles, hide a selection, hide everything matching a word, keep only the N most recent, and undo the last hide (single hides and bulk hides are grouped so undo restores the whole batch). An `/archive` view paginates through everything ever collected, and `/logs` reads back from Cloud Logging filtered to a specific run.

For the full walkthrough — data model, every endpoint, and the exact UI behaviors — see the two design docs:

- **[news_aggregator_summary.md](news_aggregator_summary.md)** — plain-English tour of how the pieces fit together
- **[news-aggregator-spec.md](news-aggregator-spec.md)** — the precise spec: data model, endpoints, behaviors

## Repository layout

| Path | What it is |
|------|------------|
| `fetcher.py` | Source list, URL canonicalizer, article-ID hash, and the parallel `run_fetch` orchestration. Knows nothing about HTTP/FastAPI. |
| `main.py` | The FastAPI app: routes, templates, token check. Imports and calls `run_fetch`. |
| `templates/` | Jinja2 templates for the article view, archive, and log viewer. |
| `tests/` | `pytest` coverage for the four areas where a bug would be silent (see below). |
| `Procfile` | Tells the Cloud Run buildpack how to start uvicorn. |
| `requirements.txt` / `requirements-dev.txt` | Runtime and development dependencies. |
| `.gcloudignore` | Keeps local junk out of the build upload. |

## Running and deploying

Deploy from the project directory:

```powershell
gcloud run deploy news-aggregator `
  --source . `
  --region us-central1 `
  --quiet
```

Cloud Run buildpacks read `requirements.txt` and `Procfile` and produce the container automatically. A fetch can be triggered any time by hitting `/fetch?token=…` (bookmarkable) or the footer's "Refresh now" link.

## Development

- Format with **black**, lint with **ruff** — no style debates.
- Type hints on all function signatures.
- Use async for real I/O wins (feed fetches, Firestore), not reflexively everywhere.

Tests live in `tests/` and run with `pytest`. Priority coverage is the logic that fails quietly:

- URL canonicalization
- the never-come-back rule for articles
- the 24-hour alert dedup window
- the 48-hour staleness check

**Firestore indexes — check before adding queries.** Any query that combines a `where` with an `order_by` on a different field, or stacks `where` clauses across fields, needs a composite index. A missing index returns a `FailedPrecondition` 500, which front-end code that only checks `res.ok` will swallow silently. List existing indexes with `gcloud firestore indexes composite list` before adding a query, create the index if it's missing, and wait for it to reach `READY` (2–5 minutes) before redeploying.

## Scope

Deliberately a single-page list with hide controls. Not included by design: article bodies or previews, search/filter on the main view, multiple users, email or push digests, cross-source dedup, and article auto-expiry.
