"""Loads config/taxonomy.yaml: categories, priorities and the review policy."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class Category(BaseModel):
    description: str
    team: str
    hubspot_category: str


class Priority(BaseModel):
    description: str
    sla_hours: int = Field(gt=0)
    hubspot_priority: str


class ReviewPolicy(BaseModel):
    min_confidence: float = Field(ge=0.0, le=1.0)
    always_review_priorities: list[str] = Field(default_factory=list)
    always_review_categories: list[str] = Field(default_factory=list)
    required_customer_fields: list[str] = Field(default_factory=list)


class Taxonomy(BaseModel):
    categories: dict[str, Category]
    priorities: dict[str, Priority]
    review_policy: ReviewPolicy

    @model_validator(mode="after")
    def _check_references(self) -> Taxonomy:
        if not self.categories or not self.priorities:
            raise ValueError("taxonomy needs at least one category and one priority")
        unknown_p = set(self.review_policy.always_review_priorities) - set(self.priorities)
        unknown_c = set(self.review_policy.always_review_categories) - set(self.categories)
        if unknown_p or unknown_c:
            raise ValueError(f"review_policy references unknown keys: {unknown_p | unknown_c}")
        return self

    @property
    def category_keys(self) -> list[str]:
        return list(self.categories)

    @property
    def priority_keys(self) -> list[str]:
        return list(self.priorities)


def load_taxonomy(path: Path) -> Taxonomy:
    with path.open(encoding="utf-8") as fh:
        return Taxonomy.model_validate(yaml.safe_load(fh))


@lru_cache
def get_taxonomy() -> Taxonomy:
    from ticket_triage.settings import get_settings

    return load_taxonomy(get_settings().taxonomy_path)
