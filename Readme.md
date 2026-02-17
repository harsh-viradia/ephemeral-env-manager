FastAPI service for managing slot-based ephemeral Kubernetes namespaces (eph1–eph25).  
Used for QA and feature environments with GitLab pipeline integration.

---

## Features

- Lists used and available namespaces
- Bearer token authentication via Kubernetes Secrets
- Namespace metadata using labels and annotations
- GitLab pipeline trigger support
- Dockerized (non-root)
- Works in-cluster and locally
- Health endpoint

---

## Environment Variables

Core:

EPH_PREFIX=eph  
EPH_SLOTS=25  
OWNER_SECRET_NAME=eph-owner-tokens  
OWNER_SECRET_NAMESPACE=ephemeral-python-app  
LOG_LEVEL=INFO  

GitLab:

GITLAB_PROJECT_ID (required)  
GITLAB_TRIGGER_TOKEN (required)  
GITLAB_API_URL=https://gitlab.com/api/v4  
GITLAB_REF_BRANCH=terraform-iac  

---

## Authentication

Bearer tokens are stored in Kubernetes Secrets.

Example:

apiVersion: v1  
kind: Secret  
metadata:  
  name: eph-owner-tokens  
  namespace: ephemeral-python-app  
type: Opaque  
data:  
  harsh: YmVhcmVyLXRva2Vu  

Owner must be lowercase alphanumeric.

---

## Namespace Format

eph1  
eph2  
eph25  

Labels:

ephemeral.owner=<owner>

Annotations:

ephemeral.fe-branch=<branch>  
ephemeral.be-branch=<branch>  
ephemeral.created-at=<timestamp>  

---

## Docker

Build:

docker build -t ephemeral-manager .

Run:

docker run -p 8000:8000 -e GITLAB_PROJECT_ID=123 -e GITLAB_TRIGGER_TOKEN=xxxx ephemeral-manager

Service:

http://localhost:8000

---

## Kubernetes Requirements

RBAC:

namespaces: list  
secrets: get  

---

## API

Health:

GET /healthz

List:

GET /list  
Authorization: Bearer <token>

---

## Local Development

pip install -r requirements.txt  
uvicorn app.main:app --reload  

---

## Security

Non-root container  
Secrets in Kubernetes  
Bearer authentication  
Input validation  
GitLab timeout  

---