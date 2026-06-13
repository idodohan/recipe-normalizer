"""Extraction pipeline: per-source acquire plugins + the shared normalize stage.

Importing this package registers all built-in plugins in EXTRACTORS.
Extraction is stateless — it never imports ingestion; the worker wires jobs
to plugins and persists results.
"""

from recipe_normalizer.extraction import (
    image_plugin,  # noqa: F401  (registers ImageExtractor)
    pdf_plugin,  # noqa: F401  (registers PdfExtractor)
    text_plugin,  # noqa: F401  (registers TextExtractor)
    url_plugin,  # noqa: F401  (registers UrlExtractor)
)
from recipe_normalizer.extraction.base import (
    EXTRACTORS,
    Acquired,
    Extractor,
    TierFailed,
    register,
)

__all__ = ["EXTRACTORS", "Acquired", "Extractor", "TierFailed", "register"]
