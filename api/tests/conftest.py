import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cortex_api.config import Config
from cortex_api.db import init_db
from cortex_api.main import create_app
from cortex_api.models import Base


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/cortex.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    return Config(workdir=tmp_path / "work")


@pytest.fixture
def client(session_factory, config):
    return TestClient(create_app(session_factory, config=config))
