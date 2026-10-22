from fastapi.testclient import TestClient

from brilliant_research_agent.api import create_app
from brilliant_research_agent.config import Settings
from brilliant_research_agent.libraries import LIBRARIES


def test_health_libraries_languages(tmp_path):
    app = create_app(Settings(database_path=tmp_path / "t.sqlite"))
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert (tmp_path / "t.sqlite").exists()
        assert set(client.get("/api/languages").json()) == {"en", "ar", "fr", "zh", "de", "es"}
        assert client.get("/api/languages").json()["ar"]["rtl"] is True
        libs = {lib["id"]: lib for lib in client.get("/api/libraries").json()}
        assert libs["openalex"]["status"] == "available"
        assert "manual" in client.get("/api/document-types").json()


def test_only_implemented_connectors_are_available():
    available = {lib["id"] for lib in LIBRARIES if lib["status"] == "available"}
    assert available == {"openalex"}


def test_settings_repr_hides_secrets(tmp_path):
    s = Settings(database_path=tmp_path, openalex_api_key="K1", app_secret_key="K2")
    assert "K1" not in repr(s) and "K2" not in repr(s)
