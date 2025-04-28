# -------------------- Imports --------------------

import logging
import os
import asyncio
from typing import Optional, Tuple
import httpx
from fastapi import BackgroundTasks, FastAPI, Request, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseSettings, Field, HttpUrl, SecretStr, ValidationError
from pydantic import SettingsConfigDict
import hmac
import hashlib
import json

# -------------------- Configuration --------------------
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=False
    )

    tf_api_url: HttpUrl = Field(default="https://app.terraform.io", env="TF_API_URL")
    tf_token: SecretStr = Field(..., env="TF_TOKEN")
    gh_token: SecretStr = Field(..., env="GH_TOKEN")
    github_repository: str = Field(..., env="GITHUB_REPOSITORY")
    github_workflow_file: str = Field(default="ansible-runner.yml", env="GITHUB_WORKFLOW_FILE")
    github_ref: str = Field(default="main", env="GITHUB_REF")
    hmac_key: Optional[SecretStr] = Field(None, env="HMAC_KEY")

settings = Settings()

# Validate repository format at startup
if "/" not in settings.github_repository:
    raise RuntimeError(f"Invalid GITHUB_REPOSITORY format: {settings.github_repository}")

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------- FastAPI Setup --------------------
app = FastAPI(
    title="Terraform Run Task Endpoint",
    description="Handles Terraform Cloud run-task webhooks and dispatches Ansible via GitHub Actions."
)

client: httpx.AsyncClient

@app.on_event("startup")
async def startup_event():
    global client
    client = httpx.AsyncClient(timeout=10.0)

@app.on_event("shutdown")
async def shutdown_event():
    await client.aclose()

# -------------------- Models --------------------
class RunTaskPayload(BaseSettings):
    stage: str
    is_destroy: bool = Field(False, alias="is-destroy")
    task_result_callback_url: HttpUrl
    access_token: SecretStr
    run_id: Optional[str] = Field(None, alias="run_id")

    model_config = SettingsConfigDict(
        validate_default=True,
        extra="forbid",
        populate_by_name=True
    )

# -------------------- Health Endpoints --------------------
@app.get("/")
async def health() -> dict:
    return {"status": "ok"}

@app.get("/run-task")
async def ping() -> dict:
    return {"status": "ready"}

# -------------------- Main Endpoint --------------------
@app.post("/run-task")
async def run_task(request: Request, background_tasks: BackgroundTasks) -> Response:
    body_bytes = await request.body()
    sig_header = request.headers.get("X-TFC-Task-Signature")

    if settings.hmac_key:
        if not sig_header:
            logger.warning("Missing X-TFC-Task-Signature header")
            raise HTTPException(status_code=400, detail="Missing signature header")
        expected = hmac.new(
            settings.hmac_key.get_secret_value().encode(),
            body_bytes,
            hashlib.sha512
        ).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            logger.warning("Invalid signature")
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        data = json.loads(body_bytes)
        payload = RunTaskPayload(**data)
    except (ValueError, ValidationError) as e:
        logger.error("Payload validation error", exc_info=e)
        raise HTTPException(status_code=422, detail="Invalid payload")

    background_tasks.add_task(handle_task_result, payload)
    return Response(status_code=204)

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
) -> None:
    payload = {"data": {"type": "task-results", "attributes": {"status": status, "message": message}}}
    url_str = str(callback_url)
    headers = {
        "Authorization": f"Bearer {access_token.get_secret_value()}",
        "Content-Type": "application/vnd.api+json",
    }
    for attempt, delay in enumerate((1, 2, 4), start=1):
        try:
            resp = await client.patch(url_str, json=payload, headers=headers)
            if resp.status_code == 200:
                return
            logger.error(f"Attempt {attempt} failed: {resp.status_code} - {resp.text}")
        except httpx.HTTPError as e:
            logger.error(f"HTTP error on attempt {attempt}: {e}")
        await asyncio.sleep(delay)
    logger.error("Exceeded retries for posting task result")

async def dispatch_workflow_if_applicable(payload: RunTaskPayload) -> None:
    if payload.stage != "post_apply":
        return
    # Check run details
    if payload.run_id:
        tf_url = f"{settings.tf_api_url}/api/v2/runs/{payload.run_id}"
        for delay in (1, 2, 4):
            try:
                resp = await client.get(
                    tf_url,
                    headers={"Authorization": f"Bearer {settings.tf_token.get_secret_value()}"}
                )
                if resp.status_code == 200:
                    attrs = resp.json().get("data", {}).get("attributes", {})
                    if attrs.get("is-destroy", False):
                        logger.info("Detected destroy run, skipping GitHub Actions dispatch.")
                        return
                    break
                logger.warning(f"Fetch attempt failed: {resp.status_code}")
            except httpx.HTTPError as e:
                logger.warning(f"HTTP error fetching run details: {e}")
            await asyncio.sleep(delay)
    else:
        logger.warning("No run_id provided, proceeding with dispatch.")

    # Dispatch GitHub Actions
    dispatch_url = (
        f"https://api.github.com/repos/{settings.github_repository}/actions/workflows/{settings.github_workflow_file}/dispatches"
    ).replace("\n", "")
    body = {"ref": settings.github_ref}
    headers = {
        "Authorization": f"Bearer {settings.gh_token.get_secret_value()}",
        "Accept": "application/vnd.github.v3+json",
    }
    for attempt, delay in enumerate((1, 2, 4), start=1):
        try:
            resp = await client.post(dispatch_url, json=body, headers=headers)
            if resp.status_code < 300:
                logger.info("GitHub Actions dispatched successfully.")
                return
            logger.error(f"Dispatch attempt {attempt} failed: {resp.status_code} - {resp.text}")
        except httpx.HTTPError as e:
            logger.error(f"HTTP error on dispatch attempt {attempt}: {e}")
        await asyncio.sleep(delay)
    logger.error("Exceeded retries for GitHub Actions dispatch")

async def handle_task_result(payload: RunTaskPayload) -> None:
    status, message = determine_status_and_message(payload.stage, payload.is_destroy)
    await post_task_result(
        payload.task_result_callback_url,
        payload.access_token,
        status,
        message
    )
    await dispatch_workflow_if_applicable(payload)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "3000"))
    import uvicorn
    uvicorn.run("run_task_service:app", host="0.0.0.0", port=port)
