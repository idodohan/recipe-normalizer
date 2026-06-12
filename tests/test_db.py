from sqlalchemy import text


def test_postgres_roundtrip(db_session):
    assert db_session.execute(text("select 1")).scalar() == 1
