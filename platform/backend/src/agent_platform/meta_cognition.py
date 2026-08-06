"""Meta-cognition layer — observe collaboration, extract decision patterns,
and generate reusable workflow templates automatically.

The core idea: during a development conversation, key decision points form
a tree. That tree IS a workflow. Extract it → validate it → publish it as
a template → next time someone asks a similar question, suggest the template.

Example (from our DingTalk journey):
  "Auto check-in for App X"
    → Does X have an API?      → YES → use http_request
    → NO → Does X have quick mode? → YES → just launch app
    → NO → Can we simulate taps?   → YES → input tap + coordinates
    → NO → Fall back to manual reminder
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from .workflow_models import EdgeSpec, NodeSpec, WorkflowSpec


@dataclass
class DecisionPoint:
    """A single branching decision in a collaboration."""

    id: str = field(default_factory=lambda: str(uuid4()))
    question: str = ""                         # "Does this app have a public API?"
    context: str = ""                          # "Trying to automate DingTalk check-in"
    branches: list[DecisionBranch] = field(default_factory=list)
    parent_id: str | None = None
    depth: int = 0


@dataclass
class DecisionBranch:
    """One possible answer to a decision point."""

    answer: str  # "YES", "NO", "LIMITED"
    description: str = ""  # "App has API but requires enterprise admin"
    outcome: str = ""  # "Use http_request block"
    sub_decisions: list[DecisionPoint] = field(default_factory=list)


class DecisionTracker:
    """Records decision points during a collaboration session.

    Usage::

        tracker = DecisionTracker("DingTalk Auto Punch")
        tracker.ask("Does this app have a public API?")
        tracker.answer("NO", "API exists but requires enterprise admin")
        tracker.ask("Does it have quick/auto mode?")
        tracker.answer("YES", "DingTalk quick punch triggers on app open")
        # ... later
        workflow = tracker.extract_workflow()
        # workflow is a valid WorkflowSpec ready for template publishing
    """

    def __init__(self, task_name: str = "Untitled Task") -> None:
        self.task_name = task_name
        self.roots: list[DecisionPoint] = []
        self._current: DecisionPoint | None = None
        self._stack: list[DecisionPoint] = []

    def ask(self, question: str, context: str = "") -> DecisionPoint:
        """Record a new decision point."""
        dp = DecisionPoint(
            question=question,
            context=context or f"During: {self.task_name}",
            depth=len(self._stack),
            parent_id=self._current.id if self._current else None,
        )
        if self._current:
            # Find the last branch of current and add as sub-decision
            if self._current.branches:
                last_branch = self._current.branches[-1]
                last_branch.sub_decisions.append(dp)
        else:
            self.roots.append(dp)
        return dp

    def answer(
        self, answer: str, outcome: str = "", description: str = ""
    ) -> DecisionBranch:
        """Record the answer to the current decision point."""
        if not self._current:
            raise ValueError("No active decision point. Call ask() first.")
        branch = DecisionBranch(
            answer=answer, outcome=outcome, description=description
        )
        self._current.branches.append(branch)
        return branch

    def extract_workflow(self) -> WorkflowSpec:
        """Convert the decision tree into a Lilies workflow template.

        Each decision point becomes: LLM → If/Else
        Each branch becomes: Template (solution description)
        """
        nodes: list[NodeSpec] = []
        edges: list[EdgeSpec] = []
        node_counter = [0]
        edge_counter = [0]

        # Start node
        start_id = "start"
        nodes.append(
            NodeSpec(
                id=start_id, type="start", title=self.task_name,
                config={
                    "inputs": [
                        {"name": "task_description", "label": "任务描述", "type": "string"},
                    ]
                },
            )
        )

        def process_decision(dp: DecisionPoint, parent_id: str) -> tuple[str, str]:
            """Returns (last_node_id, last_output_port) for chaining."""
            nid = f"d_{dp.id[:8]}"
            # LLM node for this decision
            nodes.append(
                NodeSpec(
                    id=nid, type="llm", title=dp.question[:60],
                    config={
                        "system": f"Analyze and answer: {dp.question}",
                        "prompt": {"$ref": {"node_id": start_id, "path": ["task_description"]}},
                    },
                )
            )
            node_counter[0] += 1
            # Determine correct source port based on parent node type
            parent_node = next((n for n in nodes if n.id == parent_id), None)
            src_port = "output"
            if parent_node and parent_node.type == "template_transform":
                src_port = "text"
            edges.append(
                EdgeSpec(
                    id=f"e_{edge_counter[0]}",
                    source=parent_id, target=nid,
                    source_port=src_port, target_port="input",
                )
            )
            edge_counter[0] += 1

            if not dp.branches:
                return nid, "text"

            # If/Else router
            router_id = f"r_{dp.id[:8]}"
            cases = []
            branch_ids = []
            for b in dp.branches:
                bid = b.answer.lower().replace(" ", "_")[:20]
                branch_ids.append(bid)
                cases.append({
                    "id": bid,
                    "conditions": [
                        {
                            "value": {"$ref": {"node_id": nid, "path": ["text"]}},
                            "operator": "contains",
                            "expected": b.answer,
                        }
                    ],
                    "logical_operator": "and",
                })

            nodes.append(
                NodeSpec(
                    id=router_id, type="if_else", title=f"→ {dp.question[:40]}",
                    config={"cases": cases, "default_branch": branch_ids[-1] if branch_ids else "else"},
                )
            )
            edges.append(
                EdgeSpec(
                    id=f"e_{edge_counter[0]}",
                    source=nid, target=router_id,
                    source_port="text", target_port="input",
                )
            )
            edge_counter[0] += 1

            # Template per branch (prefix with dp.id to avoid duplicates across questions)
            for b, bid in zip(dp.branches, branch_ids):
                tid = f"t_{dp.id[:6]}_{bid[:8]}"
                nodes.append(
                    NodeSpec(
                        id=tid, type="template_transform",
                        title=f"方案: {b.answer}",
                        config={
                            "template": f"# {b.answer}\n\n{b.description}\n\n## 实现\n{b.outcome}\n\n## 分析\n{{{{ analysis }}}}",
                            "variables": {"analysis": {"$ref": {"node_id": nid, "path": ["text"]}}},
                        },
                    )
                )
                edges.append(
                    EdgeSpec(
                        id=f"e_{edge_counter[0]}",
                        source=router_id, target=tid,
                        source_port="branch", target_port="input",
                        branch=bid,
                    )
                )
                edge_counter[0] += 1

                # Process sub-decisions for this branch
                for sub in b.sub_decisions:
                    process_decision(sub, tid)

            return router_id, "branch"

        prev_id = start_id
        prev_port = "output"
        for root in self.roots:
            prev_id, prev_port = process_decision(root, prev_id)

        # Aggregator + End
        agg_id = "agg"
        agg_vars = []
        for node in nodes:
            if node.type == "template_transform":
                agg_vars.append({
                    "$ref": {"node_id": node.id, "path": ["text"], "optional": True}
                })
        nodes.append(
            NodeSpec(
                id=agg_id, type="variable_aggregator", title="聚合结果",
                config={"variables": agg_vars, "mode": "first_non_null"},
            )
        )
        edges.append(
            EdgeSpec(
                id=f"e_{edge_counter[0]}",
                source=prev_id, target=agg_id,
                source_port=prev_port, target_port="input",
            )
        )
        edge_counter[0] += 1

        end_id = "end"
        nodes.append(
            NodeSpec(
                id=end_id, type="end", title="方案输出",
                config={
                    "outputs": {
                        "solution": {"$ref": {"node_id": agg_id, "path": ["output"]}},
                    }
                },
            )
        )
        edges.append(
            EdgeSpec(
                id=f"e_{edge_counter[0]}",
                source=agg_id, target=end_id,
                source_port="output", target_port="input",
            )
        )

        return WorkflowSpec(nodes=nodes, edges=edges)

    def summary(self) -> str:
        """Generate a human-readable summary of the decision tree."""
        lines = [f"# Decision Tree: {self.task_name}", ""]

        def walk(dp: DecisionPoint, indent: int = 0):
            prefix = "  " * indent
            lines.append(f"{prefix}- **Q**: {dp.question}")
            for b in dp.branches:
                lines.append(f"{prefix}  - → {b.answer}: {b.outcome}")
                for sub in b.sub_decisions:
                    walk(sub, indent + 2)

        for root in self.roots:
            walk(root)
        return "\n".join(lines)


# ── Demo: Extract the DingTalk decision tree ──

def demo_dingtalk_workflow() -> WorkflowSpec:
    """Replay our actual collaboration as a decision tree, then extract it."""
    tracker = DecisionTracker("App 自动化决策流程")

    # Root: API check
    tracker._current = tracker.ask(
        "该 App 是否有公开 API 可以完成此任务？",
        "自动打卡/签到类任务",
    )
    # Branch: NO
    api_no = tracker.answer(
        "NO",
        outcome="检查急速模式",
        description="个人用户通常无法使用企业级考勤 API。需要企业管理员权限。",
    )
    # Sub-decision: Quick mode
    sub = DecisionPoint(
        question="该 App 是否有急速/自动模式，打开即触发？",
        context="API 不可用，寻找替代方案",
        depth=1,
    )
    api_no.sub_decisions.append(sub)
    tracker._current = sub
    quick_yes = tracker.answer(
        "YES",
        outcome="只需定时启动 App",
        description="钉钉「急速打卡」: 在考勤时间范围内打开 App，自动完成打卡。最简方案。",
    )
    quick_no = tracker.answer(
        "NO",
        outcome="尝试模拟点击",
    )
    # Sub-sub-decision: Clicks
    click_sub = DecisionPoint(
        question="是否可通过模拟点击完成？坐标是否稳定？",
        context="无急速模式",
        depth=2,
    )
    quick_no.sub_decisions.append(click_sub)
    tracker._current = click_sub
    tracker.answer(
        "FEASIBLE",
        outcome="input tap + 坐标校准",
        description="使用 Android input tap 命令模拟屏幕点击。需要开发者选项获取坐标。坐标可能因 App 更新变化。",
    )
    tracker.answer(
        "NOT_FEASIBLE",
        outcome="设置提醒，人工完成",
        description="该 App 有反自动化检测，或操作需要生物识别验证。目前无法可靠自动化。",
    )

    # Branch: YES (add to the root decision point)
    tracker._current = tracker.roots[0]
    api_yes = tracker.answer(
        "YES",
        outcome="使用 HTTP Request 积木直接调用 API",
        description="App 提供公开 API。获取凭证 → HTTP Request → schedule_trigger 定时触发。",
    )

    return tracker.extract_workflow()
