"""验证记忆接口、账号行锁与写作偏好的修订号。"""

import asyncio

import httpx

from jobpilot.tests.test_agent_api import sign_in


async def test_manual_entries_deduplicate_by_category_and_trim(
    async_client: httpx.AsyncClient,
):
    await sign_in(async_client)
    data = {"category": "job_pref", "content": "  不考虑外包  "}
    first = (await async_client.post("/api/memories", json=data)).json()
    duplicate = (await async_client.post("/api/memories", json=data)).json()
    assert first["status"] == "saved"
    assert duplicate == {"status": "exists", "entry": first["entry"]}
    assert first["entry"]["content"] == "不考虑外包"
    assert first["entry"]["source"] == {"kind": "manual"}
    await async_client.post("/api/memories", json={**data, "category": "agent_rule"})
    current = (await async_client.get("/api/memories")).json()
    assert current["used_chars"] == 10
    assert current["budget_chars"] == 1500
    assert len(current["entries"]) == 2
    path = f"/api/memories/{first['entry']['memory_id']}"
    updated = await async_client.put(
        path, json={**data, "content": "只考虑自研", "expected_revision": 1}
    )
    assert updated.json()["entry"]["revision"] == 2
    assert (
        await async_client.request("DELETE", path, json={"expected_revision": 1})
    ).status_code == 409
    assert (
        await async_client.request("DELETE", path, json={"expected_revision": 2})
    ).status_code == 204
    assert (await async_client.get("/api/memories")).json()["used_chars"] == 5
    archive = (await async_client.get("/api/account/export")).json()
    assert archive["format_version"] == 2
    assert len(archive["memory"]) == 2
    assert any(item["deleted_at"] for item in archive["memory"])


async def test_concurrent_manual_adds_cannot_exceed_character_budget(
    async_client: httpx.AsyncClient,
):
    await sign_in(async_client)
    for index in range(7):
        saved = await async_client.post(
            "/api/memories", json={"category": "job_pref", "content": f"{index}" * 200}
        )
        assert saved.is_success
    results = await asyncio.gather(
        *(
            async_client.post(
                "/api/memories", json={"category": "agent_rule", "content": char * 100}
            )
            for char in ("甲", "乙")
        )
    )
    assert sorted(result.status_code for result in results) == [200, 409]
    denied = next(
        result.json()["detail"] for result in results if not result.is_success
    )
    assert len(denied["entries"]) == 8
    assert (await async_client.get("/api/memories")).json()["used_chars"] == 1500


async def test_writing_preferences_validate_patch_and_default(
    async_client: httpx.AsyncClient,
):
    await sign_in(async_client)
    path = "/api/writing-preference"
    initial = (await async_client.get(path)).json()
    assert initial["revision"] == 0
    assert initial["options"]["tone"] == ["平实", "简洁有力"]
    assert set(initial["values"].values()) == {None}
    assert (
        await async_client.put(path, json={"expected_revision": 0, "tone": "非法"})
    ).status_code == 422

    response = await async_client.put(
        path, json={"expected_revision": 0, "tone": "平实", "tech_terms": "英文原名"}
    )
    assert response.json()["revision"] == 1
    assert (
        await async_client.put(path, json={"expected_revision": 0, "tone": None})
    ).status_code == 409
    response = await async_client.put(path, json={"expected_revision": 1, "tone": None})
    assert response.json()["values"] == {
        "tone": None,
        "tech_terms": "英文原名",
        "bullet_length": None,
    }
    assert response.json()["revision"] == 2
    archive = (await async_client.get("/api/account/export")).json()
    assert archive["writing_preference"][0]["tech_terms"] == "英文原名"
