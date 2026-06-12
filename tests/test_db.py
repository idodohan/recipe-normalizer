from sqlalchemy import text


def test_postgres_roundtrip(db_session):
    assert db_session.execute(text("select 1")).scalar() == 1


def test_commit_does_not_leak_part1_insert(db_session):
    db_session.execute(text("insert into _scratch (id) values (1)"))
    db_session.commit()
    assert db_session.execute(text("select count(*) from _scratch")).scalar() == 1


def test_commit_does_not_leak_part2_isolated(db_session):
    # Runs after part1 (file order); the committed row must not be visible here.
    assert db_session.execute(text("select count(*) from _scratch")).scalar() == 0
