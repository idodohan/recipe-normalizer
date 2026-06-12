"""Service-level tests for catalog matching, creation, merge, search, update."""

import pytest

from recipe_normalizer.catalog import service
from recipe_normalizer.catalog.models import IngredientStatus
from recipe_normalizer.catalog.seed_loader import load_seed
from recipe_normalizer.errors import ApiError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(db_session):  # type: ignore[no-untyped-def]
    """Load seed data and return the session."""
    load_seed(db_session)
    return db_session


# ---------------------------------------------------------------------------
# match
# ---------------------------------------------------------------------------


def test_match_alias_case_insensitive(seeded) -> None:
    result = service.match(seeded, "AP flour")
    assert result is not None
    assert result.name == "all-purpose flour"


def test_match_alias_uppercase(seeded) -> None:
    result = service.match(seeded, "ap flour")
    assert result is not None
    assert result.name == "all-purpose flour"


def test_match_alias_with_whitespace(seeded) -> None:
    result = service.match(seeded, "  ap FLOUR ")
    assert result is not None
    assert result.name == "all-purpose flour"


def test_match_exact_name(seeded) -> None:
    result = service.match(seeded, "all-purpose flour")
    assert result is not None
    assert result.name == "all-purpose flour"


def test_match_multilingual_hebrew(seeded) -> None:
    result = service.match(seeded, "קמח")
    assert result is not None
    assert result.name == "all-purpose flour"


def test_match_unknown_returns_none(seeded) -> None:
    result = service.match(seeded, "zzgremlin dust")
    assert result is None


# ---------------------------------------------------------------------------
# create_unreviewed
# ---------------------------------------------------------------------------


def test_create_unreviewed_new_ingredient(db_session) -> None:
    ing = service.create_unreviewed(db_session, name="gremlin dust")
    assert ing.status == IngredientStatus.unreviewed
    alias_texts = [a.alias for a in ing.aliases]
    assert "gremlin dust" in alias_texts
    # The alias language should be "und"
    und_aliases = [a for a in ing.aliases if a.alias == "gremlin dust"]
    assert und_aliases[0].language == "und"


def test_create_unreviewed_idempotent(db_session) -> None:
    ing1 = service.create_unreviewed(db_session, name="gremlin dust")
    ing2 = service.create_unreviewed(db_session, name="gremlin dust")
    assert ing1.id == ing2.id


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


@pytest.fixture()
def two_ingredients(db_session):  # type: ignore[no-untyped-def]
    """Create two unreviewed ingredients and return (source, target, session)."""
    source = service.create_unreviewed(db_session, name="AP flour variant")
    target = service.create_unreviewed(db_session, name="all-purpose flour variant")
    return source, target, db_session


def test_merge_target_has_both_alias_sets(two_ingredients) -> None:
    source, target, db = two_ingredients
    source_alias_texts = [a.alias for a in source.aliases]
    result = service.merge(db, source_id=source.id, target_id=target.id)
    # target should now have all source aliases + source name as alias
    result_alias_texts = [a.alias for a in result.aliases]
    for alias in source_alias_texts:
        assert alias in result_alias_texts
    assert source.name in result_alias_texts


def test_merge_source_merged_into_id_set(two_ingredients) -> None:
    source, target, db = two_ingredients
    service.merge(db, source_id=source.id, target_id=target.id)
    assert source.merged_into_id == target.id


def test_merge_match_on_source_alias_resolves_to_target(two_ingredients) -> None:
    source, target, db = two_ingredients
    old_source_alias = source.aliases[0].alias  # "AP flour variant" stored as alias
    service.merge(db, source_id=source.id, target_id=target.id)
    result = service.match(db, old_source_alias)
    assert result is not None
    assert result.id == target.id


def test_merge_hook_fires(two_ingredients) -> None:
    source, target, db = two_ingredients
    calls: list[tuple] = []

    def my_hook(sess, src_id, tgt_id) -> None:  # type: ignore[no-untyped-def]
        calls.append((src_id, tgt_id))

    service.register_merge_hook(my_hook)
    try:
        service.merge(db, source_id=source.id, target_id=target.id)
        assert len(calls) == 1
        assert calls[0] == (source.id, target.id)
    finally:
        # Clean up hooks so they don't bleed into other tests
        service._MERGE_HOOKS.remove(my_hook)


def test_merge_into_itself_raises_422(two_ingredients) -> None:
    source, target, db = two_ingredients
    with pytest.raises(ApiError) as exc_info:
        service.merge(db, source_id=source.id, target_id=source.id)
    assert exc_info.value.status_code == 422


def test_merge_already_merged_source_raises_409(two_ingredients) -> None:
    source, target, db = two_ingredients
    # A third ingredient to merge into
    third = service.create_unreviewed(db, name="third ingredient")
    service.merge(db, source_id=source.id, target_id=target.id)
    with pytest.raises(ApiError) as exc_info:
        service.merge(db, source_id=source.id, target_id=third.id)
    assert exc_info.value.status_code == 409


def test_merge_unknown_source_raises_404(two_ingredients) -> None:
    source, target, db = two_ingredients
    import uuid

    with pytest.raises(ApiError) as exc_info:
        service.merge(db, source_id=uuid.uuid4(), target_id=target.id)
    assert exc_info.value.status_code == 404


def test_merge_unknown_target_raises_404(two_ingredients) -> None:
    source, target, db = two_ingredients
    import uuid

    with pytest.raises(ApiError) as exc_info:
        service.merge(db, source_id=source.id, target_id=uuid.uuid4())
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_substring_match(seeded) -> None:
    results = service.search(seeded, "flou")
    names = [r.name for r in results]
    assert "all-purpose flour" in names


def test_search_empty_query_returns_all(seeded) -> None:
    results = service.search(seeded, "")
    assert len(results) > 0


def test_search_filter_by_status(db_session) -> None:
    load_seed(db_session)
    service.create_unreviewed(db_session, name="mystery spice")
    results = service.search(db_session, "", status=IngredientStatus.unreviewed)
    assert all(r.status == IngredientStatus.unreviewed for r in results)
    names = [r.name for r in results]
    assert "mystery spice" in names


# ---------------------------------------------------------------------------
# update_ingredient
# ---------------------------------------------------------------------------


def test_update_dietary_flags_assignment(db_session) -> None:
    ing = service.create_unreviewed(db_session, name="test ingr")
    updated = service.update_ingredient(db_session, ing.id, dietary_flags=["dairy", "egg"])
    assert updated.dietary_flags == ["dairy", "egg"]


def test_update_status_to_reviewed(db_session) -> None:
    ing = service.create_unreviewed(db_session, name="test ingr2")
    updated = service.update_ingredient(db_session, ing.id, status=IngredientStatus.reviewed)
    assert updated.status == IngredientStatus.reviewed


def test_update_invalid_gram_weights_key_raises_422(db_session) -> None:
    ing = service.create_unreviewed(db_session, name="test ingr3")
    with pytest.raises(ApiError) as exc_info:
        service.update_ingredient(db_session, ing.id, gram_weights={"badunit": 100.0})
    assert exc_info.value.status_code == 422


def test_update_zero_density_raises_422(db_session) -> None:
    ing = service.create_unreviewed(db_session, name="test ingr4")
    with pytest.raises(ApiError) as exc_info:
        service.update_ingredient(db_session, ing.id, density_g_per_ml=0.0)
    assert exc_info.value.status_code == 422


def test_update_negative_density_raises_422(db_session) -> None:
    ing = service.create_unreviewed(db_session, name="test ingr5")
    with pytest.raises(ApiError) as exc_info:
        service.update_ingredient(db_session, ing.id, density_g_per_ml=-1.5)
    assert exc_info.value.status_code == 422


def test_get_ingredient_missing_raises_404(db_session) -> None:
    import uuid

    with pytest.raises(ApiError) as exc_info:
        service.get_ingredient(db_session, uuid.uuid4())
    assert exc_info.value.status_code == 404
