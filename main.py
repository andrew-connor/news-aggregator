import asyncio
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from google.cloud import firestore
from google.cloud import logging as gcp_logging
from google.cloud import secretmanager
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel

from fetcher import SOURCES, log_struct, run_fetch

app = FastAPI()
templates = Jinja2Templates(directory="templates")
templates.env.filters["central"] = (
    lambda dt: dt.astimezone(ZoneInfo("America/Chicago")).strftime("%b %d, %Y %H:%M")
)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "news-aggregator-495416")
db = firestore.AsyncClient(project=PROJECT_ID)

_fetch_token: Optional[str] = None


def get_fetch_token() -> str:
    global _fetch_token
    if _fetch_token is None:
        sm = secretmanager.SecretManagerServiceClient()
        name = f"projects/{PROJECT_ID}/secrets/fetch-token/versions/latest"
        resp = sm.access_secret_version(request={"name": name})
        _fetch_token = resp.payload.data.decode().strip()
    return _fetch_token


class HideRequest(BaseModel):
    type: str
    id: str


class HideSelectedRequest(BaseModel):
    ids: list[str]


class KeepRecentRequest(BaseModel):
    count: int


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, show: Optional[str] = None) -> HTMLResponse:
    show_hidden = show == "hidden"

    alerts_q = db.collection("alerts")
    if not show_hidden:
        alerts_q = alerts_q.where(filter=FieldFilter("hidden", "==", False))
    alerts_q = alerts_q.order_by("created_at", direction=firestore.Query.DESCENDING)
    alerts: list[dict] = []
    async for doc in alerts_q.stream():
        item = doc.to_dict()
        item["id"] = doc.id
        alerts.append(item)

    articles_q = db.collection("articles")
    if not show_hidden:
        articles_q = articles_q.where(filter=FieldFilter("hidden", "==", False))
    articles_q = (
        articles_q.order_by("fetched_at", direction=firestore.Query.DESCENDING).limit(1000)
    )
    articles: list[dict] = []
    async for doc in articles_q.stream():
        item = doc.to_dict()
        item["id"] = doc.id
        articles.append(item)

    last_run: Optional[dict] = None
    async for doc in (
        db.collection("runs")
        .order_by("started_at", direction=firestore.Query.DESCENDING)
        .limit(5)
        .stream()
    ):
        data = doc.to_dict()
        if data.get("finished_at"):
            last_run = data
            break

    try:
        token = get_fetch_token()
    except Exception:
        token = None

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "alerts": alerts,
            "articles": articles,
            "last_run": last_run,
            "show_hidden": show_hidden,
            "fetch_token": token,
        },
    )


@app.post("/hide")
async def hide(req: HideRequest) -> dict:
    if req.type not in ("article", "alert"):
        raise HTTPException(status_code=400, detail="type must be 'article' or 'alert'")
    col = "articles" if req.type == "article" else "alerts"
    update: dict = {"hidden": True, "hidden_at": firestore.SERVER_TIMESTAMP}
    if req.type == "article":
        update["hide_batch_id"] = str(uuid4())
    await db.collection(col).document(req.id).update(update)
    return {"ok": True}


@app.post("/unhide")
async def unhide(req: HideRequest) -> dict:
    if req.type not in ("article", "alert"):
        raise HTTPException(status_code=400, detail="type must be 'article' or 'alert'")
    col = "articles" if req.type == "article" else "alerts"
    update: dict = {"hidden": False, "hidden_at": None}
    if req.type == "article":
        update["hide_batch_id"] = None
    await db.collection(col).document(req.id).update(update)
    return {"ok": True}


@app.post("/hide-all")
async def hide_all() -> dict:
    batch_id = str(uuid4())
    query = db.collection("articles").where(filter=FieldFilter("hidden", "==", False))
    batch = db.batch()
    count = 0
    async for doc in query.stream():
        batch.update(
            doc.reference,
            {
                "hidden": True,
                "hidden_at": firestore.SERVER_TIMESTAMP,
                "hide_batch_id": batch_id,
            },
        )
        count += 1
        if count % 500 == 0:
            await batch.commit()
            batch = db.batch()
    if count % 500 != 0 and count > 0:
        await batch.commit()
    return {"ok": True, "hidden": count}


@app.post("/hide-selected")
async def hide_selected(req: HideSelectedRequest) -> dict:
    if not req.ids:
        return {"ok": True, "hidden": 0}
    batch_id = str(uuid4())
    batch = db.batch()
    count = 0
    for article_id in req.ids:
        batch.update(
            db.collection("articles").document(article_id),
            {
                "hidden": True,
                "hidden_at": firestore.SERVER_TIMESTAMP,
                "hide_batch_id": batch_id,
            },
        )
        count += 1
        if count % 500 == 0:
            await batch.commit()
            batch = db.batch()
    if count % 500 != 0 and count > 0:
        await batch.commit()
    return {"ok": True, "hidden": count}


@app.post("/keep-recent")
async def keep_recent(req: KeepRecentRequest) -> dict:
    keep = max(req.count, 0)
    batch_id = str(uuid4())
    query = (
        db.collection("articles")
        .where(filter=FieldFilter("hidden", "==", False))
        .order_by("fetched_at", direction=firestore.Query.DESCENDING)
    )
    batch = db.batch()
    seen = 0
    hidden = 0
    async for doc in query.stream():
        seen += 1
        if seen <= keep:
            continue
        batch.update(
            doc.reference,
            {
                "hidden": True,
                "hidden_at": firestore.SERVER_TIMESTAMP,
                "hide_batch_id": batch_id,
            },
        )
        hidden += 1
        if hidden % 500 == 0:
            await batch.commit()
            batch = db.batch()
    if hidden % 500 != 0 and hidden > 0:
        await batch.commit()
    return {"ok": True, "hidden": hidden, "kept": min(seen, keep)}


@app.post("/undo-hide")
async def undo_hide() -> dict:
    # Find the most recently hidden article
    most_recent_q = (
        db.collection("articles")
        .where(filter=FieldFilter("hidden", "==", True))
        .order_by("hidden_at", direction=firestore.Query.DESCENDING)
        .limit(1)
    )
    most_recent_doc = None
    async for doc in most_recent_q.stream():
        most_recent_doc = doc
        break

    if most_recent_doc is None:
        return {"ok": True, "restored": 0}

    data = most_recent_doc.to_dict()
    batch_id = data.get("hide_batch_id")

    if not batch_id:
        # Pre-batch-id hide — just unhide this single article
        await most_recent_doc.reference.update(
            {"hidden": False, "hidden_at": None, "hide_batch_id": None}
        )
        return {"ok": True, "restored": 1}

    # Unhide all articles with this batch ID
    batch_q = db.collection("articles").where(
        filter=FieldFilter("hide_batch_id", "==", batch_id)
    )
    batch = db.batch()
    count = 0
    async for doc in batch_q.stream():
        batch.update(
            doc.reference,
            {"hidden": False, "hidden_at": None, "hide_batch_id": None},
        )
        count += 1
        if count % 500 == 0:
            await batch.commit()
            batch = db.batch()
    if count > 0 and count % 500 != 0:
        await batch.commit()
    return {"ok": True, "restored": count}


@app.get("/archive", response_class=HTMLResponse)
async def archive_page(
    request: Request, before: Optional[str] = None
) -> HTMLResponse:
    page_size = 100
    q = db.collection("articles").order_by(
        "fetched_at", direction=firestore.Query.DESCENDING
    )
    if before:
        try:
            before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
            q = q.where(filter=FieldFilter("fetched_at", "<", before_dt))
        except ValueError:
            pass
    q = q.limit(page_size)

    articles: list[dict] = []
    async for doc in q.stream():
        item = doc.to_dict()
        item["id"] = doc.id
        articles.append(item)

    next_before: Optional[str] = None
    if len(articles) == page_size:
        last_fetched = articles[-1].get("fetched_at")
        if last_fetched:
            next_before = last_fetched.isoformat()

    return templates.TemplateResponse(
        request,
        "archive.html",
        {"articles": articles, "next_before": next_before},
    )


@app.get("/fetch")
async def fetch_endpoint(request: Request, token: str = Query(...)) -> dict:
    try:
        expected = get_fetch_token()
    except Exception as exc:
        log_struct("ERROR", "Could not load fetch token", error=str(exc))
        raise HTTPException(status_code=500, detail="Token unavailable")

    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Invalid token")

    triggered_by = (
        "schedule" if request.headers.get("X-CloudScheduler-JobName") else "manual"
    )
    run_id = await run_fetch(db, triggered_by)
    return {"run_id": run_id, "status": "ok"}


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    run: Optional[str] = None,
    source: Optional[str] = None,
    level: Optional[str] = None,
    since: Optional[str] = None,
) -> HTMLResponse:
    filter_parts = ['resource.type="cloud_run_revision"']
    if run:
        filter_parts.append(f'jsonPayload.run_id="{run}"')
    if source:
        filter_parts.append(f'jsonPayload.source="{source}"')
    if level:
        severity_map = {"info": "INFO", "warning": "WARNING", "error": "ERROR"}
        if level in severity_map:
            filter_parts.append(f"severity={severity_map[level]}")

    since_hours = {"hour": 1, "day": 24, "week": 168, "30days": 720}.get(since or "day", 24)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    filter_parts.append(f'timestamp>="{cutoff}"')
    log_filter = "\n".join(filter_parts)

    def _fetch_logs() -> list[dict]:
        client = gcp_logging.Client(project=PROJECT_ID)
        entries = []
        for entry in client.list_entries(filter_=log_filter, max_results=500):
            payload = entry.payload
            entries.append(
                {
                    "time": entry.timestamp,
                    "level": str(entry.severity),
                    "source": payload.get("source", "") if isinstance(payload, dict) else "",
                    "run_id": payload.get("run_id", "") if isinstance(payload, dict) else "",
                    "message": (
                        payload.get("message", str(payload))
                        if isinstance(payload, dict)
                        else str(payload)
                    ),
                }
            )
        return entries

    entries = await asyncio.to_thread(_fetch_logs)

    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "entries": entries,
            "filters": {"run": run, "source": source, "level": level, "since": since},
            "sources": list(SOURCES.keys()),
        },
    )
