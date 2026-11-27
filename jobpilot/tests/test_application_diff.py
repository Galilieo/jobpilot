"""验证投递的事实差异与原简历删除后的快照状态。"""

from uuid import UUID

import httpx
import pytest
from sqlalchemy.orm.attributes import flag_modified

from jobpilot.db.models import Fact, FactSet, ResumeVersion
from jobpilot.tests.test_applications_api import presented_resume as presented_resume
from jobpilot.tests.test_applications_api import save_application
from jobpilot.tests.test_tailoring_graph import Factory


@pytest.mark.parametrize("pending", [False, True])
async def test_diff_only_warns_about_sent_bullets_when_responsibility_changes(
    async_client: httpx.AsyncClient,
    presented_resume: tuple[UUID, str, dict],
    session_factory: Factory,
    pending: bool,
) -> None:
    user_id, job_id, version = presented_resume
    async with session_factory.begin() as session:
        row = await session.get_one(ResumeVersion, UUID(version["version_id"]))
        content = row.content_json
        bullet = content["sections"][0]["entries"][0]["bullets"][0]
        if pending:
            bullet["origin"] = "pending"
            row.content_json = {**content}
            flag_modified(row, "content_json")
        before = await session.get_one(Fact, UUID(bullet["fact_ids"][0]))
        old_set = await session.get_one(FactSet, before.fact_set_id)
        latest = FactSet(user_id=user_id, version=old_set.version + 1)
        session.add(latest)
        await session.flush()
        responsibility = (
            "participate" if before.responsibility != "participate" else "own"
        )
        session.add(
            Fact(
                user_id=user_id,
                fact_set_id=latest.fact_set_id,
                lineage_id=before.lineage_id,
                text="更新了未投出的事实" if pending else before.text,
                kind=before.kind,
                responsibility=responsibility,
                experience=before.experience,
                has_number=before.has_number,
                source="user",
            )
        )

    application = await save_application(async_client, job_id, version["version_id"])
    path = f"/api/applications/{application['application_id']}"
    snapshot = await async_client.get(f"{path}/snapshot.html")
    assert (bullet["text"] in snapshot.text) != pending
    compared = (await async_client.get(f"{path}/diff")).json()
    change = next(
        change
        for change in compared["fact_changes"]
        if change["item_id"] == str(before.lineage_id)
    )
    assert change["before"] == before.text
    assert change["after"] == ("更新了未投出的事实" if pending else before.text)
    assert change["before_responsibility"] == before.responsibility
    assert change["after_responsibility"] == responsibility
    if pending:
        assert compared["risks"] == []
    else:
        [risk] = compared["risks"]
        assert (
            risk["reason"] == "fact_changed"
            and risk["bullet_id"] == bullet["bullet_id"]
        )


async def test_diff_marks_changed_fact_and_deleted_source(
    async_client: httpx.AsyncClient,
    presented_resume: tuple[UUID, str, dict],
    session_factory: Factory,
) -> None:
    user_id, job_id, version = presented_resume
    application = await save_application(async_client, job_id, version["version_id"])
    path = f"/api/applications/{application['application_id']}/diff"
    async with session_factory.begin() as session:
        old_set = await session.get(FactSet, UUID(application["fact_set_id"]))
        lineage = UUID(
            version["content"]["sections"][0]["entries"][0]["bullets"][0]["fact_ids"][0]
        )
        before = await session.get(Fact, lineage)
        latest = FactSet(user_id=user_id, version=old_set.version + 1)
        session.add(latest)
        await session.flush()
        session.add(
            Fact(
                user_id=user_id,
                fact_set_id=latest.fact_set_id,
                lineage_id=before.lineage_id,
                text="修正为 20%",
                kind=before.kind,
                responsibility=before.responsibility,
                experience=before.experience,
                has_number=True,
                source="user",
            )
        )
    response = await async_client.get(path)
    assert response.is_success, response.text
    risk = response.json()["risks"][0]
    assert risk["reason"] == "fact_changed"
    assert risk["facts"][0]["before"] == before.text
    assert risk["facts"][0]["after"] == "修正为 20%"
    assert (await async_client.delete(f"/api/jobs/{job_id}")).is_success
    assert (await async_client.get(path)).json()["source_deleted"] is True
