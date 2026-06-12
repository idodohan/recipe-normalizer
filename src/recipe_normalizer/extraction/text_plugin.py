"""Text passthrough plugin (spec §6.2): pasted/uploaded text needs no acquire work."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from recipe_normalizer.extraction.base import Acquired, register

if TYPE_CHECKING:
    from recipe_normalizer.filestore import FileStore
    from recipe_normalizer.llm.client import LLMClient

__all__ = ["TextExtractor"]


class TextExtractor:
    """Passthrough: payload {"text": str} → Acquired(text=...), raw text retained."""

    input_type: ClassVar[str] = "text"

    def acquire(self, payload: dict[str, Any], *, llm: LLMClient, store: FileStore) -> Acquired:
        text: str = payload["text"]
        ref = store.save(text.encode("utf-8"), suffix="txt")
        return Acquired(text=text, artifacts={"raw_text_ref": ref})


register(TextExtractor())
