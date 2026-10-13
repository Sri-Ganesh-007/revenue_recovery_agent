"""The LLM decision maker: asks Llama (through LangChain) for ONE structured recovery decision.

The LLM chooses the strategy. Nothing in this module (or anywhere in Python) picks the action for
it; the deterministic policy later approves or blocks what the LLM proposed.
"""
import json

import httpx
import ollama
from langchain_core.exceptions import OutputParserException
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama
from pydantic import ValidationError

from app.config import Settings, settings as default_settings
from app.context.recovery_context import RecoveryContext
from app.schemas.recovery import DecisionEnvelope


class OllamaUnavailableError(Exception):
    """Ollama could not be reached, timed out, or returned an error."""


class InvalidModelOutputError(Exception):
    """The model answered, but not with a valid recovery decision."""


SYSTEM_PROMPT = """You are a payment recovery assistant for a subscription business. You receive the context of one
failed subscription payment and choose exactly ONE recovery action. Read the failure_reason and the history
carefully: different failures call for different actions. Your choice is only a proposal; a separate safety policy
checks it, and a blocked proposal is never executed.

Your options and when each tends to make sense:
- schedule_retry: the failure looks temporary (for example insufficient funds), so trying again later may work.
  Needs retry_after_hours (1-168).
- request_payment_update: the payment method itself is the problem (for example an expired card, invalid
  details, or a processor decline), so the customer must supply new details.
- send_reminder: a gentle nudge is enough (tone friendly or urgent).
- escalate_to_human: the case is risky or unclear (for example suspected fraud or an unclear gateway timeout) and a
  person should decide. Give a short reason.
- no_action: nothing useful can be done right now. Give a short reason.

Always explain your choice in "explanation". If something is unclear or missing, say so in "uncertainty".
{feedback}"""

PROMPT = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_PROMPT),
    ("human", "Payment context (JSON):\n{context}"),
])


def context_for_prompt(context: RecoveryContext) -> str:
    """The context as JSON for the model. The customer's name and email are not needed, so they are not sent."""
    data = context.model_dump(mode="json")
    data["customer"] = {"id": context.customer.id}
    return json.dumps(data)


def build_llm(config: Settings = default_settings) -> ChatOllama:
    return ChatOllama(model=config.ollama_model, base_url=config.ollama_url, temperature=0,
                      num_ctx=4096, client_kwargs={"timeout": config.ollama_timeout})


def build_chain(config: Settings = default_settings):
    """prompt -> Llama -> a validated DecisionEnvelope (Pydantic), using Ollama's JSON-schema output."""
    return PROMPT | build_llm(config).with_structured_output(DecisionEnvelope, method="json_schema")


class DecisionMaker:
    def __init__(self, chain=None, config: Settings = default_settings):
        # `chain` is injectable so tests can use a fake instead of a real model.
        self.chain = chain if chain is not None else build_chain(config)

    def propose(self, context: RecoveryContext, feedback: str | None = None):
        """Return one validated decision. `feedback` (optional) tells the model why its last try was refused."""
        inputs = {"context": context_for_prompt(context), "feedback": f"\n{feedback}" if feedback else ""}
        try:
            result = self.chain.invoke(inputs)
        except (httpx.HTTPError, ollama.ResponseError, ConnectionError, TimeoutError) as exc:
            raise OllamaUnavailableError(f"Ollama request failed: {exc}") from exc
        except (OutputParserException, ValidationError, ValueError) as exc:
            raise InvalidModelOutputError(f"Model output was not a valid decision: {exc}") from exc

        if result is None:
            raise InvalidModelOutputError("Model returned no decision")
        try:
            return (result if isinstance(result, DecisionEnvelope) else DecisionEnvelope.model_validate(result)).decision
        except ValidationError as exc:
            raise InvalidModelOutputError(f"Model output was not a valid decision: {exc}") from exc
