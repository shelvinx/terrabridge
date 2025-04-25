# file: run_task_service.py

import logging
import os
from typing import Optional, Tuple
import httpx
from fastapi import BackgroundTasks, FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl, ValidationError
from pydantic_settings import BaseSettings
from contextlib import asynccontextmanager
import hmac
import hashlib
import json

# -------------------- Configuration --------------------
class Settings(BaseSettings):
    tf_api_url: str = Field("https://app.terraform.io", env="TF_API_URL")
    tf_token: str = Field(..., env="TF_TOKEN")
    gh_token: str = Field(..., env="GH_TOKEN")
    github_repository: str = Field(..., env="GITHUB_REPOSITORY")
    github_workflow_file: str = Field("ansible-runner.yml", env="GITHUB_WORKFLOW_FILE")
    github_ref: str = Field("main", env="GITHUB_REF")
    hmac_key: Optional[str] = Field(None, env="HMAC_KEY")

    class Config:
        case_sensitive = False

settings = Settings()

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------- FastAPI Setup --------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=10.0)
    yield
    await client.aclose()

app = FastAPI(
    title="Terraform Run Task Endpoint",
    description="Handles Terraform Cloud run-task webhooks and dispatches Ansible via GitHub Actions.",
    lifespan=lifespan,
)
client: Optional[httpx.AsyncClient] = None

# -------------------- Models --------------------
class RunTaskPayload(BaseModel):
    stage: str
    is_destroy: bool = Field(False, alias="is-destroy")
    task_result_callback_url: HttpUrl
    access_token: str
    run_id: Optional[str] = Field(None, alias="run_id")

    class Config:
        validate_by_name = True
        extra = "ignore"

# -------------------- Health Endpoints --------------------
@app.get("/")
async def health():
    return {"status": "ok"}

@app.get("/run-task")
async def ping():
    return {"status": "ready"}

# -------------------- Main Endpoint --------------------
@app.post("/run-task")
async def run_task(request: Request, background_tasks: BackgroundTasks):
    # Read raw body for signature
    body_bytes = await request.body()
    sig_header = request.headers.get("X-TFC-Task-Signature")
    # Verify HMAC signature if configured
    if settings.hmac_key:
        if not sig_header:
            logger.warning("Missing X-TFC-Task-Signature header")
            raise HTTPException(status_code=400, detail="Missing signature header")
        expected = hmac.new(settings.hmac_key.encode(), body_bytes, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            logger.warning("Invalid signature")
            raise HTTPException(status_code=401, detail="Invalid signature")
    # Parse and validate payload
    try:
        data = json.loads(body_bytes)
        payload = RunTaskPayload.parse_obj(data)
    except (ValueError, ValidationError) as e:
        logger.error("Payload validation error", exc_info=e)
        raise HTTPException(status_code=422, detail="Invalid payload")
    # Enqueue background processing
    background_tasks.add_task(handle_task_result, payload)
    return JSONResponse(content={}, status_code=200)

# -------------------- Helper Functions --------------------
def determine_status_and_message(stage: str, is_destroy: bool) -> Tuple[str, str]:
    if is_destroy:
        return "skipped", "Destroy run detected, skipping run task."
    if stage == "post_apply":
        return "passed", "Task passed at post_apply."
    return "skipped", f"Unhandled run task stage: {stage}"

async def post_task_result(
    callback_url: str,
    access_token: str,
    status: str,
    message: str,
) -> None:
    payload = {"data": {"type": "task-results", "attributes": {"status": status, "message": message}}}
    # Ensure URL is str
    url_str = str(callback_url)
    masked = access_token[:4] + "..." + access_token[-4:]
    logger.debug(f"Posting result to callback: {payload}")
    logger.debug(f"Callback URL: {url_str}")
    logger.debug(f"Using access_token: {masked}")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/vnd.api+json",
    }
    resp = await client.patch(url_str, json=payload, headers=headers)  # type: ignore
    logger.debug(f"Callback PATCH status: {resp.status_code}, body: {resp.text}")
    if resp.status_code != 200:
        logger.error(f"Callback PATCH failed: {resp.status_code} - {resp.text}")

async def dispatch_workflow_if_applicable(payload: RunTaskPayload) -> None:
    if payload.stage != "post_apply":
        return
    # Check actual run type from Terraform API to skip destroys
    run_id = payload.run_id
    if run_id:
        url2 = f"{settings.tf_api_url}/api/v2/runs/{run_id}"
        logger.debug(f"Fetching run details for destroy check: {url2}")
        resp2 = await client.get(url2, headers={"Authorization": f"Bearer {settings.tf_token}"})
        if resp2.status_code == 200:
            attrs = resp2.json()["data"]["attributes"]
            is_destroy_api = attrs.get("is-destroy", False)
            logger.debug(f"Run {run_id} is-destroy (from API): {is_destroy_api}")
            if is_destroy_api:
                logger.info("Detected destroy run, skipping GitHub Actions dispatch.")
                return
        else:
            logger.warning(f"Failed to fetch run attributes for {run_id}: {resp2.status_code}")
    else:
        logger.warning("No run_id provided, cannot check destroy status. Proceeding with dispatch.")
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
        return
    dispatch_url = (
        f"https://api.github.com/repos/{owner}/{repo_name}/actions/workflows/"
        f"{settings.github_workflow_file}/dispatches"
    )
    body = {"ref": settings.github_ref}
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = await client.post(dispatch_url, json=body, headers=headers)  # type: ignore
    logger.debug(f"GitHub Actions dispatch status: {resp.status_code}, body: {resp.text}")
    if resp.status_code >= 300:
        logger.error(f"Dispatch failed: {resp.status_code} - {resp.text}")

async def handle_task_result(payload: RunTaskPayload) -> None:
    try:
        status, message = determine_status_and_message(payload.stage, payload.is_destroy)
        await post_task_result(
            payload.task_result_callback_url, payload.access_token, status, message
        )
        await dispatch_workflow_if_applicable(payload)
    except Exception:
        logger.exception("Error handling task result")

if __name__ == "__main__":
    import uvicorn, os
    port = int(os.getenv("PORT", 3000))
    uvicorn.run("run_task_service:app", host="0.0.0.0", port=port)
