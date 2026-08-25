import pytest

from src.ibp_client import IBPKeyFigureClient


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


def _client():
    return IBPKeyFigureClient("tenant.example.com", "user", "pass", "SMUPILOTO")


def test_connection_discovers_pa_via_service_document(monkeypatch):
    client = _client()

    def fake_get(url, **kwargs):
        assert url.endswith("/PLANNING_DATA_API_SRV/")
        return _FakeResponse(200, {
            "d": {"EntitySets": ["SMUPILOTO", "SMUPILOTOTrans", "SMUPILOTOMessage", "OtherThing"]}
        })

    monkeypatch.setattr(client._session, "get", fake_get)
    result = client.test_connection()

    assert result["ok"] is True
    assert result["service"] == "SAP_COM_0720"
    assert result["planning_areas"] == ["SMUPILOTO"]


def test_connection_falls_back_to_0143_and_reports_0720_error(monkeypatch):
    client = _client()
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "PLANNING_DATA_API_SRV" in url:
            return _FakeResponse(404, text="Resource not found for the segment 'PlanningAreaSet'.")
        return _FakeResponse(200, {"d": {"EntitySets": ["SMUPILOTO", "SMUPILOTOTrans"]}})

    monkeypatch.setattr(client._session, "get", fake_get)
    result = client.test_connection()

    assert result["ok"] is True
    assert result["service"] == "SAP_COM_0143"
    assert result["planning_areas"] == ["SMUPILOTO"]
    assert "404" in result["svc0720_error"]
    assert len(calls) == 2


def test_connection_reports_failure_when_both_services_fail(monkeypatch):
    client = _client()

    def fake_get(url, **kwargs):
        return _FakeResponse(500, text="boom")

    monkeypatch.setattr(client._session, "get", fake_get)
    result = client.test_connection()

    assert result["ok"] is False
    assert result["service"] is None
