from __future__ import annotations

from datetime import UTC, datetime

from app.core.models import RunState, StageName, TodoPlan, TodoTask, TodoTaskStatus


DEFAULT_STAGE_TASKS: list[tuple[StageName, str, str]] = [
    (StageName.plan, 'Define plan and schema', 'OrchestratorAgent'),
    (StageName.collect, 'Collect evidence', 'CollectorAgent'),
    (StageName.normalize, 'Normalize evidence', 'Normalizer'),
    (StageName.analyze, 'Analyze competitors', 'AnalystAgent'),
    (StageName.qa, 'Review analysis quality', 'QACriticAgent'),
    (StageName.draft, 'Draft report', 'WriterAgent'),
    (StageName.finalize, 'Finalize run output', 'Finalizer'),
]


class TodoStateManager:
    def __init__(self, state: RunState):
        self.state = state

    def init_from_run_state(self) -> TodoPlan:
        if self.state.todo_plan.tasks:
            return self.state.todo_plan
        tasks: list[TodoTask] = []
        previous_task_id: str | None = None
        for stage, title, owner in DEFAULT_STAGE_TASKS:
            task_id = f'{stage.value}_task'
            depends_on = [previous_task_id] if previous_task_id else []
            tasks.append(
                TodoTask(
                    task_id=task_id,
                    title=title,
                    owner_agent=owner,
                    stage=stage,
                    status=TodoTaskStatus.pending,
                    depends_on=depends_on,
                )
            )
            previous_task_id = task_id
        self.state.todo_plan = TodoPlan(tasks=tasks, current_task_id=tasks[0].task_id if tasks else None, version=1)
        return self.state.todo_plan

    def mark_stage_started(self, stage: StageName, *, agent_name: str | None = None) -> TodoTask | None:
        task = self._find_task(stage)
        if task is None:
            return None
        task.status = TodoTaskStatus.in_progress
        if agent_name:
            task.owner_agent = agent_name
        task.updated_at = datetime.now(UTC)
        self.state.todo_plan.current_task_id = task.task_id
        self.state.todo_plan.version += 1
        return task

    def mark_stage_completed(self, stage: StageName, *, agent_name: str | None = None, notes: str = '') -> TodoTask | None:
        task = self._find_task(stage)
        if task is None:
            return None
        task.status = TodoTaskStatus.completed
        if agent_name:
            task.owner_agent = agent_name
        if notes:
            task.notes = notes
        task.updated_at = datetime.now(UTC)
        self.state.todo_plan.current_task_id = task.task_id
        self.state.todo_plan.version += 1
        return task

    def mark_stage_blocked(self, stage: StageName, *, reason: str, agent_name: str | None = None) -> TodoTask | None:
        task = self._find_task(stage)
        if task is None:
            return None
        task.status = TodoTaskStatus.blocked
        if agent_name:
            task.owner_agent = agent_name
        task.notes = reason
        task.updated_at = datetime.now(UTC)
        self.state.todo_plan.current_task_id = task.task_id
        self.state.todo_plan.version += 1
        return task

    def _find_task(self, stage: StageName) -> TodoTask | None:
        for task in self.state.todo_plan.tasks:
            if task.stage == stage:
                return task
        return None
