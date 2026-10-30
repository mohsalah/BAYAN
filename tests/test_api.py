from fastapi.testclient import TestClient

from bayan.api import create_app
from bayan.config import Settings
from bayan.libraries import LIBRARIES


def test_health_libraries_languages(tmp_path):
    app = create_app(Settings(database_path=tmp_path / "t.sqlite"))
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert (tmp_path / "t.sqlite").exists()
        assert set(client.get("/api/languages").json()) == {"en", "ar", "fr", "zh", "de", "es"}
        assert client.get("/api/languages").json()["ar"]["rtl"] is True
        libs = {lib["id"]: lib for lib in client.get("/api/libraries").json()}
        assert libs["openalex"]["status"] == "available"
        types = client.get("/api/document-types").json()
        assert [t["id"] for t in types] == [
            "peer_reviewed", "proceedings", "thesis", "report", "other"]
        assert "not verified" in types[0]["description"]


def test_only_implemented_connectors_are_available():
    available = {lib["id"] for lib in LIBRARIES if lib["status"] == "available"}
    assert available == {"openalex", "crossref", "arxiv", "europepmc"}


def test_settings_repr_hides_secrets(tmp_path):
    s = Settings(database_path=tmp_path, openalex_api_key="K1", app_secret_key="K2")
    assert "K1" not in repr(s) and "K2" not in repr(s)


def test_connection_can_be_closed_on_another_thread(tmp_path):
    """Under uvicorn a request's connection is opened and closed on different threads."""
    import threading

    from bayan.db import connect, init_db

    init_db(tmp_path / "t.sqlite")
    conn = connect(tmp_path / "t.sqlite")
    errors = []

    def use_and_close():
        try:
            conn.execute("SELECT 1").fetchone()
            conn.close()
        except Exception as exc:  # pragma: no cover - the failure being guarded against
            errors.append(exc)

    worker = threading.Thread(target=use_and_close)
    worker.start()
    worker.join()
    assert errors == []
