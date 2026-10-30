from __future__ import annotations

import uuid


def test_memory_crud_lifecycle(client, auth_headers):
    idem = f"life-{uuid.uuid4()}"
    create = client.post(
        "/v1/memories",
        json={"kind": "preference", "content": "likes stoic framing"},
        headers={**auth_headers, "Idempotency-Key": idem},
    )
    assert create.status_code == 201
    mid = create.json()["id"]
    etag = create.json()["etag"]

    dup = client.post(
        "/v1/memories",
        json={"kind": "preference", "content": "likes stoic framing"},
        headers={**auth_headers, "Idempotency-Key": idem},
    )
    assert dup.status_code == 201
    assert dup.json()["id"] == mid

    got = client.get(f"/v1/memories/{mid}", headers=auth_headers)
    assert got.status_code == 200

    corrected = client.post(
        f"/v1/memories/{mid}:correct",
        json={"content": "likes concise stoic framing"},
        headers={**auth_headers, "If-Match": etag},
    )
    assert corrected.status_code == 200
    assert corrected.json()["current_version"]["version"] == 2

    search = client.post(
        "/v1/memories/search",
        json={"query": "concise", "limit": 5},
        headers=auth_headers,
    )
    assert search.status_code == 200
    assert any(i["id"] == mid for i in search.json()["items"])

    forgot = client.post(f"/v1/memories/{mid}:forget", headers=auth_headers)
    assert forgot.status_code == 200

    hidden = client.get(f"/v1/memories/{mid}", headers=auth_headers)
    assert hidden.status_code == 404

    deleted = client.delete(f"/v1/memories/{mid}", headers=auth_headers)
    assert deleted.status_code == 204

    recreate = client.post(
        "/v1/memories",
        json={"kind": "preference", "content": "likes concise stoic framing"},
        headers=auth_headers,
    )
    assert recreate.status_code == 422


def test_health_ready(client):
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["checks"]["database"] is True


def test_forbidden_without_token(client):
    r = client.post("/v1/memories", json={"content": "x"})
    assert r.status_code == 403
