"""Understanding, Planning, Context Building and Verification engines.

These four are the front half of the brain: what did the user ask, what steps
would satisfy it, what may enter the prompt, and did the result actually happen.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.core.safety import scan_for_harmful_request
from app.models.base import (
    CancelToken,
    ChatMessage,
    Complexity,
    GenerationRequest,
    ModelAdapter,
    RiskLevel,
    TaskType,
)

logger = logging.getLogger("nyxora.engines")

SLASH_COMMANDS: dict[str, TaskType] = {
    "/plan": TaskType.RESEARCH,
    "/summarize": TaskType.DOCUMENT,
    "/scaffold": TaskType.PROJECT_SCAFFOLD,
    "/remember": TaskType.MEMORY_REQUEST,
    "/research": TaskType.RESEARCH,
    "/files": TaskType.FILE_TASK,
}

_CODING_RE = re.compile(
    r"```|\bdef \w+\(|\bclass \w+|function \w+\(|\bimport \w+|traceback|pytest"
    r"|refactor|bug\b|typescript|\bfn \w+\(",
    re.IGNORECASE,
)
_FILE_RE = re.compile(
    r"\b(read|list|create|edit|write|delete|rename)\b.{0,40}\b(file|folder|directory|\.py|\.md|\.txt)\b",
    re.IGNORECASE,
)
_SCAFFOLD_RE = re.compile(
    r"\b(scaffold|bootstrap|set up|create)\b.{0,40}\b(project|app|dashboard|api)\b", re.IGNORECASE
)
_DOC_RE = re.compile(
    r"\b(summarize|document|pdf|from my (docs|files)|according to (my|the) document)", re.IGNORECASE
)
_MEMORY_RE = re.compile(
    r"\b(remember|forget|my preference|i prefer|don't use this)\b", re.IGNORECASE
)


class TaskUnderstanding(BaseModel):
    task_type: TaskType = TaskType.CHAT
    complexity: Complexity = Complexity.SIMPLE
    needs_tools: list[str] = Field(default_factory=list)
    needs_documents: bool = False
    needs_memory: bool = True
    needs_vision: bool = False
    risk_level: RiskLevel = RiskLevel.NONE
    ambiguities: list[str] = Field(default_factory=list)
    confidence: float = 0.6
    method: Literal["rules", "model", "fallback"] = "rules"
    should_clarify: bool = False


class PlanStep(BaseModel):
    id: str
    description: str
    tool: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    required_permission: str = "read"
    completion_criteria: list[str] = Field(default_factory=list)
    max_retries: int = 1


class Plan(BaseModel):
    goal: str
    steps: list[PlanStep] = Field(default_factory=list)
    needs_approval: bool = False
    generated_by: Literal["rules", "model", "fallback"] = "rules"

    @property
    def max_risk_permission(self) -> str:
        order = {"read": 0, "write": 1, "delete": 2, "external": 2, "forbidden": 3}
        return max(
            (s.required_permission for s in self.steps),
            key=lambda p: order.get(p, 0),
            default="read",
        )


class UnderstandingEngine:
    """Rules and slash-commands first; optional small-model classification second."""

    def __init__(self, adapter: ModelAdapter | None = None, model: str | None = None) -> None:
        self.adapter = adapter
        self.model = model

    def understand(
        self,
        text: str,
        *,
        has_attachments: bool = False,
        has_images: bool = False,
        workspace_available: bool = True,
    ) -> TaskUnderstanding:
        stripped = (text or "").strip()
        result = TaskUnderstanding()

        cmd = stripped.split(maxsplit=1)[0].lower() if stripped else ""
        if cmd in SLASH_COMMANDS:
            result.task_type = SLASH_COMMANDS[cmd]
            result.method = "rules"
            result.confidence = 0.95
        else:
            result.task_type = self._classify_rules(stripped, has_attachments)
            result.confidence = 0.7 if result.task_type is not TaskType.UNKNOWN else 0.4

        result.needs_vision = has_images
        result.needs_documents = (
            result.task_type in {TaskType.DOCUMENT, TaskType.RESEARCH} or has_attachments
        )
        result.needs_memory = True

        if result.task_type in {TaskType.FILE_TASK, TaskType.PROJECT_SCAFFOLD, TaskType.CODING}:
            result.needs_tools = self._tools_for(result.task_type)
            result.risk_level = RiskLevel.MEDIUM if result.needs_tools else RiskLevel.LOW
            if result.task_type is TaskType.PROJECT_SCAFFOLD:
                result.risk_level = RiskLevel.MEDIUM
        if result.task_type is TaskType.MEMORY_REQUEST:
            result.risk_level = RiskLevel.LOW

        length = len(stripped)
        if length > 1200 or result.task_type in {TaskType.RESEARCH, TaskType.PROJECT_SCAFFOLD}:
            result.complexity = Complexity.COMPLEX
        elif result.needs_tools or length > 320:
            result.complexity = Complexity.MODERATE
        else:
            result.complexity = Complexity.SIMPLE

        if result.task_type is TaskType.PROJECT_SCAFFOLD and not re.search(
            r"\b(python|react|node|dashboard|api|app)\b", stripped, re.IGNORECASE
        ):
            result.ambiguities.append("which project template to use")
        if result.task_type is TaskType.FILE_TASK and not _FILE_RE.search(stripped):
            result.ambiguities.append("which file is targeted")

        harmful = scan_for_harmful_request(stripped)
        if harmful.verdict.value == "refuse":
            result.risk_level = RiskLevel.HIGH
            result.ambiguities.append("request matches refusal policy")

        result.should_clarify = bool(
            result.ambiguities and result.risk_level in {RiskLevel.MEDIUM, RiskLevel.HIGH}
        )
        return result

    def _classify_rules(self, text: str, has_attachments: bool) -> TaskType:
        if not text:
            return TaskType.UNKNOWN
        if _MEMORY_RE.search(text):
            return TaskType.MEMORY_REQUEST
        if _SCAFFOLD_RE.search(text):
            return TaskType.PROJECT_SCAFFOLD
        if _CODING_RE.search(text):
            return TaskType.CODING
        if _FILE_RE.search(text):
            return TaskType.FILE_TASK
        if has_attachments or _DOC_RE.search(text):
            return TaskType.DOCUMENT
        if re.search(
            r"\b(research|compare|analyze|investigate|why|how does)\b", text, re.IGNORECASE
        ):
            return TaskType.RESEARCH
        return TaskType.CHAT

    def _tools_for(self, task_type: TaskType) -> list[str]:
        if task_type is TaskType.PROJECT_SCAFFOLD:
            return ["project.scaffold"]
        if task_type is TaskType.FILE_TASK:
            return ["fs.list", "fs.read"]
        if task_type is TaskType.CODING:
            return ["fs.read", "fs.edit"]
        return []

    async def classify_with_model(self, text: str, cancel: CancelToken) -> TaskUnderstanding | None:
        """Optional second layer. Never trusted; validated and falls back to rules."""
        if self.adapter is None or not self.model:
            return None
        schema_hint = json.dumps(
            {
                "task_type": [t.value for t in TaskType],
                "complexity": ["simple", "moderate", "complex"],
                "confidence": 0.9,
            }
        )
        req = GenerationRequest(
            model=self.model,
            messages=[
                ChatMessage(
                    role="user",
                    content=(
                        f"Classify this request as JSON. Schema: {schema_hint}"
                        f"\n\nRequest: {text[:2000]}"
                    ),
                )
            ],
            system="You classify user requests. Reply with JSON only.",
            response_format="json",
            temperature=0.0,
        )
        try:
            result = await self.adapter.generate(req, cancel)
            data = json.loads(repair_json(result.text))
            return TaskUnderstanding(
                task_type=TaskType(data.get("task_type", "unknown")),
                complexity=Complexity(data.get("complexity", "simple")),
                confidence=float(data.get("confidence", 0.5)),
                method="model",
            )
        except (ValidationError, ValueError, RuntimeError, json.JSONDecodeError):
            logger.info("model_classification_fell_back")
            return None


def repair_json(raw: str) -> str:
    """Trim fences and stray prose around a JSON object. Two-pass, then give up."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


PLANNING_SCHEMA = json.dumps(
    {
        "goal": "string",
        "steps": [
            {
                "id": "s1",
                "description": "string",
                "tool": (
                    "one of: fs.list fs.read fs.mkdir fs.create fs.edit fs.delete"
                    " project.scaffold | null"
                ),
                "params": {},
                "depends_on": [],
                "required_permission": "read | write | delete | external",
                "completion_criteria": ["string"],
            }
        ],
    }
)

ALLOWED_PLAN_TOOLS = {
    "fs.list",
    "fs.read",
    "fs.mkdir",
    "fs.create",
    "fs.edit",
    "fs.delete",
    "project.scaffold",
}


class PlanningEngine:
    def __init__(self, adapter: ModelAdapter | None = None, model: str | None = None) -> None:
        self.adapter = adapter
        self.model = model

    def rule_plan(self, understanding: TaskUnderstanding, goal: str) -> Plan:
        """Deterministic plans for the shapes V1 actually supports."""
        if understanding.task_type is TaskType.PROJECT_SCAFFOLD:
            return Plan(
                goal=goal,
                generated_by="rules",
                needs_approval=True,
                steps=[
                    PlanStep(
                        id="s1",
                        description="Inspect available templates",
                        tool="fs.list",
                        params={"path": "."},
                        required_permission="read",
                    ),
                    PlanStep(
                        id="s2",
                        description="Scaffold the project",
                        tool="project.scaffold",
                        params={
                            "template": "python_dashboard",
                            "project_name": _project_name(goal),
                        },
                        required_permission="write",
                        depends_on=["s1"],
                        completion_criteria=["all scaffolded files exist", "no file is empty"],
                    ),
                    PlanStep(
                        id="s3",
                        description="Verify the result",
                        tool="fs.list",
                        params={"path": _project_name(goal)},
                        required_permission="read",
                        depends_on=["s2"],
                        completion_criteria=["directory listing is non-empty"],
                    ),
                ],
            )
        if understanding.task_type is TaskType.FILE_TASK:
            return Plan(
                goal=goal,
                generated_by="rules",
                steps=[
                    PlanStep(
                        id="s1",
                        description="List the workspace root",
                        tool="fs.list",
                        params={"path": "."},
                        required_permission="read",
                    )
                ],
            )
        return Plan(
            goal=goal,
            generated_by="fallback",
            steps=[PlanStep(id="s1", description="Answer directly")],
        )

    async def plan(self, understanding: TaskUnderstanding, goal: str, cancel: CancelToken) -> Plan:
        fallback = self.rule_plan(understanding, goal)
        if understanding.complexity is Complexity.SIMPLE or self.adapter is None or not self.model:
            return fallback
        req = GenerationRequest(
            model=self.model,
            messages=[
                ChatMessage(
                    role="user",
                    content=f"Goal: {goal}\nTask type: {understanding.task_type}\n"
                    f"Produce a JSON plan against this schema:\n{PLANNING_SCHEMA}",
                )
            ],
            system="You are a planning engine. Output only JSON matching the schema. "
            "Every consequential step must declare required_permission.",
            response_format="json",
            temperature=0.0,
        )
        for attempt in range(2):  # one automatic repair retry
            try:
                result = await self.adapter.generate(req, cancel)
                data = json.loads(repair_json(result.text))
                plan = self._validate(data, goal)
                if plan is not None:
                    return plan
            except (ValidationError, ValueError, json.JSONDecodeError, RuntimeError):
                logger.info("plan_repair_attempt", extra={"attempt": attempt})
                req.messages[-1] = ChatMessage(
                    role="user",
                    content=req.messages[-1].content
                    + "\n\nPrevious output was invalid. Return strict JSON only.",
                )
        return fallback

    def _validate(self, data: dict[str, Any], goal: str) -> Plan | None:
        steps_raw = data.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            return None
        steps: list[PlanStep] = []
        for i, entry in enumerate(steps_raw[:12]):
            if not isinstance(entry, dict):
                return None
            tool = entry.get("tool")
            if tool is not None and tool not in ALLOWED_PLAN_TOOLS:
                return None  # a plan naming an unavailable tool is not trusted
            permission = str(entry.get("required_permission", "read"))
            if permission not in {"read", "write", "delete", "external", "forbidden"}:
                return None
            params = entry.get("params") if isinstance(entry.get("params"), dict) else {}
            steps.append(
                PlanStep(
                    id=str(entry.get("id") or f"s{i + 1}"),
                    description=str(entry.get("description", ""))[:300],
                    tool=tool,
                    params=params,
                    depends_on=[
                        str(d)
                        for d in entry.get("depends_on", [])
                        if isinstance(entry.get("depends_on"), list)
                    ],
                    required_permission=permission,
                    completion_criteria=[str(c) for c in entry.get("completion_criteria", [])][:5],
                )
            )
        if not steps:
            return None
        needs_approval = any(
            s.required_permission in {"write", "delete", "external"} for s in steps
        )
        return Plan(
            goal=str(data.get("goal", goal))[:300],
            steps=steps,
            needs_approval=needs_approval,
            generated_by="model",
        )


def _project_name(goal: str) -> str:
    match = re.search(
        r"[\"'`]?([A-Za-z0-9_\-]{3,30})[\"'`]?\s+(project|app|dashboard)", goal, re.IGNORECASE
    )
    if match:
        return match.group(1).lower()
    return (
        "".join(
            c if c.isalnum() else "_" for c in goal.lower().split()[0] if c.isalnum() or c in " -_"
        )[:24]
        or "project"
    )


class ContextSection(BaseModel):
    name: str
    content: str
    tokens: int
    priority: int


class ContextBudget(BaseModel):
    total_output_reserved: int = 1024
    system_pct: float = 0.30
    memory_pct: float = 0.15
    documents_pct: float = 0.25
    history_pct: float = 0.20
    tools_pct: float = 0.10


class BuiltContext(BaseModel):
    prompt: str
    sections: list[ContextSection] = Field(default_factory=list)
    included: list[str] = Field(default_factory=list)
    truncated: list[str] = Field(default_factory=list)
    estimated_tokens: int = 0


class ContextBuilder:
    """Assembles the prompt within a token budget and records what it kept."""

    def __init__(self, system_prompt: str, budget: ContextBudget | None = None) -> None:
        self.system_prompt = system_prompt
        self.budget = budget or ContextBudget()

    @staticmethod
    def _tokens(text: str) -> int:
        return max(1, len(text) // 4)

    def build(
        self,
        *,
        window: int,
        task_instructions: str = "",
        memory_items: list[str] | None = None,
        document_chunks: list[str] | None = None,
        history: list[str] | None = None,
        tool_results: list[str] | None = None,
        user_message: str = "",
    ) -> BuiltContext:
        alloc = {
            "system": int(window * self.budget.system_pct),
            "task": int(window * 0.05),
            "memory": int(window * self.budget.memory_pct),
            "documents": int(window * self.budget.documents_pct),
            "history": int(window * self.budget.history_pct),
            "tools": int(window * self.budget.tools_pct),
        }
        sections: list[ContextSection] = []
        truncated: list[str] = []
        included: list[str] = []

        def add(name: str, text: str, cap_tokens: int, priority: int) -> None:
            if not text:
                return
            est = self._tokens(text)
            if est > cap_tokens:
                # truncate lowest priority first, by clipping the tail
                text = text[: cap_tokens * 4]
                est = cap_tokens
                truncated.append(name)
            else:
                included.append(name)
            sections.append(ContextSection(name=name, content=text, tokens=est, priority=priority))

        add("system", self.system_prompt, alloc["system"], 0)
        add("task", task_instructions, alloc["task"], 1)
        add("memory", "\n".join(f"- {m}" for m in (memory_items or [])), alloc["memory"], 2)
        add("documents", "\n\n".join(document_chunks or []), alloc["documents"], 2)
        add("history", "\n".join(history or []), alloc["history"], 3)
        add("tools", "\n\n".join(tool_results or []), alloc["tools"], 1)

        sections.sort(key=lambda s: s.priority)
        body = "\n\n".join(f"## {s.name}\n{s.content}" for s in sections if s.content)
        prompt = f"{body}\n\n## user\n{user_message}" if body else user_message
        return BuiltContext(
            prompt=prompt,
            sections=sections,
            included=included,
            truncated=truncated,
            estimated_tokens=sum(s.tokens for s in sections) + self._tokens(user_message),
        )


class VerificationResult(BaseModel):
    ok: bool
    checks: list[dict[str, Any]] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)


class VerificationEngine:
    """Deterministic post-condition checks. A run is never 'done' if these fail."""

    def check_file_exists(self, path: Path) -> dict[str, Any]:
        return {"check": "file_exists", "target": str(path), "ok": Path(path).is_file()}

    def check_non_empty(self, path: Path) -> dict[str, Any]:
        p = Path(path)
        return {
            "check": "content_non_empty",
            "target": str(p),
            "ok": p.is_file() and p.stat().st_size > 0,
        }

    def check_json_valid(self, text: str) -> dict[str, Any]:
        try:
            json.loads(text)
            return {"check": "json_valid", "ok": True}
        except (json.JSONDecodeError, TypeError):
            return {"check": "json_valid", "ok": False}

    def check_diff_applied(self, path: Path, expected: str) -> dict[str, Any]:
        p = Path(path)
        actual = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        return {"check": "diff_applied", "target": str(p), "ok": actual == expected}

    def check_cites_sources(self, answer: str, sources: list[str]) -> dict[str, Any]:
        if not sources:
            return {"check": "cites_sources", "ok": True, "note": "no sources required"}
        cited = any(f"[{i + 1}]" in answer for i in range(len(sources))) or any(
            s.split("/")[-1] in answer for s in sources if s
        )
        return {"check": "cites_sources", "ok": cited, "sources": len(sources)}

    def aggregate(self, checks: list[dict[str, Any]]) -> VerificationResult:
        failed = [str(c.get("check", "?")) for c in checks if not c.get("ok")]
        return VerificationResult(ok=not failed, checks=checks, failed=failed)
