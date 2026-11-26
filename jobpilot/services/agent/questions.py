"""给提问卡和决定接口共用的服务端结构与校验。"""

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from jobpilot.db.models import AgentAction
from jobpilot.services.agent.types import (
    Decision,
    DecisionIn,
    JobChoice,
    Payload,
    ToolError,
)

JOB_CHOICE_QUESTION = "已解析新岗位。要新开对话、切换当前岗位，还是保持原岗位？"


class Question(BaseModel):
    question_id: str
    text: str


class OrdinaryQuestions(BaseModel):
    type: Literal["questions"] = "questions"
    questions: list[Question]


class JobSelection(BaseModel):
    type: Literal["job_choice"] = "job_choice"
    question: str = JOB_CHOICE_QUESTION
    job_id: UUID
    choices: list[JobChoice] = Field(default_factory=lambda: list(JobChoice))


def number_questions(texts: Sequence[str]) -> OrdinaryQuestions:
    """让同一张卡反复读取时给同一问题返回相同 ID。"""
    return OrdinaryQuestions(
        questions=[
            Question(question_id=f"q{index}", text=text)
            for index, text in enumerate(texts, 1)
        ]
    )


def load_question_card(args: Payload) -> OrdinaryQuestions | JobSelection:
    if "choices" in args:
        return JobSelection.model_validate(args)
    if args.get("type") == "questions":
        return OrdinaryQuestions.model_validate(args)
    # 旧检查点已经保存的卡原来用字符串数组。读取时给它们生成稳定的 ID。
    return number_questions(args["questions"])


def validate_card_decision(body: DecisionIn, action: AgentAction) -> None:
    supplied = body.model_fields_set & {"answers", "job_choice"}
    if body.decision == Decision.REJECT or action.tool != "ask_user":
        if supplied:
            raise ToolError("写入确认或拒绝只接收决定")
        return
    card = load_question_card(action.args_json)
    if isinstance(card, JobSelection):
        if supplied != {"job_choice"} or body.job_choice not in card.choices:
            raise ToolError("请选择卡片提供的岗位操作")
        return
    expected = {question.question_id for question in card.questions}
    if (
        supplied != {"answers"}
        or body.answers is None
        or body.answers.keys() != expected
    ):
        raise ToolError("请按问题 ID 逐一回答本卡的全部问题")


def question_results(card: OrdinaryQuestions, stored: Payload) -> Payload:
    """把答案和原问题明确关联，兼容部署前已经保存的决定。"""
    answers = stored.get("answers") or {}
    if isinstance(answers, list):
        answers = {
            question.question_id: answer
            for question, answer in zip(card.questions, answers, strict=False)
        }
    return {
        "answers": {
            question.question_id: {
                "question": question.text,
                "answer": answers[question.question_id],
            }
            for question in card.questions
            if question.question_id in answers
        }
    }
