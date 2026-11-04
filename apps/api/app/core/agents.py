"""Agent Manager and the two executable V1 agents.

Least privilege is structural, not advisory: an agent holds a frozenset of tool
names and the runtime refuses anything outside it. Agents can never grant
permissions - not to themselves, not to each other.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel

from app.core.engines import Plan, PlanningEngine, PlanStep, TaskUnderstanding
from app.core.tools import ToolContext
from app.models.base import Complexity, TaskType
from app.security.permissions import PermissionClass, PermissionController


class AgentStatus(StrEnum):
    AVAILABLE = "available"
    WORKING = "working"
    BLOCKED = "blocked"
    INTERFACE_ONLY = "interface_only"


class AgentTask(BaseModel):
    id: str
    goal: str
    inputs: dict[str, Any] = {}
    constraints: list[str] = []
    completion_criteria: list[str] = []
    parent_id: str | None = None
    permissions_granted: list[str] = []
    deadline_s: int | None = None


class ToolAction(BaseModel):
    tool: str
    params: dict[str, Any] = {}
    permission_class: PermissionClass


class ActionResult(BaseModel):
    ok: bool
    outputs: dict[str, Any] = {}
    error: str | None = None


class VerificationResult(BaseModel):
    ok: bool
    detail: str = ""


class Agent(Protocol):
    name: str
    description: str
    allowed_tools: frozenset[str]

    def plan(self, task: AgentTask) -> Plan: ...

    def request_permission(self, action: ToolAction) -> PermissionClass: ...

    async def execute(self, approved: ToolAction, ctx: ToolContext) -> ActionResult: ...

    def verify(self, result: ActionResult) -> VerificationResult: ...


class BaseAgent:
    """Shared enforcement. Subclasses declare intent; this class polices it."""

    name = "base"
    description = ""
    allowed_tools: frozenset[str] = frozenset()

    def __init__(self, permissions: PermissionController) -> None:
        self.permissions = permissions

    def request_permission(self, action: ToolAction) -> PermissionClass:
        if action.tool not in self.allowed_tools:
            msg = (
                f"agent '{self.name}' may not call '{action.tool}'. "
                f"Allowed: {sorted(self.allowed_tools)}"
            )
            raise PermissionError(msg)
        return action.permission_class

    def classify_tool(self, tool: str) -> PermissionClass:
        return {
            "fs.list": PermissionClass.READ,
            "fs.read": PermissionClass.READ,
            "fs.mkdir": PermissionClass.WRITE,
            "fs.create": PermissionClass.WRITE,
            "fs.edit": PermissionClass.WRITE,
            "project.scaffold": PermissionClass.WRITE,
            "fs.delete": PermissionClass.DELETE,
        }.get(tool, PermissionClass.FORBIDDEN)

    def verify(self, result: ActionResult) -> VerificationResult:
        return VerificationResult(ok=result.ok, detail=result.error or "ok")


class ManagerAgent(BaseAgent):
    """Plans and coordinates. V1 scope: one supervised workflow, not autonomy."""

    name = "manager"
    description = "Decomposes a goal into permission-labelled steps and reports honestly."
    allowed_tools = frozenset(
        {"fs.list", "fs.read", "fs.mkdir", "fs.create", "fs.edit", "project.scaffold"}
    )

    def __init__(
        self, permissions: PermissionController, planner: PlanningEngine | None = None
    ) -> None:
        super().__init__(permissions)
        self.planner = planner

    def plan(self, task: AgentTask) -> Plan:
        understanding = TaskUnderstanding(
            task_type=TaskType.PROJECT_SCAFFOLD, complexity=Complexity.COMPLEX
        )
        plan = self.rule_plan(task)
        if self.planner is not None:
            # A model-generated plan is only accepted if it stays inside our allow-list.
            candidate = self.planner.rule_plan(understanding, task.goal)
            if all((s.tool or "fs.list") in self.allowed_tools for s in candidate.steps):
                plan = candidate
        for step in plan.steps:
            if step.tool and step.tool not in self.allowed_tools:
                msg = f"plan step uses a tool outside manager's allow-list: {step.tool}"
                raise PermissionError(msg)
        return plan

    def rule_plan(self, task: AgentTask) -> Plan:
        return Plan(
            goal=task.goal,
            generated_by="rules",
            needs_approval=True,
            steps=[
                PlanStep(
                    id="s1",
                    description="Inspect the workspace root",
                    tool="fs.list",
                    params={"path": "."},
                    required_permission="read",
                ),
                PlanStep(
                    id="s2",
                    description="Scaffold the project",
                    tool="project.scaffold",
                    params={
                        "template": task.inputs.get("template", "python_dashboard"),
                        "project_name": task.inputs.get("project_name", "nyxora_project"),
                    },
                    required_permission="write",
                    depends_on=["s1"],
                    completion_criteria=["files exist", "no file is empty"],
                ),
            ],
        )


class CodingAgent(BaseAgent):
    name = "coding"
    description = "Writes and edits project files inside the workspace root."
    allowed_tools = frozenset({"fs.read", "fs.create", "fs.edit", "fs.list", "project.scaffold"})

    def plan(self, task: AgentTask) -> Plan:
        files = task.inputs.get("files", {})
        steps = [
            PlanStep(
                id=f"f{i + 1}",
                description=f"Write {name}",
                tool="fs.create",
                params={
                    "path": f"{task.inputs.get('project_name', 'project')}/{name}",
                    "content": content,
                },
                required_permission="write",
                completion_criteria=["file exists and is non-empty"],
            )
            for i, (name, content) in enumerate(files.items())
        ]
        return Plan(goal=task.goal, steps=steps, needs_approval=True, generated_by="rules")


class InterfaceOnlyAgent(BaseAgent):
    """Documents a future specialist without pretending it exists."""

    def __init__(self, name: str, description: str, allowed: frozenset[str]) -> None:
        self.name = name
        self.description = description
        self.allowed_tools = allowed

    def plan(self, task: AgentTask) -> Plan:
        msg = f"agent '{self.name}' is an interface only in V1 and cannot execute"
        raise NotImplementedError(msg)


ROSTER: dict[str, dict[str, Any]] = {
    "manager": {
        "description": "Planning and coordination",
        "tools": ("fs.list", "fs.read", "fs.mkdir", "fs.create", "fs.edit", "project.scaffold"),
        "status": AgentStatus.AVAILABLE,
    },
    "coding": {
        "description": "Code changes and tests",
        "tools": ("fs.read", "fs.create", "fs.edit", "project.scaffold"),
        "status": AgentStatus.AVAILABLE,
    },
    "research": {
        "description": "Source-grounded research",
        "tools": ("fs.read",),
        "status": AgentStatus.INTERFACE_ONLY,
    },
    "computer": {
        "description": "Controlled computer actions",
        "tools": (),
        "status": AgentStatus.INTERFACE_ONLY,
    },
    "security": {
        "description": "Policy checks and audit inspection",
        "tools": (),
        "status": AgentStatus.INTERFACE_ONLY,
    },
    "personal_assistant": {
        "description": "Reminders and notifications",
        "tools": (),
        "status": AgentStatus.INTERFACE_ONLY,
    },
    "document_analyst": {
        "description": "Document analysis over RAG",
        "tools": ("fs.read",),
        "status": AgentStatus.INTERFACE_ONLY,
    },
    "system_monitor": {
        "description": "App health telemetry",
        "tools": (),
        "status": AgentStatus.INTERFACE_ONLY,
    },
    "workflow_manager": {
        "description": "Multi-step coordination",
        "tools": (),
        "status": AgentStatus.INTERFACE_ONLY,
    },
}


class AgentManager:
    def __init__(self, permissions: PermissionController) -> None:
        self.permissions = permissions
        self._agents: dict[str, BaseAgent] = {
            "manager": ManagerAgent(permissions),
            "coding": CodingAgent(permissions),
        }

    def get(self, name: str) -> BaseAgent | None:
        return self._agents.get(name)

    def roster(self) -> list[dict[str, Any]]:
        out = []
        for name, meta in ROSTER.items():
            out.append(
                {
                    "name": name,
                    "description": meta["description"],
                    "allowed_tools": sorted(meta["tools"]),
                    "status": meta["status"].value,
                    "executable": name in self._agents,
                }
            )
        return out

    def dispatch(self, agent_name: str, action: ToolAction) -> PermissionClass:
        agent = self._agents.get(agent_name)
        if agent is None:
            msg = f"'{agent_name}' is not executable in V1"
            raise PermissionError(msg)
        return agent.request_permission(action)
