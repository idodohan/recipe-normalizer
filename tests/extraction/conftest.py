"""Shared fixtures for extraction tests.

StubLLM is the ONLY way extraction tests fake the LLM — it stands in for the
LLMClient seam (never the anthropic SDK; that is reserved for tests/llm).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy.orm import Session

from recipe_normalizer.catalog.seed_loader import load_seed
from recipe_normalizer.users.models import User


class StubLLM:
    """Stub of the LLMClient seam implementing just ``structured``.

    - ``extract.normalize`` calls return the canned *result* (asserts one was given).
    - ``catalog.match`` band questions answer "no match" (index=None) so persist
      tests exercise the real fuzzy matcher without scripted choices.
    - Any other feature is unexpected and fails the test.

    Every call is recorded in ``self.calls`` for assertions on kwargs.
    """

    def __init__(self, result: BaseModel | None = None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def structured(
        self,
        *,
        feature: str,
        output_model: type[Any],
        content: str | list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        fast: bool = False,
        max_tokens: int = 8000,
    ) -> Any:
        self.calls.append(
            {
                "feature": feature,
                "output_model": output_model,
                "content": content,
                "system": system,
                "model": model,
                "fast": fast,
                "max_tokens": max_tokens,
            }
        )
        if feature == "extract.normalize":
            assert self.result is not None, "StubLLM has no canned NormalizeResult"
            return self.result
        if feature == "catalog.match":
            return output_model(index=None)
        raise AssertionError(f"unexpected LLM feature: {feature!r}")


@pytest.fixture()
def seeded(db_session: Session) -> Session:
    """Load catalog seed data and return the session."""
    load_seed(db_session)
    return db_session


@pytest.fixture()
def owner(seeded: Session) -> User:
    user = User(
        email=f"extract-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="hash",
        display_name="Extractor",
    )
    seeded.add(user)
    seeded.flush()
    return user
