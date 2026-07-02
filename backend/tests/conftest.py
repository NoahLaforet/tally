"""Test bootstrap: isolate the database and neutralize real config.

Environment variables are set before the app is imported so the module-level
Settings singleton and engine bind to a throwaway directory, never to a real
database or real Plaid keys (env vars take precedence over .env files).
"""

from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="tally-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["SESSION_SECRET"] = "test-secret-0123456789abcdef0123456789abcdef"
os.environ["PLAID_CLIENT_ID"] = ""
os.environ["PLAID_SECRET"] = ""
os.environ["PLAID_ENV"] = "sandbox"
os.environ["EXTRA_ORIGINS"] = ""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_on():
    """Flip auth on for one test; middleware and deps read settings live."""
    settings.AUTH_ENABLED = True
    try:
        yield
    finally:
        settings.AUTH_ENABLED = False
