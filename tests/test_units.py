from __future__ import annotations

import uuid

import httpx
import pytest
from pydantic import ValidationError

from tests.conftest import FakeHubSpot, triage_output
from ticket_triage.crm.hubspot import HubSpotError, TicketDraft, split_name
from ticket_triage.llm.schema import output_model_for
from ticket_triage.pipeline import changed_fields, in_manual_baseline, review_reasons
from ticket_triage.taxonomy import Taxonomy
from ticket_triage.webhooks import signature
from ticket_triage.webhooks.payloads import GenericTicketPayload, PostmarkInbound

# ---------------------------------------------------------------- signatures


def test_signature_roundtrip() -> None:
    body = b'{"id":"1"}'
    header = signature.sign("secret", body, timestamp=1_700_000_000)
    signature.verify("secret", body, header, 300, now=1_700_000_100)


@pytest.mark.parametrize(
    ("body", "secret", "now", "message"),
    [
        (b'{"id":"2"}', "secret", 1_700_000_000, "mismatch"),
        (b'{"id":"1"}', "other", 1_700_000_000, "mismatch"),
        (b'{"id":"1"}', "secret", 1_700_000_301, "tolerance"),
    ],
)
def test_signature_rejects(body: bytes, secret: str, now: int, message: str) -> None:
    header = signature.sign("secret", b'{"id":"1"}', timestamp=1_700_000_000)
    with pytest.raises(signature.SignatureError, match=message):
        signature.verify(secret, body, header, 300, now=now)


@pytest.mark.parametrize("header", [None, "", "v1=abc", "t=notanumber,v1=abc"])
def test_signature_rejects_malformed(header: str | None) -> None:
    with pytest.raises(signature.SignatureError):
        signature.verify("secret", b"{}", header, 300)


def test_signature_accepts_any_of_several_v1_during_rotation() -> None:
    body = b"{}"
    good = signature.sign("secret", body, timestamp=100).split("v1=")[1]
    signature.verify("secret", body, f"t=100,v1=deadbeef,v1={good}", 300, now=100)


# ---------------------------------------------------------------- payloads


def test_generic_payload_normalizes() -> None:
    ticket = GenericTicketPayload.model_validate(
        {
            "id": "abc",
            "subject": "  Hello ",
            "body": " Help ",
            "customer": {"email": "Dana@Example.com"},
        }
    ).normalize()
    assert ticket.subject == "Hello"
    assert ticket.body == "Help"
    assert ticket.sender_email == "dana@example.com"
    assert ticket.channel == "web_form"


def test_generic_payload_requires_body_and_valid_email() -> None:
    with pytest.raises(ValidationError):
        GenericTicketPayload.model_validate({"id": "x", "body": ""})
    with pytest.raises(ValidationError):
        GenericTicketPayload.model_validate(
            {"id": "x", "body": "hi", "customer": {"email": "not-an-email"}}
        )


def test_postmark_prefers_stripped_reply_and_falls_back_to_html() -> None:
    reply = PostmarkInbound.model_validate(
        {
            "MessageID": "m1",
            "FromFull": {"Email": "Kim@Example.com", "Name": "Kim Lee"},
            "Subject": "Re: order",
            "TextBody": "new text\n> quoted old text",
            "StrippedTextReply": "new text",
        }
    ).normalize()
    assert reply.body == "new text"
    assert reply.sender_email == "kim@example.com"
    assert reply.sender_name == "Kim Lee"
    assert reply.channel == "email"

    html = PostmarkInbound.model_validate(
        {
            "MessageID": "m2",
            "From": "a@b.co",
            "HtmlBody": "<p>Line one</p><style>p{}</style><p>Line&nbsp;two</p>",
        }
    ).normalize()
    assert html.body == "Line one\nLine\xa0two"


# ---------------------------------------------------------------- policy


def test_review_reasons_auto_when_confident(taxonomy: Taxonomy) -> None:
    assert review_reasons(triage_output(), "dana@example.com", taxonomy) == []


def test_review_reasons_collects_every_trigger(taxonomy: Taxonomy) -> None:
    output = triage_output(confidence=0.4, priority="P1", category="complaint", needs_human=True)
    reasons = review_reasons(output, None, taxonomy)
    assert reasons == [
        "low_confidence",
        "priority_P1",
        "category_complaint",
        "missing_email",
        "model_flagged",
    ]


def test_extracted_email_satisfies_required_field(taxonomy: Taxonomy) -> None:
    output = triage_output()
    output["customer"]["email"] = "found@in.text"
    assert review_reasons(output, None, taxonomy) == []


def test_manual_baseline_sampling_is_deterministic_and_matches_rate() -> None:
    ids = [uuid.uuid4() for _ in range(20_000)]
    share = sum(in_manual_baseline(i, 0.1) for i in ids) / len(ids)
    assert 0.09 < share < 0.11
    assert all(in_manual_baseline(i, 0.1) == in_manual_baseline(i, 0.1) for i in ids[:100])
    assert not any(in_manual_baseline(i, 0.0) for i in ids[:100])
    assert all(in_manual_baseline(i, 1.0) for i in ids[:100])


def test_changed_fields() -> None:
    proposal = {
        "category": "billing",
        "priority": "P3",
        "summary": "s",
        "language": "en",
        "sentiment": "negative",
        "customer": {"email": "a@b.co", "order_id": None},
    }
    decision = {**proposal, "priority": "P2", "customer": {"email": "a@b.co", "order_id": "7"}}
    assert changed_fields(proposal, decision) == ["priority", "customer.order_id"]


def test_output_schema_restricts_categories(taxonomy: Taxonomy) -> None:
    schema = output_model_for(taxonomy)
    schema.model_validate(triage_output())
    with pytest.raises(ValidationError):
        schema.model_validate(triage_output(category="made_up"))
    json_schema = schema.model_json_schema()
    assert json_schema["properties"]["category"]["enum"] == taxonomy.category_keys


# ---------------------------------------------------------------- HubSpot client


def _draft(email: str | None = "dana@northwind.example") -> TicketDraft:
    return TicketDraft(
        subject="Charged twice",
        content="Summary: ...",
        priority="MEDIUM",
        category="BILLING_ISSUE",
        source_type="FORM",
        contact_email=email,
        contact_firstname="Dana",
        contact_lastname="Whitfield",
        contact_phone=None,
        contact_company="Northwind Labs",
    )


def test_hubspot_creates_contact_and_associated_ticket() -> None:
    fake = FakeHubSpot()
    result = fake.client().sync(_draft())
    assert (result.ticket_id, result.contact_id) == ("9001", "501")

    contact = fake.bodies("/crm/v3/objects/contacts")[0]
    assert contact == {
        "properties": {
            "email": "dana@northwind.example",
            "firstname": "Dana",
            "lastname": "Whitfield",
            "company": "Northwind Labs",
        }
    }
    ticket = fake.bodies("/crm/v3/objects/tickets")[0]
    assert ticket["properties"]["hs_pipeline_stage"] == "1"
    assert ticket["properties"]["hs_ticket_priority"] == "MEDIUM"
    assert ticket["associations"] == [
        {
            "to": {"id": "501"},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 16}],
        }
    ]
    assert fake.requests[0].headers["Authorization"] == "Bearer pat-test"


def test_hubspot_reuses_existing_contact() -> None:
    fake = FakeHubSpot(existing_contact_id="777")
    result = fake.client().sync(_draft())
    assert result.contact_id == "777"
    assert fake.bodies("/crm/v3/objects/contacts") == []


def test_hubspot_ticket_without_email_has_no_contact() -> None:
    fake = FakeHubSpot()
    result = fake.client().sync(_draft(email=None))
    assert result.contact_id is None
    assert "associations" not in fake.bodies("/crm/v3/objects/tickets")[0]


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (502, True), (400, False)])
def test_hubspot_errors_classified(status: int, retryable: bool) -> None:
    fake = FakeHubSpot(fail_with=status)
    with pytest.raises(HubSpotError) as info:
        fake.client().sync(_draft())
    assert info.value.retryable is retryable
    assert info.value.status == status


def test_hubspot_network_error_is_retryable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    from ticket_triage.crm.hubspot import HubSpotClient

    client = HubSpotClient(
        "t", pipeline_id="0", pipeline_stage_id="1", transport=httpx.MockTransport(boom)
    )
    with pytest.raises(HubSpotError) as info:
        client.sync(_draft())
    assert info.value.retryable


def test_split_name() -> None:
    assert split_name("Dana Whitfield") == ("Dana", "Whitfield")
    assert split_name("Nguyễn Văn An") == ("Nguyễn", "Văn An")
    assert split_name("Cher") == ("Cher", None)
    assert split_name("  ") == (None, None)
