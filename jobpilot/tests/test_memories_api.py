"""通过管理接口验证记忆的修订号、用户隔离和删除后的恢复。"""

import httpx

from jobpilot.tests.test_agent_api import sign_in


async def test_memory_delete_requires_current_revision_and_can_be_restored(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    path = "/api/memories/cities"
    created = await async_client.put(
        path, json={"expected_revision": 0, "value": ["杭州", "上海", "杭州"]}
    )
    assert created.status_code == 200
    assert created.json()["value"] == ["杭州", "上海"]
    assert created.json()["revision"] == 1

    stale = await async_client.request("DELETE", path, json={"expected_revision": 0})
    assert stale.status_code == 409
    deleted = await async_client.request("DELETE", path, json={"expected_revision": 1})
    assert deleted.status_code == 200
    assert deleted.json()["value"] is None
    assert deleted.json()["revision"] == 2
    assert deleted.json()["deleted"] is True

    records = (await async_client.get("/api/memories")).json()
    assert next(item for item in records if item["key"] == "cities") == deleted.json()
    restored = await async_client.put(
        path, json={"expected_revision": 2, "value": ["苏州"]}
    )
    assert restored.status_code == 200
    assert restored.json()["revision"] == 3


async def test_memory_values_are_validated_and_scoped_to_the_signed_in_user(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    created = await async_client.put(
        "/api/memories/tone", json={"expected_revision": 0, "value": "平实"}
    )
    assert created.status_code == 200

    await sign_in(async_client, "another-memory-owner@example.invalid")
    for key, value in [
        ("unknown", "平实"),
        ("cities", ["长" * 21]),
        ("tone", "非常活泼"),
    ]:
        response = await async_client.put(
            f"/api/memories/{key}", json={"expected_revision": 0, "value": value}
        )
        assert response.status_code == 422
    records = (await async_client.get("/api/memories")).json()
    assert next(item for item in records if item["key"] == "tone")["revision"] == 0
    wrong_owner = await async_client.request(
        "DELETE", "/api/memories/tone", json={"expected_revision": 1}
    )
    assert wrong_owner.status_code == 409
