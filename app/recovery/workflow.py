"""The small LangGraph workflow behind POST /recovery/simulate.

    START -> propose -> check -> done
                ^         |
                '---------'   (blocked or invalid on the first try: ask once more)

It only orchestrates. The LLM proposes (DecisionMaker); the deterministic policy approves or blocks
(policy.py). At most TWO proposals are ever requested, and a blocked proposal is never executed here.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.context.recovery_context import RecoveryContext
from app.recovery.decision_maker import InvalidModelOutputError
from app.recovery.policy import DEFAULT_CONFIG, PolicyConfig, PolicyDecision, make_policy_check, snapshot_from_context
from app.schemas.recovery import decision_parameters

MAX_PROPOSALS = 2


@dataclass
class Attempt:
    """One proposal and what the policy said about it (or why it was unusable)."""
    decision: object | None = None            # the LLM's validated decision
    parameters: dict | None = None
    verdict: PolicyDecision | None = None
    error: str | None = None                  # set when the LLM output was not a valid decision

    @property
    def action(self) -> str | None:
        return self.decision.action if self.decision is not None else None

    @property
    def approved(self) -> bool:
        return self.verdict is not None and self.verdict.allowed


class State(TypedDict):
    attempts: list[Attempt]
    feedback: str | None


def feedback_for(attempt: Attempt) -> str:
    if attempt.verdict is not None:
        v = attempt.verdict
        return (f"IMPORTANT: your previous proposal '{attempt.action}' was refused by the safety policy: {v.reason} "
                f"Propose a DIFFERENT action. Actions the policy currently permits: {v.alternatives}.")
    return f"IMPORTANT: your previous answer was not a valid decision ({attempt.error}). Answer again with a valid decision."


def build_workflow(decision_maker, context: RecoveryContext, now: datetime, config: PolicyConfig = DEFAULT_CONFIG):
    policy_check = make_policy_check(snapshot_from_context(context, now), config)

    def propose(state: State) -> dict:
        attempts = list(state["attempts"])
        try:
            decision = decision_maker.propose(context, state["feedback"])
        except InvalidModelOutputError as exc:
            attempts.append(Attempt(error=str(exc)))
        else:
            attempts.append(Attempt(decision=decision, parameters=decision_parameters(decision)))
        return {"attempts": attempts}

    def check(state: State) -> dict:
        attempts = list(state["attempts"])
        last = attempts[-1]
        last.verdict = policy_check(last.action, last.parameters)      # approve or block; never picks an action
        return {"attempts": attempts}

    def after_propose(state: State) -> str:
        if state["attempts"][-1].decision is not None:
            return "check"
        return "retry" if len(state["attempts"]) < MAX_PROPOSALS else "done"

    def after_check(state: State) -> str:
        last = state["attempts"][-1]
        return "done" if last.approved or len(state["attempts"]) >= MAX_PROPOSALS else "retry"

    def retry(state: State) -> dict:
        return {"feedback": feedback_for(state["attempts"][-1])}

    graph = StateGraph(State)
    graph.add_node("propose", propose)
    graph.add_node("check", check)
    graph.add_node("retry", retry)
    graph.add_edge(START, "propose")
    graph.add_conditional_edges("propose", after_propose, {"check": "check", "retry": "retry", "done": END})
    graph.add_conditional_edges("check", after_check, {"retry": "retry", "done": END})
    graph.add_edge("retry", "propose")
    return graph.compile()


def run_workflow(decision_maker, context: RecoveryContext, now: datetime,
                 config: PolicyConfig = DEFAULT_CONFIG) -> list[Attempt]:
    """Ask the LLM (at most twice) and return every attempt in order. Raises InvalidModelOutputError if no
    attempt produced a usable decision, and lets OllamaUnavailableError through."""
    final = build_workflow(decision_maker, context, now, config).invoke({"attempts": [], "feedback": None})
    attempts = final["attempts"]
    if not any(a.decision is not None for a in attempts):
        raise InvalidModelOutputError(attempts[-1].error or "Model produced no valid decision")
    return attempts
