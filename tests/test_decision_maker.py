"""The LangChain decision maker and the structured decision schema. Uses fake chains: no model, no database.
One optional live test runs only with LIVE_OLLAMA=1 (it needs a running Ollama and writes nothing)."""
import json
import os

import httpx
import ollama
import pytest
from langchain_core.exceptions import OutputParserException
from pydantic import ValidationError

from app.config import Settings
from app.recovery.decision_maker import (PROMPT, DecisionMaker, InvalidModelOutputError, OllamaUnavailableError,
                                         build_chain, build_llm, context_for_prompt)
from app.schemas.recovery import (DecisionEnvelope, RecoveryProposal, decision_parameters, to_proposal)
from tests.context_factory import make_context

VALID = {
    "schedule_retry": {"action": "schedule_retry", "retry_after_hours": 24, "explanation": "Funds were short."},
    "request_payment_update": {"action": "request_payment_update", "explanation": "Card expired."},
    "send_reminder": {"action": "send_reminder", "tone": "friendly", "explanation": "Gentle nudge."},
    "escalate_to_human": {"action": "escalate_to_human", "reason": "Possible fraud", "explanation": "Needs a person."},
    "no_action": {"action": "no_action", "reason": "Wait a day", "explanation": "Nothing useful now."},
}


def envelope(**decision):
    return DecisionEnvelope.model_validate({"decision": decision})


class FakeChain:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.inputs = result, error, []

    def invoke(self, inputs):
        self.inputs.append(inputs)
        if self.error:
            raise self.error
        return self.result


# ---------------------------------------------------------------- structured output validation

@pytest.mark.parametrize("name,data", VALID.items())
def test_each_action_has_a_valid_form(name, data):
    assert envelope(**data).decision.action == name


def test_parameters_have_the_shape_the_rest_of_the_system_uses():
    got = {name: decision_parameters(envelope(**data).decision) for name, data in VALID.items()}
    assert got == {"schedule_retry": {"retry_after_hours": 24}, "request_payment_update": {},
                   "send_reminder": {"tone": "friendly"}, "escalate_to_human": {"reason": "Possible fraud"},
                   "no_action": {"reason": "Wait a day"}}


@pytest.mark.parametrize("bad", [
    {"action": "schedule_retry", "explanation": "x"},                                        # hours missing
    {"action": "schedule_retry", "retry_after_hours": 0, "explanation": "x"},                # too small
    {"action": "schedule_retry", "retry_after_hours": 169, "explanation": "x"},              # too large
    {"action": "schedule_retry", "retry_after_hours": "soon", "explanation": "x"},           # wrong type
    {"action": "send_reminder", "tone": "threatening", "explanation": "x"},                  # not an allowed tone
    {"action": "send_reminder", "explanation": "x"},                                         # tone missing
    {"action": "escalate_to_human", "explanation": "x"},                                     # reason missing
    {"action": "no_action", "reason": "", "explanation": "x"},                               # reason empty
    {"action": "refund_customer", "explanation": "x"},                                       # not an action
    {"action": "request_payment_update"},                                                    # explanation missing
    {"action": "request_payment_update", "explanation": "x", "charge_now": True},            # extra field
])
def test_invalid_decisions_are_rejected(bad):
    with pytest.raises(ValidationError):
        envelope(**bad)


def test_uncertainty_is_optional():
    assert envelope(**VALID["no_action"]).decision.uncertainty is None
    assert envelope(**VALID["no_action"], uncertainty="No payment history").decision.uncertainty == "No payment history"


# ---------------------------------------------------------------- /recovery/proposal contract

def test_proposal_contract_is_built_from_the_decision():
    proposal = to_proposal(envelope(**VALID["schedule_retry"]).decision, "pay_1")
    assert isinstance(proposal, RecoveryProposal)
    assert proposal.model_dump() == {"payment_id": "pay_1", "action": "schedule_retry",
                                     "parameters": {"retry_after_hours": 24}, "summary": "Funds were short."}


def test_long_explanations_are_shortened_for_the_proposal_summary():
    decision = envelope(**{**VALID["no_action"], "explanation": "x" * 900}).decision
    assert len(to_proposal(decision, "pay_1").summary) == 300


# ---------------------------------------------------------------- the prompt

def test_prompt_contains_the_context_without_the_customers_name_or_email():
    messages = PROMPT.format_messages(context=context_for_prompt(make_context()), feedback="")
    text = "\n".join(m.content for m in messages)
    assert "insufficient_funds" in text and '"customer": {"id": "c1"}' in text
    assert "Jane Doe" not in text and "jane@example.com" not in text


def test_prompt_lists_all_five_actions():
    text = PROMPT.format_messages(context="{}", feedback="")[0].content
    for action in VALID:
        assert action in text


def test_prompt_includes_feedback_only_when_given():
    assert "BLOCKED" not in PROMPT.format_messages(context="{}", feedback="")[0].content
    assert "BLOCKED" in PROMPT.format_messages(context="{}", feedback="\nBLOCKED: no")[0].content


# ---------------------------------------------------------------- DecisionMaker with a fake chain

def test_propose_returns_the_validated_decision():
    chain = FakeChain(envelope(**VALID["schedule_retry"]))
    decision = DecisionMaker(chain=chain).propose(make_context())
    assert decision.action == "schedule_retry" and decision.retry_after_hours == 24


def test_propose_sends_the_context_and_feedback_to_the_chain():
    chain = FakeChain(envelope(**VALID["no_action"]))
    DecisionMaker(chain=chain).propose(make_context(failure_reason="suspected_fraud"), feedback="Last try was refused.")
    sent = chain.inputs[0]
    assert json.loads(sent["context"])["failed_payment"]["failure_reason"] == "suspected_fraud"
    assert "Last try was refused." in sent["feedback"]
    DecisionMaker(chain=chain).propose(make_context())
    assert chain.inputs[1]["feedback"] == ""


def test_dict_results_are_validated_too():
    chain = FakeChain({"decision": VALID["send_reminder"]})
    assert DecisionMaker(chain=chain).propose(make_context()).tone == "friendly"


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"),
                                   ollama.ResponseError("model not found", 404), ConnectionError("down"),
                                   TimeoutError("timeout")])
def test_connection_problems_become_ollama_unavailable(error):
    with pytest.raises(OllamaUnavailableError):
        DecisionMaker(chain=FakeChain(error=error)).propose(make_context())


def test_unparseable_or_invalid_output_becomes_invalid_model_output():
    for chain in (FakeChain(error=OutputParserException("not json")),
                  FakeChain(result=None),
                  FakeChain(result={"decision": {"action": "refund_customer", "explanation": "x"}}),
                  FakeChain(result={"decision": {"action": "schedule_retry", "explanation": "x"}})):
        with pytest.raises(InvalidModelOutputError):
            DecisionMaker(chain=chain).propose(make_context())


# ---------------------------------------------------------------- real LangChain wiring (no network)

def test_llm_is_configured_from_settings():
    llm = build_llm(Settings(database_url="sqlite://", ollama_url="http://example:1234", ollama_model="llama3.2:3b",
                             ollama_timeout=45))
    assert (llm.model, llm.base_url, llm.temperature) == ("llama3.2:3b", "http://example:1234", 0)
    assert llm.client_kwargs == {"timeout": 45}


def test_chain_is_prompt_then_model_then_pydantic_parser():
    steps = [type(s).__name__ for s in build_chain(Settings(database_url="sqlite://")).steps]
    assert steps == ["ChatPromptTemplate", "_ChatModelBinding", "PydanticOutputParser"]


def test_an_unreachable_ollama_is_reported_as_unavailable():
    maker = DecisionMaker(config=Settings(database_url="sqlite://", ollama_url="http://127.0.0.1:1", ollama_timeout=3))
    with pytest.raises(OllamaUnavailableError):
        maker.propose(make_context())


def test_langsmith_tracing_is_off_by_default(monkeypatch):
    for var in ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        monkeypatch.delenv(var, raising=False)
    from langsmith.utils import get_env_var, tracing_is_enabled
    get_env_var.cache_clear()
    assert tracing_is_enabled() is False


# ---------------------------------------------------------------- optional live test

@pytest.mark.skipif(os.environ.get("LIVE_OLLAMA") != "1", reason="set LIVE_OLLAMA=1 to call a real Ollama")
@pytest.mark.parametrize("reason", ["insufficient_funds", "expired_card", "suspected_fraud"])
def test_live_llama_returns_a_valid_decision(reason):
    decision = DecisionMaker().propose(make_context(failure_reason=reason))
    assert decision.action in VALID and decision.explanation.strip()
    DecisionEnvelope.model_validate({"decision": decision.model_dump()})       # round-trips through the schema
