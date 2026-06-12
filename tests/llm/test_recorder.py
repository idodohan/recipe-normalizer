from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from recipe_normalizer.llm.client import DbUsageRecorder
from recipe_normalizer.llm.models import LlmUsage


def test_db_usage_recorder_writes_row(db_session: Session) -> None:
    user_id = uuid4()
    job_id = uuid4()
    recorder = DbUsageRecorder(db_session, user_id=user_id, job_id=job_id)

    recorder.record(
        feature="extract",
        model="claude-opus-4-8",
        input_tokens=1000,
        output_tokens=500,
        cost=0.0175,
    )

    row = db_session.execute(select(LlmUsage)).scalar_one()
    assert row.feature == "extract"
    assert row.user_id == user_id
    assert row.job_id == job_id
    assert row.model == "claude-opus-4-8"
    assert row.input_tokens == 1000
    assert row.output_tokens == 500
    assert float(row.cost_usd) == pytest.approx(0.0175)
    assert row.id is not None
    assert row.created_at is not None


def test_db_usage_recorder_nullable_user_and_job_ids(db_session: Session) -> None:
    recorder = DbUsageRecorder(db_session)

    recorder.record(
        feature="classify",
        model="claude-haiku-4-5",
        input_tokens=10,
        output_tokens=2,
        cost=0.00002,
    )

    row = db_session.execute(select(LlmUsage)).scalar_one()
    assert row.user_id is None
    assert row.job_id is None


def test_db_usage_recorder_flushes_without_committing(db_session: Session) -> None:
    recorder = DbUsageRecorder(db_session)
    recorder.record(
        feature="extract", model="claude-opus-4-8", input_tokens=1, output_tokens=1, cost=0.0
    )
    # The row is visible inside the session (flushed) without an explicit commit.
    assert db_session.execute(select(LlmUsage)).scalar_one() is not None
