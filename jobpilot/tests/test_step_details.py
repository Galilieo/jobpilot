"""工具步骤的界面细节：按工具取出岗位名、版本和条数，出错没有细节，回传模型前去掉。"""

from jobpilot.services.agent.context import summarize_tool
from jobpilot.services.agent.step_details import model_view, step_detail


def test_read_job_and_list_jobs_keep_title_company_location() -> None:
    fields = {
        "title": "Java 后端",
        "company": "港桥",
        "location": "上海",
        "salary": "200/天",
    }
    assert step_detail("read_job", {"parsed": {"basic_fields": fields}}) == {
        "title": "Java 后端",
        "company": "港桥",
        "location": "上海",
    }
    listed = step_detail("list_jobs", {"jobs": [{"job_id": "j1", "fields": fields}]})
    assert listed == {
        "jobs": [
            {
                "job_id": "j1",
                "title": "Java 后端",
                "company": "港桥",
                "location": "上海",
            }
        ]
    }


def test_facts_and_report_are_counted_not_copied() -> None:
    facts = {"version": 3, "facts": [{}, {}], "criteria": [{}], "education": []}
    assert step_detail("read_facts", facts) == {
        "version": 3,
        "facts": 2,
        "criteria": 1,
        "education": 0,
    }
    report = {"items": [{}, {}, {}], "main_gaps": ["甲", "乙", "丙", "丁"]}
    assert step_detail("read_match_report", report) == {
        "items": 3,
        "main_gaps": ["甲", "乙", "丙"],
    }


def test_long_job_text_is_cut_and_errors_have_no_detail() -> None:
    long_title = "很长的岗位名" * 20
    detail = step_detail(
        "read_job", {"parsed": {"basic_fields": {"title": long_title}}}
    )
    assert detail is not None and len(detail["title"]) == 60
    assert step_detail("read_job", {"error": "岗位不存在"}) is None
    assert step_detail("match_job", {"report_id": "r1"}) is None


def test_model_view_drops_detail_only() -> None:
    step = summarize_tool(
        "read_facts", {"fact_set_id": "f1", "version": 3, "facts": []}
    )
    assert step["detail"]["version"] == 3
    assert model_view(step) == {"tool": "read_facts", "fact_set_id": "f1"}


def test_history_tool_returns_steps_without_detail() -> None:
    from jobpilot.db.models import Message, MessageRole
    from jobpilot.services.agent.history import history_parts

    step = summarize_tool("read_job", {"job_id": "j1", "parsed": {"basic_fields": {}}})
    message = Message(role=MessageRole.TOOL, content="", steps_json=[step])
    [part] = history_parts(message)
    assert "detail" in step
    assert "detail" not in part.text
