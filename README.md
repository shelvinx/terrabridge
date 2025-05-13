# Terraform Run Task Endpoint

Python code is primarily written by AI, overseeing the architecture, design and implementation.
Handle Terraform Cloud run-task webhooks and dispatch Ansible workflows via GitHub Actions to demonstrate the concept.

## Tech Stack
- FastAPI
- Redis
- GitHub Actions
- Terraform Cloud

The WebSocket layer was refactored to open a single Redis Pub/Sub connection at startup, with a background task reading every message and broadcasting it to an in-process WeakSet of WebSocket objects. Client handlers now only register and unregister their sockets, never creating new Redis connections, which prevents connection leaks and keeps Redis client count fixed below the free-plan cap. The trade-off is that fan-out state is held in memory—messages are lost on process restart and each worker has its own subscriber and client set—so this won't scale but that is not required.

## Features
- Handle Terraform Cloud run-task webhooks
- Verify webhook signatures using HMAC
- Post task results to Terraform Cloud
- Dispatch GitHub Actions workflows for Ansible
- Skip destroy runs automatically


## Configuration
Configure environment variables before running:
```bash
export TF_TOKEN=<terraform-cloud-token>
export GH_TOKEN=<github-token>
export GITHUB_REPOSITORY=<owner/repo>
export HMAC_KEY=<hmac-secret>
export PORT=3000
```

## Startup Command
```bash
uvicorn run_task_service:app --host 0.0.0.0 --port 3000
```

## Deployment
CI/CD set up on Azure Web App with auto-deploy from `main` branch.