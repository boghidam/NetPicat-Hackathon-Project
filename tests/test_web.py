"""Integration tests for the Noodle Traffic Control FastAPI + WebSocket backend."""

from __future__ import annotations

from fastapi.testclient import TestClient

from smartcity.web import create_app


def test_web_api_and_websocket_endpoints(tmp_path, monkeypatch):
    import smartcity.providers.credentials as cred_mod
    import smartcity.slots as slots_mod

    monkeypatch.setattr(slots_mod, "DEFAULT_SLOTS_DIR", tmp_path / "slots")
    monkeypatch.setattr(cred_mod, "CREDENTIALS_PATH", tmp_path / "local_credentials.json")

    app = create_app(slot_name="default")
    with TestClient(app) as client:
        # 1. Static UI index & health
        r_idx = client.get("/")
        assert r_idx.status_code == 200 and "Noodle" in r_idx.text and "Aether (Simulation)" in r_idx.text
        assert client.get("/api/health").json()["ok"] is True

        # 2. Full 51-line Iași map payload
        mp = client.get("/api/map").json()
        assert len(mp["routes"]) == 51
        assert len(mp["landmarks"]) == 8

        # 3. Named slot creation & reset without deleting other slots
        s0 = client.get("/api/slots").json()
        assert s0["active_slot"] == "default"
        s1 = client.post("/api/slots/select", json={"slot": "investor_slot", "clean": True}).json()
        assert s1["active_slot"] == "investor_slot"
        names = {s["name"] for s in s1["slots"]}
        assert {"default", "investor_slot"} <= names
        s2 = client.post("/api/slots/reset", json={"slot": "investor_slot"}).json()
        assert {"default", "investor_slot"} <= {s["name"] for s in s2["slots"]}

        # 4. Credentials masked storage (without hitting external internet)
        r_cred = client.post(
            "/api/credentials",
            json={"api_key": "secret-demo-1234", "agency_id": "1", "test": False},
        ).json()
        assert r_cred["ok"] is True
        g_cred = client.get("/api/credentials").json()
        assert g_cred["has_key"] is True and g_cred["masked_key"].endswith("1234") and "secret" not in g_cred["masked_key"]

        # 5. Mode switching Aether <-> Terra
        assert client.post("/api/mode", json={"mode": "terra"}).json()["mode"] == "terra"
        assert client.post("/api/mode", json={"mode": "aether"}).json()["mode"] == "aether"

        # 6. WebSocket frame streaming & live commands
        with client.websocket_connect("/ws") as ws:
            frame = ws.receive_json()
            assert frame["mode"] == "aether"
            assert frame["kpis"]["total_routes"] == 51
            assert 0.0 <= frame["kpis"]["predicted_flow"] <= 1.0
            assert "passenger_hours_saved_per_rider" in frame["kpis"]
            assert "passenger_mins_saved_per_rider" in frame["kpis"]
            assert frame["kpis"]["cars_count"] >= 1000
            assert "buses_driving" in frame["kpis"] and "buses_layover" in frame["kpis"]
            ws.send_json({"cmd": "set_adaptive", "adaptive": False})
            # Drain any already-queued 20 Hz frame before the command took effect
            got_false = any(ws.receive_json()["adaptive"] is False for _ in range(5))
            assert got_false
