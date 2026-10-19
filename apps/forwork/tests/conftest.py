import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forwork.db import Database  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "data" / "forwork.db")
    yield d
    d.close()
