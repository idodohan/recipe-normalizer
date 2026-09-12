import asyncio
from recipe_normalizer.llm.client import LLMClient
from recipe_normalizer.extraction.normalize import NormalizeResult
import os

client = LLMClient()
res = client.structured(
    feature="test",
    output_model=NormalizeResult,
    content="Title: Chocolate Chip Cookies\nIngredients: 1 cup sugar, 2 cups flour",
    system="Extract recipe"
)
print("Extraction success:", res.is_recipe)
print("Title:", res.recipes[0].title if res.recipes else None)
