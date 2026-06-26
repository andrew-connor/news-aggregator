# News Aggregator — Spec for sign-off

## 1. Goal

A personal, single-user news aggregator that pulls headlines from a fixed set of public news sources four times a day, presents them on a single web page accessible from any device, and never shows the same article twice — even after dismissal.

## 2. Sources

Eight RSS feeds, each source's broadest "headlines" feed:

| Source | Feed URL |
| --- | --- |
| MPR News | `https://www.mprnews.org/feed/homepage` |
| NPR News | `https://feeds.npr.org/1001/rss.xml` |
| France 24 (English) | `https://www.france24.com/en/rss` |
| Al Jazeera | `https://www.aljazeera.com/xml/rss/all.xml` |
| BNO News | `https://bnonews.com/index.php/feed/` |
| El País (English) | `https://feeds.elpais.com/mrss-s/pages/ep/site/english.elpais.com/portada` |
| AllAfrica | `https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf` |
| Asia Times | `https://asiatimes.com/feed` |

AP, BBC, and CBC intentionally excluded. CBC blocks requests from GCP Cloud Run IP ranges.

## 3. Schedule

Four Cloud Scheduler jobs hitting the same `/fetch` endpoint, all with timezone `America/Chicago`:

- 5:00 AM Central
- 9:00 AM Central
- 1:00 PM Central
- 5:00 PM Central

DST handled automatically by Cloud Scheduler. The same endpoint can be triggered manually (see §7).

## 4. Architecture

| Component | Service | Purpose |
| --- | --- | --- |
| Web app | Cloud Run (Python, FastAPI) | Renders the UI, handles fetch and hide endpoints |
| Scheduler | Cloud Scheduler (×4) | Triggers `/fetch` at 5am, 9am, 1pm, and 5pm Central |
| Database | Firestore (Native mode) | Stores articles, alerts, runs |
| Logging | Cloud Logging | Captures stdout from Cloud Run; `/logs` reads from this |
| Monitoring | Cloud Monitoring | Emails on scheduler failure |
| Auth | None (public URL) | UI is publicly accessible via obscure `*.run.app` URL; IAP excluded due to load balancer cost |
| Secrets | Secret Manager | Holds the `/fetch` token |

Expected cost: $0 (free tiers).

## 5. Data model

### `articles` collection

Document ID: SHA-256 of canonical URL (truncated to 16 hex chars).

| Field | Type | Notes |
| --- | --- | --- |
| `url` | string | Canonicalized (tracking params stripped) |
| `original_url` | string | Original URL as received |
| `title` | string | Headline |
| `source` | string | e.g., "NPR News" |
| `published_at` | timestamp | From feed if available |
| `fetched_at` | timestamp | First time we saw it |
| `hidden` | bool | Default false |
| `hidden_at` | timestamp \| null | When you clicked hide |
| `hide_batch_id` | string \| null | UUID grouping a hide event; single hides get a unique ID, "Hide all" gives every article in the batch the same ID. Used by Undo to restore the whole batch. |

Once a document is created it is never deleted. Re-fetching the same URL is a no-op. This implements the never-come-back rule.

### `alerts` collection

Document ID: auto.

| Field | Type | Notes |
| --- | --- | --- |
| `source` | string | e.g., "MPR News" or "system" |
| `error_type` | string | `fetch_failed`, `stale_feed`, `parse_error`, `internal_error` |
| `message` | string | Human-readable summary |
| `run_id` | string | Links to `/logs?run=…` |
| `created_at` | timestamp | |
| `hidden` | bool | Default false |
| `hidden_at` | timestamp \| null | |

**Dedup rule**: before creating an alert, query for an unhidden alert with the same `(source, error_type)` created within the last 24 hours. If one exists, skip creating a new one. If the existing one was hidden, a new alert may be created.

Alerts are subject to soft-delete (hide) but **not** to the never-come-back rule — they can recur.

### `runs` collection

Document ID: `run_id` (e.g., `20260505-1700`).

| Field | Type |
| --- | --- |
| `started_at` | timestamp |
| `finished_at` | timestamp \| null |
| `triggered_by` | `"schedule"` \| `"manual"` |
| `status` | `"success"` \| `"partial_failure"` \| `"failure"` |
| `per_source` | map of source → `{ ok: bool, article_count: int, error: string \| null }` |

The most recent run's `finished_at` is shown on the main page as "Last updated."

## 6. Behaviors

### Fetch run

Triggered by `GET /fetch` (scheduler or manual). For each source, in parallel:

1. Download the RSS feed (30s timeout).
2. Parse with `feedparser`.
3. **Staleness check**: if the newest item is more than 48 hours old at fetch time, create a `stale_feed` alert for this source and continue. Don't fail the run.
4. For each item:
   - Canonicalize URL (strip query params).
   - Hash → document ID.
   - If document doesn't exist in `articles`, create it.
   - If it exists, skip.

On any per-source exception (network error, parse error), create a `fetch_failed` alert for that source with the error class in the message. Other sources continue independently.

On any outer-level exception (database failure, etc.), attempt to create an `internal_error` alert under source `system`. Always finalize the `runs` doc with status.

### URL canonicalization

- Lowercase scheme + host
- Strip everything from `?` onward (no whitelist initially; we'll add exceptions only if real articles break — see §11)
- Remove trailing slash
- Remove fragment

### Hide / unhide

- `POST /hide` with `{type: "article" | "alert", id: "..."}` sets `hidden=true`, `hidden_at=now`. For articles, also sets a unique `hide_batch_id` (UUID) so Undo can find this single hide.
- `POST /unhide` reverses it (clearing `hide_batch_id` for articles).
- Main page has a "Show hidden" toggle for recovery.

### Hide all

- `POST /hide-all` hides every currently visible article in one request. Alerts are not affected.
- The UI presents a confirmation prompt before sending the request ("Hide all visible articles?").
- All articles hidden in the same call share a `hide_batch_id` so Undo can restore them as a group.

### Hide selected

- Each article row has a checkbox. The user checks any number, then clicks "Hide selected".
- `POST /hide-selected` with `{ids: [...]}` hides every article in the list in one Firestore batch and shares a single `hide_batch_id` so Undo restores the whole selection as a group.
- The button alerts the user if nothing is selected.

### Hide by word

- The sticky toolbar includes a text input and a "Hide word" button. The user types a word and clicks the button (or presses Enter in the input).
- Matching is done **client-side** against currently-visible article titles using a JavaScript regex `\b<escaped-word>\b` with the `i` flag. So "fire" matches "Fire" and "FIRE" but not "firearm" or "campfire".
- Scope is "what's on the page" — articles that appear only in `/archive` (past the 1000-article cap) are not touched. This keeps the action predictable: the user can see what's being hidden.
- The collected IDs are POSTed to the existing `/hide-selected` endpoint, so all matched articles share a single `hide_batch_id` and Undo restores them as a group.
- The user sees an alert if no visible headlines match.

(A "bulk open" companion was considered but not built: both Firefox and Chrome cap new tabs to one per user gesture, so JavaScript can't reliably open multiple background tabs from a single click. The per-row "Open" button remains the working way to open + hide articles one at a time.)

### Keep most recent N

- The sticky toolbar includes a number input and a "Keep recent" button. The user enters a count `N` and clicks the button (or presses Enter in the input).
- `POST /keep-recent` with `{count: N}` is a **server-side, global** operation: it queries all unhidden articles ordered by `fetched_at` descending, keeps the first `N`, and hides the rest in one Firestore batch. Unlike "Hide word" (which is page-scoped), this acts on every unhidden article — including those past the 1000-article display cap — so the kept set is exactly the `N` newest stories overall.
- All hidden articles share a single `hide_batch_id`, so Undo restores the whole action as a group.
- The UI confirms before sending ("Keep the N most recent stories and hide the rest?"), then reloads the page so the kept set is shown. If `N` is missing or less than 1, the button alerts and does nothing; if `N` is already ≥ the number of unhidden articles, nothing is hidden and the user is told.

### Undo last hide

- `POST /undo-hide` finds the article with the most recent `hidden_at`, reads its `hide_batch_id`, and unhides every article with that batch ID. A single hide restores one article; a "Hide all" restores all articles from that batch.
- If there are no hidden articles, the endpoint is a no-op.
- The UI button alerts the user if there's nothing to undo.

## 7. Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/` | Public | Main page: visible articles + alerts, last-run timestamp |
| `GET` | `/?show=hidden` | Public | Same page including hidden items |
| `POST` | `/hide` | Public | Hide an article or alert |
| `POST` | `/unhide` | Public | Unhide an article or alert |
| `POST` | `/hide-all` | Public | Hide all currently visible articles |
| `POST` | `/hide-selected` | Public | Hide a list of article IDs as a single batch |
| `POST` | `/keep-recent` | Public | Keep the N most recent articles, hide all others |
| `POST` | `/undo-hide` | Public | Restore the most recent hide batch |
| `GET` | `/archive` | Public | Paginated view of every article ever collected |
| `GET` | `/logs` | Public | Log viewer (filterable) |
| `GET` | `/fetch?token=…` | Shared secret | Trigger a fetch run |

`/fetch` uses a shared-secret query param so that only authorized callers (Cloud Scheduler and the user via the "Refresh now" link) can trigger a fetch run; the rest of the app is public. The token lives in Secret Manager and is injected into both Cloud Run and Scheduler config.

## 8. The `/logs` page

Reads from Cloud Logging via the `google-cloud-logging` API. Renders a simple table:

| Time | Level | Source | run_id | Message |

Filter controls (querystring):

- `run` — single `run_id`
- `source` — source name or `system`
- `level` — info / warning / error
- `since` — last hour / day / week / 30 days (Cloud Logging free-tier retention)

Each alert on the main page links to `/logs?run={run_id}` — one click takes you to exactly that run's log lines.

## 8a. The `/archive` page

Reads from Firestore and shows every article ever collected (visible and hidden), sorted newest first. 100 articles per page, cursor-paginated by `fetched_at` via a `?before=<iso8601>` query param.

Each row shows title (linked to source URL, opens in new tab), source name, fetched date in Central Time, and a "hidden" tag if the article has been hidden.

A "Back to main" link returns to `/`.

## 9. UI notes

Single page, mobile-friendly. The page header — the "News" title, the article count line, and the bulk-action toolbar ("Hide all", "Hide selected", "Undo") — is wrapped in a `position: sticky; top: 0` container. As the user scrolls, this whole header stays pinned to the top of the viewport with a subtle drop shadow, so the buttons and current count are always reachable. Alerts and articles scroll behind the pinned header.

The article count (e.g., "47 articles") is server-rendered on page load and updated client-side whenever an article is hidden or "Hide all" is clicked, so it always reflects what's actually displayed.

Below the sticky header come alerts (highlighted with a warning-colored bar) followed by articles, sorted newest first. When both alerts and articles are present, an "Articles" section label separates them. Each article row:

- A checkbox (used by "Hide selected")
- Title (linked to source URL, opens in new tab)
- Source name
- "Open" button — opens the article and hides the row in one step. On desktop it synthesizes a Ctrl/Cmd+click on a hidden anchor to open in a background tab; on touch devices it falls back to `window.open` (which opens in the foreground, since mobile browsers don't expose background-tab opening to JavaScript).
- "Hide" button

The toolbar in the sticky header groups the bulk actions. "Hide all" prompts for confirmation, then calls `POST /hide-all` and removes every article row without a full reload. "Hide selected" acts on whichever checkboxes are ticked and shows a live count in the button label (e.g. "Hide selected (3)"). "Undo" calls `POST /undo-hide` and reloads the page. A small text input and "Hide word" button let the user hide every article whose title contains a given word as a whole-word match (case-insensitive); pressing Enter in the input triggers the same action. A number input and "Keep recent" button let the user keep the N newest stories and hide all the rest (a confirmed, server-side global action that reloads the page).

The main page caps display at 1000 articles; the `/archive` page paginates through everything ever collected.

Footer shows: last run timestamp, manual-refresh link, link to `/logs`, link to `/archive`, link to toggle hidden items.

The "Refresh now" link calls `/fetch?token=…` via in-page `fetch()` (not a new tab), shows "Refreshing..." while the server-side fetch runs (10–30s), then reloads the page so the new articles appear. Ctrl/Cmd+click or middle-click on the link falls back to normal anchor behavior (opens the raw `/fetch` URL in a new tab) for power-user use.

## 10. Operational

- **Manual trigger**: `https://<your-domain>/fetch?token=…` — bookmarkable.
- **Belt-and-suspenders email alert**: Cloud Monitoring policy fires when `/fetch` returns non-200 or doesn't respond. Email goes to `andrew.callaghan.connor@gmail.com`. Catches the case where the app is too broken to post an in-app alert.
- **Retention**: Articles and alerts kept forever (per your choice). Cloud Logging retains 30 days on free tier.
- **Backups**: Firestore has automatic point-in-time recovery on the free tier.

## 11. Out of scope

Intentionally not included in v1:

- Article body, summary, or preview (titles + links only)
- Search or filter on the main view
- Multiple users
- Mobile native app (works fine in mobile browsers)
- Cross-source duplicate detection
- Email or push digest

## 12. Resolved decisions

The three open questions in earlier drafts are answered:

1. **URL canonicalization whitelist** — none. Start with strip-everything (per §6) and add exceptions only if specific articles break.
2. **Domain** — default `*.run.app` URL provided by Cloud Run.
3. **Notification email** — `andrew.callaghan.connor@gmail.com` (see §10).

Spec signed off. Deployed and running.
