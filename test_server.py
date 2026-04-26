from unittest.mock import patch

from fastapi.testclient import TestClient

import server

client = TestClient(server.app)


def test_missing_groq_api_key():
    with patch("server.client", None):
        response = client.post("/api/chat", json={"text": "Привет"})

        assert response.status_code == 200
        assert response.json() == {"reply": server.SERVICE_UNAVAILABLE_REPLY}


def test_empty_message_after_strip_is_rejected():
    response = client.post("/api/chat", json={"text": "   "})

    assert response.status_code == 422
