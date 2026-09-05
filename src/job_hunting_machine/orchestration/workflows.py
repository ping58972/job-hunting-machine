"""Replay-safe workflow contract and deterministic, local-only example graph.

Nodes transform checkpointed input only. Generate IDs, read clocks/files, obtain
human input and execute effects at explicit durable boundaries outside node logic.
The example has no integrations and is never registered for BUILD_RESUME tasks.
"""

from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class WorkflowState(TypedDict, total=False):
    task_id: str
    payload: dict[str, Any]
    prepared: str
    answer: Any
    result: str


@dataclass(frozen=True)
class Workflow:
    version: str
    graph: StateGraph[WorkflowState, None, WorkflowState, WorkflowState]


def fake_workflow(*, human: bool = False) -> Workflow:
    def prepare(state: WorkflowState) -> dict[str, str]:
        return {"prepared": str(state["payload"].get("text", "fixture")).upper()}

    def review(state: WorkflowState) -> dict[str, Any]:
        # LangGraph reruns this node on resume. Nothing before interrupt has effects.
        answer = interrupt({"question": "Accept deterministic fixture?", "text": state["prepared"]})
        return {"answer": answer}

    def finish(state: WorkflowState) -> dict[str, str]:
        return {"result": state["prepared"]}

    graph = StateGraph(WorkflowState)
    graph.add_node("prepare", prepare)
    graph.add_node("finish", finish)
    graph.add_edge(START, "prepare")
    if human:
        graph.add_node("review", review)
        graph.add_edge("prepare", "review")
        graph.add_edge("review", "finish")
    else:
        graph.add_edge("prepare", "finish")
    graph.add_edge("finish", END)
    return Workflow("fake-human-v1" if human else "fake-v1", graph)
