from __future__ import annotations

import os

os.environ.setdefault("TEST_DATABASE_URL", "postgresql+psycopg://localhost:5432/ticket_triage_test")
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["SESSION_SECRET"] = "test-session-secret-0123456789abcdef"

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from ticket_triage.app import create_app
from ticket_triage.auth import hash_password
from ticket_triage.crm.hubspot import HubSpotClient
from ticket_triage.db import get_engine, get_sessionmaker
from ticket_triage.llm.providers import LLMResult
from ticket_triage.models import User
from ticket_triage.pipeline import Pipeline
from ticket_triage.settings import PROJECT_ROOT, Settings
from ticket_triage.taxonomy import Taxonomy, load_taxonomy
from ticket_triage.webhooks.signature import HEADER, sign
from ticket_triage.worker import Worker

WEBHOOK_SECRET = "whsec-test-0123456789"
REVIEWER_PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> None:
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")


@pytest.fixture(autouse=True)
def _clean_tables() -> Iterator[None]:
    yield
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "TRUNCATE jobs, review_sessions, review_tasks, triage_runs, tickets, users CASCADE"
            )
        )


@pytest.fixture
def taxonomy() -> Taxonomy:
    return load_taxonomy(PROJECT_ROOT / "config" / "taxonomy.yaml")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        session_secret=SecretStr(os.environ["SESSION_SECRET"]),
        webhook_secrets={"web": SecretStr(WEBHOOK_SECRET)},
        postmark_inbound_user="postmark",
        postmark_inbound_password=SecretStr("pm-password-123"),
        llm_provider="openai",
        llm_model="test-model",
        llm_price_input_per_mtok="0.75",
        llm_price_output_per_mtok="4.5",
        manual_baseline_rate=0.0,
        job_max_attempts=3,
    )


def triage_output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "rationale": "Customer reports a duplicate charge on their card.",
        "category": "billing",
        "priority": "P3",
        "confidence": 0.93,
        "summary": "Customer was charged twice for the March invoice.",
        "language": "en",
        "sentiment": "negative",
        "customer": {
            "name": "Dana Whitfield",
            "email": None,
            "phone": None,
            "company": "Northwind Labs",
            "order_id": "INV-20391",
            "account_id": None,
            "product": "Pro plan",
        },
        "needs_human": False,
        "needs_human_reason": None,
    }
    output.update(overrides)
    return output


@dataclass
class ScriptedModel:
    """Test double for TriageModel: returns queued outputs or raises."""

    responses: list[dict[str, Any] | Exception] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    provider: str = "openai"
    model: str = "test-model"

    def classify(self, system: str, user: str, schema: type[BaseModel]) -> LLMResult:
        self.calls.append(user)
        response = self.responses.pop(0) if self.responses else triage_output()
        if isinstance(response, Exception):
            raise response
        schema.model_validate(response)  # the real providers guarantee this
        return LLMResult(
            output=json.loads(json.dumps(response)),
            provider=self.provider,
            model=self.model,
            input_tokens=1200,
            output_tokens=180,
            latency_ms=850,
        )


@dataclass
class FakeHubSpot:
    """Records HubSpot API calls through an httpx MockTransport."""

    existing_contact_id: str | None = None
    fail_with: int | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"message": "nope"})
        path = request.url.path
        if path == "/crm/v3/objects/contacts/search":
            results = [{"id": self.existing_contact_id}] if self.existing_contact_id else []
            return httpx.Response(200, json={"total": len(results), "results": results})
        if path == "/crm/v3/objects/contacts":
            return httpx.Response(201, json={"id": "501"})
        if path == "/crm/v3/objects/tickets":
            return httpx.Response(201, json={"id": "9001"})
        return httpx.Response(404, json={"message": f"unexpected {path}"})

    def client(self) -> HubSpotClient:
        return HubSpotClient(
            "pat-test",
            pipeline_id="0",
            pipeline_stage_id="1",
            transport=httpx.MockTransport(self.handler),
        )

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]


@pytest.fixture
def model() -> ScriptedModel:
    return ScriptedModel()


@pytest.fixture
def hubspot() -> FakeHubSpot:
    return FakeHubSpot()


@pytest.fixture
def pipeline(
    settings: Settings, taxonomy: Taxonomy, model: ScriptedModel, hubspot: FakeHubSpot
) -> Pipeline:
    return Pipeline(
        settings=settings,
        taxonomy=taxonomy,
        model_factory=lambda: model,
        hubspot_factory=hubspot.client,
    )


@pytest.fixture
def worker(pipeline: Pipeline, settings: Settings) -> Worker:
    return Worker(pipeline, get_sessionmaker(), settings)


@pytest.fixture
def client(settings: Settings, taxonomy: Taxonomy, pipeline: Pipeline) -> TestClient:
    return TestClient(create_app(settings, taxonomy, pipeline))


@pytest.fixture
def db() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def reviewer(db: Session) -> User:
    user = User(
        email="agent@example.com", name="Ava Agent", password_hash=hash_password(REVIEWER_PASSWORD)
    )
    db.add(user)
    db.commit()
    return user


def drain(worker: Worker, limit: int = 20) -> int:
    processed = 0
    while processed < limit and worker.run_once():
        processed += 1
    return processed


def post_ticket(client: TestClient, payload: dict[str, Any], source: str = "web") -> httpx.Response:
    body = json.dumps(payload).encode()
    return client.post(
        f"/webhooks/tickets/{source}",
        content=body,
        headers={"Content-Type": "application/json", HEADER: sign(WEBHOOK_SECRET, body)},
    )


def ticket_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": f"form-{uuid.uuid4().hex[:10]}",
        "channel": "web_form",
        "subject": "Charged twice for March",
        "body": "Hi, I was charged twice for invoice INV-20391 on our Pro plan. "
        "Please refund the duplicate. Dana Whitfield, Northwind Labs",
        "customer": {"email": "dana@northwind.example", "name": "Dana Whitfield"},
    }
    payload.update(overrides)
    return payload


def login(client: TestClient, email: str = "agent@example.com") -> None:
    page = client.get("/login")
    csrf = extract_csrf(page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": REVIEWER_PASSWORD, "csrf": csrf, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def extract_csrf(html: str) -> str:
    marker = 'name="csrf" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def extract_value(html: str, name: str) -> str:
    marker = f'name="{name}" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


Factory = Callable[..., Any]
