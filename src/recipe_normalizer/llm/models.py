import uuid
from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from recipe_normalizer.db import Base, TimestampMixin, new_uuid


class LlmUsage(TimestampMixin, Base):
    """One row per LLM API call: who/what spent how many tokens at what cost."""

    __tablename__ = "llm_usage"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    feature: Mapped[str] = mapped_column(String(100), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    model: Mapped[str] = mapped_column(String(60))
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6))
