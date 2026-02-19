import os
import re
import base64
import socket
import logging
from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from kubernetes import client, config

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("ephemeral-manager")

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------
PREFIX = os.getenv("EPH_PREFIX", "eph")
SLOTS = int(os.getenv("EPH_SLOTS", "25"))

K8S_SECRET_NAME = os.getenv("OWNER_SECRET_NAME", "eph-owner-tokens")
K8S_SECRET_NAMESPACE = os.getenv("OWNER_SECRET_NAMESPACE", "ephemeral-python-app")

MAX_ENV_PER_OWNER = 2

# ------------------------------------------------------------------------------
# FastAPI setup
# ------------------------------------------------------------------------------
app = FastAPI(title="Ephemeral Environment Manager", version="4.0")
security = HTTPBearer(auto_error=False)

# ------------------------------------------------------------------------------
# Kubernetes Client
# ------------------------------------------------------------------------------
def load_kube_client():
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.CoreV1Api()

# ------------------------------------------------------------------------------
# Load owner tokens from Kubernetes Secret
# ------------------------------------------------------------------------------
def load_owner_tokens() -> dict:
    v1 = load_kube_client()

    try:
        secret = v1.read_namespaced_secret(
            name=K8S_SECRET_NAME,
            namespace=K8S_SECRET_NAMESPACE,
        )

        tokens = {}
        for owner, encoded in secret.data.items():
            tokens[owner] = base64.b64decode(encoded).decode("utf-8").strip()

        return tokens

    except Exception:
        logger.exception("Failed to load owner tokens")
        raise HTTPException(status_code=500, detail="Unable to load authentication tokens")

# ------------------------------------------------------------------------------
# Validators
# ------------------------------------------------------------------------------
def validate_owner(owner: str):
    if not re.match(r"^[a-z0-9]+$", owner):
        raise HTTPException(
            status_code=400,
            detail="EPH_OWNER must be lowercase and alphanumeric only",
        )
    return owner


def validate_namespace(eph_ns: str):
    valid = [f"{PREFIX}{i}" for i in range(1, SLOTS + 1)]
    if eph_ns not in valid:
        raise HTTPException(status_code=400, detail=f"Invalid namespace: {eph_ns}")
    return eph_ns


def validate_branch(branch: str):
    if not re.match(r"^[A-Za-z0-9._/\-]+$", branch):
        raise HTTPException(status_code=400, detail="Invalid branch name")
    return branch

# ------------------------------------------------------------------------------
# Authentication
# ------------------------------------------------------------------------------
def get_bearer_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    return credentials.credentials


def authenticate(owner: str, token: str):
    owner = validate_owner(owner)
    tokens = load_owner_tokens()

    if owner not in tokens:
        raise HTTPException(status_code=403, detail="Owner not registered")

    if token != tokens[owner]:
        raise HTTPException(status_code=403, detail="Invalid bearer token")

# ------------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------------
class NamespaceInfo(BaseModel):
    name: str
    owner: str | None
    fe_branch: str | None
    be_branch: str | None
    created_at: str | None


class ListResponse(BaseModel):
    total_slots: int
    prefix: str
    used: List[NamespaceInfo]
    available: List[str]
    timestamp: str
    host: str


class ActionResponse(BaseModel):
    status: str
    message: str
    gitlab_pipeline_url: str | None = None
    timestamp: str

# ------------------------------------------------------------------------------
# GitLab Trigger
# ------------------------------------------------------------------------------
def trigger_gitlab_pipeline(variables: dict):
    import requests

    GITLAB_API_URL = os.getenv("GITLAB_API_URL", "https://gitlab.com/api/v4")
    GITLAB_PROJECT_ID = os.getenv("GITLAB_PROJECT_ID")
    GITLAB_TRIGGER_TOKEN = os.getenv("GITLAB_TRIGGER_TOKEN")
    GITLAB_REF = os.getenv("GITLAB_REF_BRANCH", "terraform-iac")

    if not all([GITLAB_PROJECT_ID, GITLAB_TRIGGER_TOKEN]):
        raise HTTPException(status_code=500, detail="GitLab configuration missing")

    url = f"{GITLAB_API_URL}/projects/{GITLAB_PROJECT_ID}/trigger/pipeline"

    payload = {
        "token": GITLAB_TRIGGER_TOKEN,
        "ref": GITLAB_REF,
    }

    for k, v in variables.items():
        payload[f"variables[{k}]"] = v

    response = requests.post(url, data=payload, timeout=15)

    if response.status_code not in (200, 201, 202):
        logger.error(response.text)
        raise HTTPException(status_code=502, detail="GitLab pipeline trigger failed")

    return response.json().get("web_url")

# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------
@app.get("/list", response_model=ListResponse)
def list_ephemeral(token: str = Depends(get_bearer_token)):
    api = load_kube_client()
    namespaces = api.list_namespace().items

    used = []
    all_slots = [f"{PREFIX}{i}" for i in range(1, SLOTS + 1)]

    pattern = re.compile(rf"^{PREFIX}[1-9][0-9]*$")

    for ns in namespaces:
        name = ns.metadata.name

        # Only match eph<number>
        if not pattern.match(name):
            continue

        labels = ns.metadata.labels or {}
        annotations = ns.metadata.annotations or {}

        used.append(
            {
                "name": name,
                "owner": labels.get("ephemeral.owner"),
                "fe_branch": annotations.get("ephemeral.fe-branch"),
                "be_branch": annotations.get("ephemeral.be-branch"),
                "created_at": annotations.get("ephemeral.created-at"),
            }
        )

    used_names = {n["name"] for n in used}
    available = [n for n in all_slots if n not in used_names]

    return ListResponse(
        total_slots=SLOTS,
        prefix=PREFIX,
        used=used,
        available=available,
        timestamp=datetime.now(timezone.utc).isoformat(),
        host=socket.gethostname(),
    )

@app.post("/create", response_model=ActionResponse)
def create_ephemeral(
    req: CreateRequest,
    token: str = Depends(get_bearer_token),
):
    authenticate(req.EPH_OWNER, token)

    api = load_kube_client()
    namespaces = api.list_namespace().items

    owner_envs = [
        ns.metadata.name
        for ns in namespaces
        if ns.metadata.labels
        and ns.metadata.labels.get("ephemeral.owner") == req.EPH_OWNER
    ]

    if len(owner_envs) >= MAX_ENV_PER_OWNER:
        raise HTTPException(
            status_code=403,
            detail=f"Owner '{req.EPH_OWNER}' already has {MAX_ENV_PER_OWNER} environments",
        )

    eph_ns = validate_namespace(req.EPH_NAMESPACE)

    fe_branch = "staging" if req.TARGET_FRONTEND_BRANCH == "default" else req.TARGET_FRONTEND_BRANCH
    be_branch = "staging" if req.TARGET_BACKEND_BRANCH == "default" else req.TARGET_BACKEND_BRANCH

    validate_branch(fe_branch)
    validate_branch(be_branch)

    pipeline_url = trigger_gitlab_pipeline(
        {
            "FB_DEPLOY_ACTION": "apply",
            "EPH_NAMESPACE": eph_ns,
            "TARGET_FRONTEND_BRANCH": fe_branch,
            "TARGET_BACKEND_BRANCH": be_branch,
            "EPH_OWNER": req.EPH_OWNER,
        }
    )

    return ActionResponse(
        status="success",
        message=f"Environment created by {req.EPH_OWNER}",
        gitlab_pipeline_url=pipeline_url,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

@app.post("/delete")
def delete_ephemeral(
    req: DeleteRequest,
    token: str = Depends(get_bearer_token),
):
    authenticate(req.EPH_OWNER, token)

    results = []

    for ns in req.EPH_NAMESPACE:
        eph_ns = validate_namespace(ns)

        pipeline_url = trigger_gitlab_pipeline(
            {
                "FB_DEPLOY_ACTION": "destroy",
                "EPH_NAMESPACE": eph_ns,
                "EPH_OWNER": req.EPH_OWNER,
            }
        )

        results.append(
            {
                "namespace": eph_ns,
                "pipeline_url": pipeline_url,
                "message": f"Deleted by {req.EPH_OWNER}",
            }
        )

    return {
        "status": "success",
        "count": len(results),
        "results": results,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}
