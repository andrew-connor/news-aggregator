# News Aggregator — Plain-English Summary

This document walks through what the app is, where its pieces live, and how everything fits together. For exact field names and edge cases, see `news-aggregator-spec.md`.

## What it is

A personal, single-user news reader. Four times a day it pulls headlines from eight RSS feeds, stores them, and shows them on one web page. Once you've seen an article (or hidden it), it never reappears. If a feed breaks, an alert shows up in the same feed instead of failing silently.

## Where it lives

Everything runs on Google Cloud Platform, in the `us-central1` region (Iowa — closest to Minneapolis). The project ID is `news-aggregator-495416`. The whole thing fits in GCP free tiers, so monthly cost is effectively $0.

The cloud services in play:

- **Cloud Run** hosts the web app. It's a managed container service — you give it source code, it builds and runs it, and you get a public `*.run.app` URL. The service is named `news-aggregator`.
- **Firestore** (Native mode) is the database. Three collections: `articles` (every headline ever seen), `alerts` (errors surfaced to the UI), and `runs` (one record per fetch attempt).
- **Cloud Scheduler** runs four cron jobs that hit the app's `/fetch` endpoint at 5am, 9am, 1pm, and 5pm Central, every day. Daylight saving is handled automatically because the schedules are configured with the `America/Chicago` timezone, not a UTC offset.
- **Cloud Logging** captures everything the app prints to stdout. The app's `/logs` page reads back from this API.
- **Cloud Monitoring** has one alerting policy: if `/fetch` returns a non-200 or fails to respond, it emails `andrew.callaghan.connor@gmail.com`. This is the safety net for when the app is too broken to even write its own in-app alert.
- **Secret Manager** holds the shared-secret token that protects `/fetch`.

## How auth works

The UI itself has no login. The Cloud Run URL is public — anyone who has the URL can read articles and click hide buttons. The original spec called for IAP (Identity-Aware Proxy) to restrict it to one Google account, but IAP on a `*.run.app` URL needs an HTTPS load balancer (~$18/mo). The owner accepted that trade-off: the URL is obscure enough for a personal news reader.

The `/fetch` endpoint is different. It's a write operation that triggers a fetch run, so it's protected by a shared-secret token stored in Secret Manager. Cloud Scheduler passes the token as a query parameter (`?token=…`) when it triggers a run, and the app compares it against the secret using a constant-time comparison. The same token is what makes the "Refresh now" link in the UI footer work.

## The code

The repo has two Python files plus templates:

- **`fetcher.py`** — knows nothing about HTTP or FastAPI. It defines the list of sources, the URL canonicalizer, the article ID hash, and the `run_fetch` function that orchestrates a fetch attempt across all sources concurrently. Uses `feedparser` directly (the library handles its own HTTP via `urllib`); the call is wrapped in `asyncio.to_thread` and `asyncio.wait_for` to keep things async-friendly with a 30-second timeout per source.
- **`main.py`** — the FastAPI app. Defines the routes (`/`, `/hide`, `/unhide`, `/hide-all`, `/hide-selected`, `/keep-recent`, `/undo-hide`, `/archive`, `/fetch`, `/logs`), reads the secret token, and renders templates. It imports `run_fetch` from `fetcher.py` and calls it when `/fetch` is hit with a valid token.
- **`templates/index.html`** — the article view. Server-rendered Jinja2. Has small bits of inline JavaScript for the hide/undo/open buttons (which call the API and update the DOM in place).
- **`templates/archive.html`** — the full-history view. Server-rendered list of every article ever collected, paginated 100 per page.
- **`templates/logs.html`** — the log viewer. Same approach: server-rendered with a filter form.
- **`tests/`** — `pytest` coverage for the four areas where bugs would be silent: URL canonicalization, the never-come-back rule for articles, the 24-hour alert dedup window, and the 48-hour staleness check.

A few small support files: `Procfile` tells the Cloud Run buildpack how to start the app (uvicorn), `requirements.txt` lists Python dependencies, and `.gcloudignore` keeps junk out of the build upload.

## How a fetch run flows

When `/fetch?token=…` is hit (by Scheduler or by you clicking "Refresh now"):

1. The token is checked against Secret Manager. Wrong token → 403, done.
2. A new `runs` document is created with a run ID like `20260506-1700` (UTC timestamp). Status starts as "running".
3. All eight sources are fetched **in parallel** with `asyncio.gather`. Each one is independent — a slow or broken source does not delay the others.
4. For each source:
   - `feedparser.parse(url)` runs in a worker thread with a 30-second timeout.
   - If the request times out, fails the network layer, returns a 4xx/5xx, or returns no entries → a `fetch_failed` (or `parse_error`) alert is created and the source is marked failed.
   - Otherwise, the staleness check looks at the newest item's timestamp. If it's more than 48 hours old, a `stale_feed` alert is created — but the run continues and we still ingest whatever is there.
   - For each entry: canonicalize the URL (lowercase scheme/host, strip query and fragment, strip trailing slash), hash it to get a 16-character document ID, and write it to `articles` only if it doesn't already exist. This is the never-come-back rule — once an article ID is in the database, re-fetching it is a no-op forever.
5. Alerts have a dedup rule: before creating one, the code checks whether an unhidden alert with the same `(source, error_type)` already exists from the last 24 hours. If so, no new alert. This prevents the UI from drowning in repeat alerts when a source is down for a while. Hidden alerts don't block creation, so dismissing an alert lets a new one show up next time.
6. The `runs` doc is updated with `finished_at`, the per-source results, and an overall status of `success`, `partial_failure`, or `failure`.

## How the UI flows

When you load `/`:

1. The app queries Firestore for unhidden alerts (newest first), unhidden articles (newest first, capped at 1000), and the most recent finished run.
2. It renders `index.html`. The top of the page has a **sticky header** wrapped in `position: sticky; top: 0` — containing the "News" title, an article count line ("47 articles"), and a toolbar with three bulk-action buttons ("Hide all", "Hide selected", "Undo"). As you scroll, this whole header stays pinned to the top of the viewport with a subtle drop shadow, so the count and actions are always reachable.
3. Below the sticky header come alerts (yellow left-border boxes) and then articles (flat list). When both alerts and articles exist, an "Articles" section label separates them. Each article row has a checkbox on the left and "Open" + "Hide" buttons on the right.
4. The footer shows the last-updated timestamp (converted from UTC to Central Time via a Jinja filter), a "Refresh now" link with the token baked in, links to `/logs` and `/archive`, and a toggle for showing hidden items.
5. **Bulk actions in the sticky toolbar:**
   - "Hide all" — shows a browser `confirm()` dialog; on OK it POSTs to `/hide-all`, which marks every currently-unhidden article as hidden in a Firestore batch and tags them all with the same `hide_batch_id`. The JavaScript then removes the article rows from the DOM in place (no page reload) and updates the article count.
   - "Hide selected (N)" — POSTs the list of checked IDs to `/hide-selected`; same batch-id behavior. The "(N)" suffix is updated live as boxes are checked. Empty selection just alerts and does nothing.
   - "Undo" — POSTs to `/undo-hide`, which finds the most recently hidden article, reads its `hide_batch_id`, and unhides every article with that ID. So a single hide restores one article, and a "Hide all", "Hide selected", or "Hide word" restores the whole batch. The page reloads to show the restored items in the right order.
   - Word input + "Hide word" — type a word and click the button (or press Enter). The JavaScript scans the currently-displayed article titles using a regex `\b<word>\b` with the `i` flag, collects matching IDs, and POSTs them to `/hide-selected`. So matching is purely client-side (no DB scan) and scope is "what's on the page" — articles past the 1000-row cap stay untouched. All matches share one `hide_batch_id`, so Undo restores the group in a single click. An alert fires if no visible titles match.
   - Number input + "Keep recent" — type a count `N` and click the button (or press Enter). After a confirm prompt, the JavaScript POSTs `{count: N}` to `/keep-recent`. Unlike "Hide word", this is **server-side and global**: the endpoint queries all unhidden articles ordered by `fetched_at` descending, keeps the first `N`, and hides the rest (including any past the 1000-row display cap) in one batch with a shared `hide_batch_id`. The page reloads to show the kept set, and Undo restores the whole action. If `N` is missing/less than 1, or nothing ends up hidden, the user is alerted and nothing changes.
6. **Per-row buttons:**
   - "Hide" — POSTs to `/hide` with a fresh `hide_batch_id`; row is removed from the DOM.
   - "Open" — opens the article URL and hides the row. On desktop it synthesizes a Ctrl/Cmd+click on a temporary anchor to open in a background tab; on touch devices it falls back to `window.open` (foreground, since mobile browsers don't expose background-tab opening to JS).
7. **"Refresh now" footer link** — calls `/fetch?token=…` via in-page `fetch()` rather than navigating, shows "Refreshing..." while the run completes (10–30s), then reloads the page. Ctrl/Cmd+click or middle-click falls back to a normal anchor (opens the raw `/fetch` URL in a new tab) for power-user use.
8. The `/?show=hidden` variant skips the `hidden=false` filter so you can see everything you've dismissed and unhide if needed.

## How the archive page flows

`/archive` is the way to browse every article ever collected, including ones older than the 1000-row cap on the main page. It queries Firestore for articles ordered by `fetched_at` descending, 100 per page. Pagination is cursor-based: each page passes the oldest article's `fetched_at` as a `?before=<iso8601>` query param to the next page, and the server queries `where fetched_at < before`. This is much more efficient on Firestore than offset-style pagination, which would have to read and skip every prior document.

Each row shows the title (linked to source), the source name, the fetched date in Central Time, and a "hidden" tag if applicable. There's no Hide button on the archive — it's a read-only view; if you want to hide or unhide from the archive, use `/?show=hidden` on the main page (which surfaces hidden items with unhide controls).

## How the logs page flows

`/logs` calls the Google Cloud Logging API with a filter built from query parameters: `run`, `source`, `level`, `since` (1 hour / 1 day / 1 week / 30 days). Each alert on the main page links directly to `/logs?run={run_id}` so one click takes you to that exact run's lines.

## Deploying

From the project directory:

```powershell
gcloud run deploy news-aggregator `
  --source . `
  --project news-aggregator-495416 `
  --region us-central1 `
  --quiet
```

Cloud Run buildpacks read `requirements.txt` and `Procfile` and produce a container automatically — there's no Dockerfile. The first deploy of a new revision takes a couple of minutes; afterwards you can hit the URL.

## Triggering a fetch manually

Two options:

- Click "Refresh now" in the page footer — it has the token pre-baked into the link.
- Hit `https://<service-url>/fetch?token=<token>` directly. Bookmarkable.

## Debugging a failure

1. Check the alerts at the top of `/`. Each has a "View logs" link that filters `/logs` to that exact `run_id`.
2. If the app itself is broken (returning 500s or down), the email-alert policy will fire and you'll get mail.
3. For build failures during `gcloud run deploy`, the URL in the deploy output goes to the Cloud Build log; that shows the buildpack output line by line.

## Things deliberately not built

No article body or preview, no search, no multiple users, no email digest, no cross-source dedup, no auto-expiry. The app is intentionally a single-page list with hide buttons — anything more is out of scope.
