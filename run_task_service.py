# file: run_task_service.py

import os
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx

TF_API_URL = "https://app.terraform.io"
TF_TOKEN   = os.getenv("TF_TOKEN", "")

app = FastAPI(
    title="Terraform Run Task Endpoint",
    description="Logs apply vs destroy, with verification support."
)

# Health endpoint for manual checks (optional)
@app.get("/")
async def health():
    return {"status": "ok"}

# Allow GET on /run-task if Terraform UI ever does a GET
@app.get("/run-task")
async def ping():
    return {"status": "ready"}

@app.post("/run-task")
async def run_task(request: Request):
    # Parse whatever JSON comes in (verification or real payload)
    try:
        body = await request.json()
    except:
        body = {}

    print(f"[DEBUG] Full incoming payload: {body}")
    stage = body.get("stage")
    is_destroy = body.get("is-destroy", False)
    callback_url = body.get("task_result_callback_url")
    access_token = body.get("access_token")

    # Respond 200 OK immediately
    asyncio.create_task(handle_task_result(body, stage, is_destroy, callback_url, access_token))
    return JSONResponse(content={}, status_code=200)

async def handle_task_result(body, stage, is_destroy, callback_url, access_token):
    if not callback_url or not access_token:
        print("[ERROR] Missing callback_url or access_token in payload.")
        return

    # Determine the status and message
    if is_destroy:
        status = "skipped"
        message = "Destroy run detected, skipping run task."
    elif stage == "post_apply":
        status = "passed"
        message = "Task passed at post_apply."
    else:
        status = "skipped"
        message = f"Unhandled run task stage: {stage}"

    # Construct the required JSON:API payload
    result_payload = {
        "data": {
            "type": "task-results",
            "attributes": {
                "status": status,
                "message": message # Include the message for clarity
            }
        }
    }

    masked_token = access_token[:4] + "..." + access_token[-4:] if access_token else None
    print(f"[DEBUG] Posting result to callback: {result_payload}")
    print(f"[DEBUG] Callback URL: {callback_url}")
    print(f"[DEBUG] Using access_token: {masked_token}")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/vnd.api+json"
    }
    async with httpx.AsyncClient() as client:
        resp = await client.patch(callback_url, json=result_payload, headers=headers)
        print(f"[DEBUG] Callback PATCH status: {resp.status_code}, body: {resp.text}")
        if resp.status_code != 200:
            print(f"[ERROR] Callback PATCH failed: {resp.status_code} - {resp.text}")

    # Dispatch GitHub Actions workflow on post_apply stage (apply only)
    if stage == "post_apply" and not is_destroy:
        run_id = body.get("run_id")
        real_is_destroy = False
        if run_id and TF_TOKEN:
            url2 = f"{TF_API_URL}/api/v2/runs/{run_id}"
            print(f"[DEBUG] Fetching run details for destroy check: {url2}")
            async with httpx.AsyncClient() as client3:
                resp2 = await client3.get(url2, headers={"Authorization": f"Bearer {TF_TOKEN}"})
            if resp2.status_code == 200:
                attrs2 = resp2.json()["data"]["attributes"]
                real_is_destroy = attrs2.get("is-destroy", False)
                print(f"[DEBUG] Run {run_id} is_destroy (from API): {real_is_destroy}")
            else:
                print(f"[WARNING] Failed to fetch run attributes for {run_id}: {resp2.status_code}")
        if real_is_destroy:
            print("[INFO] Detected destroy run via API, skipping GitHub Actions dispatch.")
        else:
            print("[INFO] Dispatching GitHub Actions workflow for Ansible")
            gh_token = os.getenv("GH_TOKEN", "")
            if not gh_token:
                print("[WARNING] GH_TOKEN not set, skipping GitHub Actions dispatch.")
            else:
                repo = os.getenv("GITHUB_REPOSITORY", "shelvinx/ansible-playbooks")
                if not repo:
                    print("[WARNING] GITHUB_REPOSITORY not set, skipping dispatch.")
                else:
                    owner, repo_name = repo.split("/")
                    workflow_file = os.getenv("GITHUB_WORKFLOW_FILE", "ansible-runner.yml")
                    dispatch_url = f"https://api.github.com/repos/{owner}/{repo_name}/actions/workflows/{workflow_file}/dispatches"
                    dispatch_payload = {"ref": os.getenv("GITHUB_REF", "main")}  
                    headers2 = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github.v3+json"}
                    async with httpx.AsyncClient() as client2:
                        dr = await client2.post(dispatch_url, json=dispatch_payload, headers=headers2)
                        print(f"[DEBUG] GitHub Actions dispatch status: {dr.status_code}, body: {dr.text}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("run_task_service:app", host="0.0.0.0", port=3000, reload=True)
