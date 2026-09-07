import pytest

from eagle.db.engine import create_schema, create_sqlite_engine, make_session_factory


@pytest.fixture
def session_factory():
    engine = create_sqlite_engine(":memory:")
    create_schema(engine)
    return make_session_factory(engine)
