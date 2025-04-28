# Terraform Run Task Endpoint

Handle Terraform Cloud run-task webhooks and dispatch Ansible workflows via GitHub Actions.

## Features
- Handle Terraform Cloud run-task webhooks
- Verify webhook signatures using HMAC
- Post task results to Terraform Cloud
- Dispatch GitHub Actions workflows for Ansible
- Skip destroy runs automatically
- Monitoring with New Relic

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
NEW_RELIC_CONFIG_FILE=newrelic.ini newrelic-admin run-program uvicorn run_task_service:app --host 0.0.0.0 --port 3000
```

## Deployment
CI/CD set up on Azure Web App with auto-deploy from `main` branch.