"""The public privacy page Google requires before publishing the Gmail sign-in app."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
def test_the_privacy_page_is_public():
    """Google won't publish the sign-in app without a privacy policy link."""
    import os
    os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
    os.environ.setdefault("SUPABASE_KEY", "fake")
    os.environ.setdefault("JWT_SECRET", "test-secret")
    sys.path.insert(0, str(ROOT / "web"))
    import app as APP
    from fastapi.testclient import TestClient
    r = TestClient(APP.app).get("/privacy")
    assert r.status_code == 200 and "read-only" in r.text and "Limited Use" in r.text
