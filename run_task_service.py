# file: run_task_service.py

import logging
import os
from typing import Optional, Tuple
import httpx
from fastapi import (
    BackgroundTasks,
    FastAPI,
    Request,
    HTTPException,
    Depends,
    Response,
    WebSocket,
)
from fastapi.responses import (
    JSONResponse,
    FileResponse,
    HTMLResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field, HttpUrl, ValidationError, SecretStr, ConfigDict
import json
import hmac
import hashlib
from settings import get_settings, Settings
from contextlib import asynccontextmanager
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis
import asyncio

# ────────────────────────────────────────── logging ─────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ────────────────────────────────────────── models ──────────────────────────────────────────
class RunTaskPayload(BaseModel):
    payload_version: int
    stage: str
    access_token: SecretStr
    task_result_callback_url: HttpUrl
    run_id: str


# ────────────────────────────────────────── helpers ─────────────────────────────────────────
async def is_destroy_run(
    run_id: str, client: httpx.AsyncClient, settings: Settings
) -> bool:
    url = f"{settings.tf_api_url}/api/v2/runs/{run_id}"
    resp = await client.get(
        url, headers={"Authorization": f"Bearer {settings.tf_token.get_secret_value()}"}
    )
    resp.raise_for_status()
    return resp.json()["data"]["attributes"].get("is-destroy", False)


# Terraform Post-Apply Run Message
def determine_status_and_message(stage: str) -> Tuple[str, str]:
    if stage == "post_apply":
        return "passed", "Task passed at post_apply."
    return "passed", f"Task passed at stage: {stage}"


async def post_task_result(
    *,
    callback_url: HttpUrl,
    access_token: SecretStr,
    status: str,
    message: str,
    client: httpx.AsyncClient,
) -> None:
    body = {
        "data": {
            "type": "task-results",
            "attributes": {"status": status, "message": message},
        }
    }
    headers = {
        "Authorization": f"Bearer {access_token.get_secret_value()}",
        "Content-Type": "application/vnd.api+json",
    }
    resp = await client.patch(str(callback_url), json=body, headers=headers)
    resp.raise_for_status()


async def dispatch_workflow_if_apply(
    payload: RunTaskPayload,
    client: httpx.AsyncClient,
    settings: Settings,
    destroy: bool,
) -> None:
    if destroy or payload.stage != "post_apply":
        return
    owner, repo = settings.github_repository.split("/", 1)
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/"
        f"{settings.github_workflow_file}/dispatches"
    )
    body = {"ref": settings.github_ref}
    headers = {
        "Authorization": f"Bearer {settings.gh_token.get_secret_value()}",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = await client.post(url, json=body, headers=headers)
    resp.raise_for_status()


async def handle_task_result(
    payload: RunTaskPayload, client: httpx.AsyncClient, settings: Settings
) -> None:
    destroy = await is_destroy_run(payload.run_id, client, settings)
    status, message = determine_status_and_message(payload.stage)
    await post_task_result(
        callback_url=payload.task_result_callback_url,
        access_token=payload.access_token,
        status=status,
        message=message,
        client=client,
    )
    await dispatch_workflow_if_apply(payload, client, settings, destroy)


# ──────────────────────────────────────── app factory ───────────────────────────────────────
templates = Jinja2Templates(directory="templates")


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    app.state.settings = settings
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.http_client = httpx.AsyncClient(timeout=10.0)
    yield
    await app.state.http_client.aclose()
    await app.state.redis.close()


app = FastAPI(
    title="Terraform Run Task Endpoint",
    description="Handles Terraform Cloud run-task webhooks and GitHub Actions callbacks.",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ────────────────────────────────────────── simple routes ───────────────────────────────────
@app.get("/run-task")
async def ready():
    return {"status": "ready"}


@app.get("/health")
async def health():
    return {"status": "ok"}


# ────────────────────────────────────── terraform callback ──────────────────────────────────
@app.post("/run-task")
async def run_task(
    request: Request,
    background_tasks: BackgroundTasks,
    client: httpx.AsyncClient = Depends(get_http_client),
    settings: Settings = Depends(get_settings),
):
    body = await request.body()
    redis: Redis = request.app.state.redis
    await redis.set("last_payload", body.decode())

    # HMAC verify (optional)
    sig = request.headers.get("X-TFC-Task-Signature")
    if settings.hmac_key:
        if not sig:
            raise HTTPException(status_code=400, detail="Missing signature header")
        expected = hmac.new(
            settings.hmac_key.get_secret_value().encode(), body, hashlib.sha512
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(status_code=401, detail="Invalid signature")

    payload = RunTaskPayload.parse_raw(body)
    background_tasks.add_task(handle_task_result, payload, client, settings)
    return JSONResponse({"enqueued": True})


# ───────────────────────────────────── github webhook ───────────────────────────────────────
@app.post("/github-webhook")
async def github_webhook(request: Request, settings: Settings = Depends(get_settings)):
    body = await request.body()
    if settings.github_webhook_secret:
        mac = hmac.new(
            settings.github_webhook_secret.get_secret_value().encode(),
            body,
            hashlib.sha256,
        )
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not hmac.compare_digest(f"sha256={mac.hexdigest()}", sig):
            raise HTTPException(status_code=401, detail="Invalid signature")

    if request.headers.get("X-GitHub-Event") == "workflow_job":
        job = json.loads(body)["workflow_job"]
        info = {
            "workflow_name": job["workflow_name"],
            "started_at": job["started_at"],
            "completed_at": job["completed_at"],
        }
        await request.app.state.redis.set("latest_job", json.dumps(info))
    return {}


# ───────────────────────────────────────── ui + helpers ─────────────────────────────────────
@app.get("/")
async def ui(request: Request):
    redis: Redis = request.app.state.redis
    raw = await redis.get("last_payload")
    if not raw:
        return templates.TemplateResponse("status.html", {"request": request})

    settings = request.app.state.settings
    data = json.loads(raw)
    run_id = data["run_id"]

    resp = await request.app.state.http_client.get(
        f"{settings.tf_api_url}/api/v2/runs/{run_id}?include=workspace",
        headers={"Authorization": f"Bearer {settings.tf_token.get_secret_value()}"},
    )
    resp.raise_for_status()
    body = resp.json()
    attrs = body["data"]["attributes"]
    ws_name = next(
        (
            i["attributes"]["name"]
            for i in body.get("included", [])
            if i["type"] == "workspaces"
        ),
        None,
    )
    return templates.TemplateResponse(
        "status.html",
        {
            "request": request,
            "workspace_name": ws_name,
            "action": attrs.get("run-action") or data.get("stage"),
            "created_by": data.get("run_created_by"),
            "created_at": data.get("run_created_at"),
            "is_destroy": data.get("is_destroy"),
        },
    )


@app.get("/status")
async def status(request: Request):
    r = request.app.state.redis
    return {
        "terraform": await r.get("last_payload"),
        "workflow": await r.get("latest_job"),
    }


# ───────────────────────────────────── websocket push ───────────────────────────────────────
@app.websocket("/ws")
async def ws_status(ws: WebSocket):
    await ws.accept()
    r: Redis = app.state.redis
    http_client = app.state.http_client
    settings: Settings = app.state.settings

    last_tf = await r.get("last_payload")
    last_gh = await r.get("latest_job")
    if last_tf:
        await _send_tf_update(ws, last_tf, http_client, settings)
    if last_gh:
        await ws.send_json(json.loads(last_gh))

    while True:
        tf = await r.get("last_payload")
        gh = await r.get("latest_job")

        if tf and tf != last_tf:
            await _send_tf_update(ws, tf, http_client, settings)
            last_tf = tf
        if gh and gh != last_gh:
            await ws.send_json(json.loads(gh))
            last_gh = gh

        await asyncio.sleep(0.25)


async def _send_tf_update(
    ws: WebSocket, raw: str, client: httpx.AsyncClient, settings: Settings
) -> None:
    data = json.loads(raw)
    run_id = data["run_id"]
    resp = await client.get(
        f"{settings.tf_api_url}/api/v2/runs/{run_id}?include=workspace",
        headers={"Authorization": f"Bearer {settings.tf_token.get_secret_value()}"},
    )
    resp.raise_for_status()
    body = resp.json()
    attrs = body["data"]["attributes"]
    ws_name = next(
        (
            i["attributes"]["name"]
            for i in body.get("included", [])
            if i["type"] == "workspaces"
        ),
        None,
    )
    await ws.send_json(
        {
            "workspace_name": ws_name,
            "action": attrs.get("run-action") or data.get("stage"),
            "duration": attrs.get("apply-duration-seconds"),
            "is_destroy": attrs.get("is-destroy"),
        }
    )


# ──────────────────────────────────────── main (local) ──────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    import uvicorn

    uvicorn.run("run_task_service:app", host="0.0.0.0", port=port)
