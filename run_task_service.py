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

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# -------------------- FastAPI Setup --------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        settings = Settings()
    except ValidationError as e:
        missing = [err["loc"][0] for err in e.errors() if err["type"] == "missing"]
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
    app.state.settings = settings
    app.state.github_runs = {}

    client = httpx.AsyncClient(timeout=10.0)
    app.state.http_client = client
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(
    title="Terraform Run Task Endpoint",
    description="Handles Terraform Cloud run-task webhooks and dispatches Ansible via GitHub Actions.",
    lifespan=lifespan,
)


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client  # pulled from lifespan


templates = Jinja2Templates(directory="templates")

# Mount static directory
app.mount("/static", StaticFiles(directory="static"), name="static")


# -------------------- Models --------------------
class RunTaskPayload(BaseModel):
    payload_version: int
    stage: str
    access_token: SecretStr
    task_result_callback_url: HttpUrl
    run_id: str

    model_config = ConfigDict(
        validate_default=True, populate_by_name=True, extra="ignore"
    )


# -------------------- Routes --------------------
@app.get("/run-task")
async def ping():
    return {"status": "ready"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response("static/favicon.ico", media_type="image/x-icon")


@app.get("/payload")
async def get_payload(request: Request):
    stored = getattr(request.app.state, "last_payload", None)
    return {"payload": stored}


# define health check endpoint
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status(request: Request):
    return {
        "terraform": request.app.state.last_payload,
        "workflow": getattr(request.app.state, "last_workflow_status", None),
    }


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    raw = getattr(request.app.state, "last_payload", None)
    if not raw:
        return templates.TemplateResponse("status.html", {"request": request})

    # grab http client
    client: httpx.AsyncClient = request.app.state.http_client

    # load settings only now, fail-safe if missing
    try:
        settings: Settings = get_settings()
    except ValidationError:
        return templates.TemplateResponse(
            "status.html", {"request": request, "error": "Service not configured"}
        )

    data = json.loads(raw)
    run_id = data.get("run_id")
    created_at = data.get("run_created_at")
    created_by = data.get("run_created_by")

    url = f"{settings.tf_api_url}/api/v2/runs/{run_id}?include=workspace"
    resp = await client.get(
        url, headers={"Authorization": f"Bearer {settings.tf_token.get_secret_value()}"}
    )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return templates.TemplateResponse(
            "status.html",
            {"request": request, "error": f"Failed fetch: {e.response.status_code}"},
        )

    body = resp.json()
    attrs = body["data"]["attributes"]
    included = body.get("included", [])
    workspace_name = next(
        (i["attributes"]["name"] for i in included if i["type"] == "workspaces"), None
    )
    action = attrs.get("run-action") or data.get("stage")
    return templates.TemplateResponse(
        "status.html",
        {
            "request": request,
            "workspace_name": workspace_name,
            "created_at": created_at,
            "created_by": created_by,
            "action": action,
        },
    )


@app.websocket("/ws")
async def ws_status(websocket: WebSocket):
    await websocket.accept()
    last = None
    settings = app.state.settings
    client = app.state.http_client
    while True:
        current = getattr(app.state, "last_payload", None)
        if current and current != last:
            data = json.loads(current)
            run_id = data.get("run_id")
            created_at = data.get("run_created_at")
            created_by = data.get("run_created_by")
            # Terraform API call
            url = f"{settings.tf_api_url}/api/v2/runs/{run_id}?include=workspace"
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {settings.tf_token.get_secret_value()}"
                },
            )
            resp.raise_for_status()
            body = resp.json()
            attrs = body["data"]["attributes"]
            included = body.get("included", [])
            workspace = next(
                (
                    i["attributes"]["name"]
                    for i in included
                    if i["type"] == "workspaces"
                ),
                None,
            )
            action = attrs.get("run-action") or data.get("stage")
            duration = attrs.get("apply-duration-seconds")
            gr = app.state.github_runs.get(str(run_id), {})
            total = gr.get("total", 0)
            completed = gr.get("completed", 0)
            progress = int(completed / total * 100) if total else None
            payload = {
                "workspace_name": workspace,
                "created_at": created_at,
                "created_by": created_by,
                "action": action,
                "duration": duration,
                "progress": progress,
            }
            await websocket.send_json(payload)
            last = current
        await asyncio.sleep(1)
    await websocket.close()


@app.post("/github-webhook")
async def github_webhook(request: Request, settings: Settings = Depends(get_settings)):
    signature = request.headers.get("X-Hub-Signature-256")
    body = await request.body()
    if settings.github_webhook_secret:
        mac = hmac.new(
            settings.github_webhook_secret.get_secret_value().encode(),
            body,
            hashlib.sha256,
        )
        if not hmac.compare_digest("sha256=" + mac.hexdigest(), signature or ""):
            raise HTTPException(401)
    event = request.headers.get("X-GitHub-Event")
    data = json.loads(body)
    if event == "workflow_job":
        run_id = str(data["workflow_job"]["run_id"])
        job_id = data["workflow_job"]["id"]
        status = data["workflow_job"]["status"]
        runs = request.app.state.github_runs
        info = runs.setdefault(run_id, {"total": None, "completed": 0, "jobs": {}})
        info["jobs"][job_id] = status
        if status == "completed":
            info["completed"] = sum(
                1 for s in info["jobs"].values() if s == "completed"
            )
        if info["total"] is None:
            owner, repo_name = settings.github_repository.split("/")
            url = f"https://api.github.com/repos/{owner}/{repo_name}/actions/runs/{run_id}/jobs"
            resp = await request.app.state.http_client.get(
                url,
                headers={
                    "Authorization": f"Bearer {settings.gh_token.get_secret_value()}"
                },
            )
            resp.raise_for_status()
            info["total"] = resp.json()["total_count"]
    return {}


# -------------------- Main Endpoint --------------------
@app.post("/run-task")
async def run_task(
    request: Request,
    background_tasks: BackgroundTasks,
    client: httpx.AsyncClient = Depends(get_http_client),
    settings: Settings = Depends(get_settings),
):
    # Read raw body for signature
    body_bytes = await request.body()
    request.app.state.last_payload = body_bytes
    sig_header = request.headers.get("X-TFC-Task-Signature")
    # Verify HMAC signature if configured
    if settings.hmac_key:
        if not sig_header:
            logger.warning("Missing X-TFC-Task-Signature header")
            raise HTTPException(status_code=400, detail="Missing signature header")
        expected = hmac.new(
            settings.hmac_key.get_secret_value().encode(), body_bytes, hashlib.sha512
        ).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            logger.warning("Invalid signature")
            raise HTTPException(status_code=401, detail="Invalid signature")
    # Parse and validate payload
    try:
        data = json.loads(body_bytes)
        logger.debug("Parsed callback JSON: %s", data)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON body", exc_info=e)
        raise HTTPException(status_code=400, detail="Invalid JSON")
    try:
        payload = RunTaskPayload.parse_obj(data)
    except ValidationError as e:
        errors = e.errors()
        logger.error("Payload validation errors: %s", errors)
        raise HTTPException(status_code=422, detail=errors)
    # Enqueue background processing
    background_tasks.add_task(handle_task_result, payload, client, settings)
    return JSONResponse(status_code=200, content={})


@app.post("/workflow-callback")
async def workflow_callback(request: Request):
    event = await request.json()
    # extract repository, workflow_run details
    request.app.state.last_workflow_status = {
        "name": event["workflow_run"]["name"],
        "status": event["workflow_run"]["status"],
        "conclusion": event["workflow_run"].get("conclusion"),
    }
    return {}


# -------------------- Helper Functions --------------------
def determine_status_and_message(stage: str, is_destroy: bool) -> Tuple[str, str]:
    if is_destroy:
        return "skipped", "Destroy run detected, skipping run task."
    if stage == "post_apply":
        return "passed", "Task passed at post_apply."
    return "skipped", f"Unhandled run task stage: {stage}"


async def post_task_result(
    callback_url: HttpUrl,
    access_token: SecretStr,
    status: str,
    message: str,
    client: httpx.AsyncClient,
) -> None:
    payload = {
        "data": {
            "type": "task-results",
            "attributes": {"status": status, "message": message},
        }
    }
    url_str = str(callback_url)
    headers = {
        "Authorization": f"Bearer {access_token.get_secret_value()}",
        "Content-Type": "application/vnd.api+json",
    }
    resp = await client.patch(url_str, json=payload, headers=headers)
    if resp.status_code != 200:
        logger.error(f"Callback PATCH failed: {resp.status_code} - {resp.text}")


async def dispatch_workflow_if_applicable(
    payload: RunTaskPayload,
    client: httpx.AsyncClient,
    settings: Settings,
) -> None:
    if payload.stage != "post_apply":
        return
    # Check actual run type from Terraform API to skip destroys
    run_id = payload.run_id
    if run_id:
        url2 = f"{settings.tf_api_url}/api/v2/runs/{run_id}"
        logger.debug(f"Fetching run details for destroy check: {url2}")
        resp2 = await client.get(
            url2,
            headers={"Authorization": f"Bearer {settings.tf_token.get_secret_value()}"},
        )
        if resp2.status_code == 200:
            attrs = resp2.json()["data"]["attributes"]

            is_destroy_api = attrs.get("is-destroy", False)
            logger.debug(f"Run {run_id} is-destroy (from API): {is_destroy_api}")
            if is_destroy_api:
                logger.info("Detected destroy run, skipping GitHub Actions dispatch.")
                return
        else:
            logger.warning(
                f"Failed to fetch run attributes for {run_id}: {resp2.status_code}"
            )
    else:
        logger.warning(
            "No run_id provided, cannot check destroy status. Proceeding with dispatch."
        )
    # Dispatch now that it's confirmed as apply
    logger.info("Dispatching GitHub Actions workflow for Ansible")
    gh_token = settings.gh_token
    if not gh_token:
        logger.warning("GH_TOKEN not set, skipping dispatch.")
        return
    repo = settings.github_repository
    if not repo:
        logger.warning("GITHUB_REPOSITORY not set, skipping dispatch.")
        return
    try:
        owner, repo_name = repo.split("/")
    except ValueError:
        logger.error(f"Invalid GITHUB_REPOSITORY format: {repo}")
        raise HTTPException(status_code=400, detail="Invalid GITHUB_REPOSITORY format")
    dispatch_url = (
        f"https://api.github.com/repos/{owner}/{repo_name}/actions/workflows/"
        f"{settings.github_workflow_file}/dispatches"
    )
    body = {"ref": settings.github_ref}
    headers = {
        "Authorization": f"Bearer {gh_token.get_secret_value()}",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = await client.post(dispatch_url, json=body, headers=headers)  # type: ignore
    logger.debug(
        f"GitHub Actions dispatch status: {resp.status_code}, body: {resp.text}"
    )
    if resp.status_code >= 300:
        logger.error(f"Dispatch failed: {resp.status_code} - {resp.text}")


async def handle_task_result(
    payload: RunTaskPayload,
    client: httpx.AsyncClient,
    settings: Settings,
) -> None:
    try:
        status, message = determine_status_and_message(payload.stage, False)
        await post_task_result(
            payload.task_result_callback_url,
            payload.access_token,
            status,
            message,
            client,
        )
        await dispatch_workflow_if_applicable(payload, client, settings)
    except Exception:
        logger.exception("Error handling task result")


if __name__ == "__main__":
    import uvicorn, os

    port = int(os.getenv("PORT", 3000))
    uvicorn.run("run_task_service:app", host="0.0.0.0", port=port)
