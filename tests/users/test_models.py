import uuid

from recipe_normalizer.users.models import Session as DbSession
from recipe_normalizer.users.models import User


def test_user_and_session_roundtrip(db_session):
    user = User(email="a@b.c", password_hash="x", display_name="A")
    db_session.add(user)
    db_session.flush()
    sess = DbSession(user_id=user.id, token_hash="t" * 64)
    db_session.add(sess)
    db_session.flush()
    assert isinstance(user.id, uuid.UUID)
    assert sess.user_id == user.id
