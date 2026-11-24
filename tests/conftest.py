import os
import tempfile

# Configure before the app package is imported anywhere.
_tmp = tempfile.mkdtemp(prefix="ff-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ.pop("SECRET_KEY", None)
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["SOURCE"] = "demo"
os.environ["INVITE_CODE"] = "letmein"

import pytest  # noqa: E402

from app.db import Base, engine, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()
    yield
    Base.metadata.drop_all(engine)
