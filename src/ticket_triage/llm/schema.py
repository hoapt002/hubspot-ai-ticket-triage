"""Structured output the model must return, built from the taxonomy."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from ticket_triage.taxonomy import Taxonomy

Sentiment = Literal["positive", "neutral", "negative", "angry"]


class ExtractedCustomer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(description="Customer's full name, or null if not stated.")
    email: str | None = Field(description="Customer's email address, or null if not stated.")
    phone: str | None = Field(description="Phone number in the form written, or null.")
    company: str | None = Field(description="Company/organisation name, or null.")
    order_id: str | None = Field(description="Order, invoice or booking number, or null.")
    account_id: str | None = Field(description="Account/customer/user id, or null.")
    product: str | None = Field(description="Product or plan the ticket is about, or null.")


class TriageOutputBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rationale: str = Field(description="One or two sentences explaining the classification.")
    category: str
    priority: str
    confidence: float = Field(
        description="Probability (0 to 1) that both category and priority are correct."
    )
    summary: str = Field(description="One-sentence English summary an agent can act on.")
    language: str = Field(description="ISO 639-1 code of the ticket language, e.g. 'en', 'vi'.")
    sentiment: Sentiment
    customer: ExtractedCustomer
    needs_human: bool = Field(
        description="True if the ticket is ambiguous, contains several unrelated requests, "
        "or asks for something an agent must decide (exceptions, compensation, legal)."
    )
    needs_human_reason: str | None = Field(description="Why a person must look, else null.")


@lru_cache
def _output_model(categories: tuple[str, ...], priorities: tuple[str, ...]) -> type[BaseModel]:
    fields: dict[str, Any] = {
        "category": (Literal[categories], Field(description="Category key from the taxonomy.")),
        "priority": (Literal[priorities], Field(description="Priority key from the taxonomy.")),
    }
    return create_model("TriageOutput", __base__=TriageOutputBase, **fields)


def output_model_for(taxonomy: Taxonomy) -> type[BaseModel]:
    return _output_model(tuple(taxonomy.category_keys), tuple(taxonomy.priority_keys))
