import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,                      # never read the real .env in tests
        db_path=str(tmp_path / "test.sqlite"),
        validation_record_path=str(tmp_path / "validation.json"),
        sigma_min_samples=10,
    )
