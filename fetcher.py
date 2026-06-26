import asyncio
import calendar
import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

SOURCES: dict[str, str] = {
    "MPR News": "https://www.mprnews.org/feed/homepage",
    "NPR News": "https://feeds.npr.org/1001/rss.xml",
    "France 24": "https://www.france24.com/en/rss",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "BNO News": "https://bnonews.com/index.php/feed/",
    "El País": "https://feeds.elpais.com/mrss-s/pages/ep/site/english.elpais.com/portada",
    "AllAfrica": "https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf",
    "Asia Times": "https://asiatimes.com/feed",
}

STALENESS_HOURS = 48
ALERT_DEDUP_HOURS = 24


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return f"{scheme}://{host}{path}"


def article_id(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode()).hexdigest()[:16]


def make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")


def log_struct(severity: str, message: str, **fields: object) -> None:
    print(json.dumps({"severity": severity, "message": message, **fields}), flush=True)


async def _should_create_alert(
    db: firestore.AsyncClient, source: str, error_type: str
) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ALERT_DEDUP_HOURS)
    query = (
        db.collection("alerts")
        .where(filter=FieldFilter("source", "==", source))
        .where(filter=FieldFilter("error_type", "==", error_type))
        .where(filter=FieldFilter("hidden", "==", False))
        .where(filter=FieldFilter("created_at", ">=", cutoff))
    )
    async for _ in query.stream():
        return False
    return True


async def create_alert(
    db: firestore.AsyncClient,
    source: str,
    error_type: str,
    message: str,
    run_id: str,
) -> None:
    if not await _should_create_alert(db, source, error_type):
        return
    await db.collection("alerts").add(
        {
            "source": source,
            "error_type": error_type,
            "message": message,
            "run_id": run_id,
            "created_at": firestore.SERVER_TIMESTAMP,
            "hidden": False,
            "hidden_at": None,
        }
    )


def _parse_timestamp(struct_time: object) -> datetime | None:
    if not struct_time:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(struct_time), tz=timezone.utc)  # type: ignore[arg-type]
    except Exception:
        return None


async def _fetch_source(
    db: firestore.AsyncClient,
    source: str,
    feed_url: str,
    run_id: str,
) -> dict:
    try:
        feed = await asyncio.wait_for(
            asyncio.to_thread(feedparser.parse, feed_url),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        msg = "ReadTimeout: feed did not respond within 30s"
        log_struct("ERROR", f"Timeout fetching {source}", source=source, run_id=run_id, error=msg)
        await create_alert(db, source, "fetch_failed", msg, run_id)
        return {"ok": False, "article_count": 0, "error": msg}
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        log_struct("ERROR", f"Error fetching {source}", source=source, run_id=run_id, error=msg)
        await create_alert(db, source, "fetch_failed", msg, run_id)
        return {"ok": False, "article_count": 0, "error": msg}

    status = getattr(feed, "status", 200)
    if status >= 400:
        msg = f"HTTP {status}"
        log_struct("ERROR", f"HTTP error fetching {source}", source=source, run_id=run_id, error=msg)
        await create_alert(db, source, "fetch_failed", msg, run_id)
        return {"ok": False, "article_count": 0, "error": msg}

    if feed.bozo and not feed.entries:
        msg = f"{type(feed.bozo_exception).__name__}: {feed.bozo_exception}"
        log_struct("ERROR", f"Feed error for {source}", source=source, run_id=run_id, error=msg)
        await create_alert(db, source, "fetch_failed", msg, run_id)
        return {"ok": False, "article_count": 0, "error": msg}

    if not feed.entries:
        await create_alert(db, source, "parse_error", "Feed returned no entries", run_id)
        return {"ok": False, "article_count": 0, "error": "no entries"}

    try:
        # Staleness check — find newest published timestamp across all entries
        newest: datetime | None = None
        for entry in feed.entries:
            pub = _parse_timestamp(entry.get("published_parsed"))
            if pub and (newest is None or pub > newest):
                newest = pub

        if newest is not None:
            age_hours = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
            if age_hours > STALENESS_HOURS:
                await create_alert(
                    db,
                    source,
                    "stale_feed",
                    f"Newest item is {int(age_hours)}h old (threshold: {STALENESS_HOURS}h)",
                    run_id,
                )

        # Ingest new articles
        count = 0
        for entry in feed.entries:
            link: str = entry.get("link", "")
            if not link:
                continue
            canonical = canonicalize_url(link)
            doc_id = article_id(canonical)
            doc_ref = db.collection("articles").document(doc_id)
            if (await doc_ref.get()).exists:
                continue

            await doc_ref.set(
                {
                    "url": canonical,
                    "original_url": link,
                    "title": entry.get("title", "(no title)"),
                    "source": source,
                    "published_at": _parse_timestamp(entry.get("published_parsed")),
                    "fetched_at": firestore.SERVER_TIMESTAMP,
                    "hidden": False,
                    "hidden_at": None,
                }
            )
            count += 1

        log_struct("INFO", f"Fetched {source}", source=source, run_id=run_id, new_articles=count)
        return {"ok": True, "article_count": count, "error": None}

    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        log_struct("ERROR", f"Error processing {source}", source=source, run_id=run_id, error=msg)
        await create_alert(db, source, "fetch_failed", msg, run_id)
        return {"ok": False, "article_count": 0, "error": msg}


async def run_fetch(db: firestore.AsyncClient, triggered_by: str = "manual") -> str:
    run_id = make_run_id()
    run_ref = db.collection("runs").document(run_id)
    await run_ref.set(
        {
            "started_at": firestore.SERVER_TIMESTAMP,
            "finished_at": None,
            "triggered_by": triggered_by,
            "status": "running",
            "per_source": {},
        }
    )
    log_struct("INFO", "Fetch run started", run_id=run_id, triggered_by=triggered_by)

    per_source: dict[str, dict] = {}
    status = "success"

    try:
        tasks = [
            _fetch_source(db, source, url, run_id)
            for source, url in SOURCES.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for source, result in zip(SOURCES.keys(), results):
            if isinstance(result, Exception):
                msg = f"{type(result).__name__}: {result}"
                per_source[source] = {"ok": False, "article_count": 0, "error": msg}
                await create_alert(db, source, "fetch_failed", msg, run_id)
            else:
                per_source[source] = result  # type: ignore[assignment]

        if all(v["ok"] for v in per_source.values()):
            status = "success"
        elif any(v["ok"] for v in per_source.values()):
            status = "partial_failure"
        else:
            status = "failure"

    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        log_struct("ERROR", "Outer fetch error", run_id=run_id, error=msg)
        await create_alert(db, "system", "internal_error", msg, run_id)
        status = "failure"

    await run_ref.update(
        {
            "finished_at": firestore.SERVER_TIMESTAMP,
            "status": status,
            "per_source": per_source,
        }
    )
    log_struct("INFO", f"Fetch run finished: {status}", run_id=run_id, status=status)
    return run_id
