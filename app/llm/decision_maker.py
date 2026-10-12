import json
import os
import urllib.error
import urllib.request

from pydantic import ValidationError

from app.context.recovery_context import RecoveryContext
from app.llm.proposal import RecoveryProposal


class OllamaUnavailableError(Exception):
    """Ollama could not be reached or returned an error."""


class InvalidModelOutputError(Exception):
    """The model replied, but not with a valid proposal."""


SYSTEM_PROMPT = """You are a payment recovery assistant for a subscription business.
You are given the context of one failed subscription payment. Propose exactly one
recovery action. Your answer is only a proposal; the application will validate it.

Allowed actions and their required parameters:
- schedule_retry: {"retry_after_hours": integer from 1 to 168}
- request_payment_update: {} (ask the customer for new payment details)
- send_reminder: {"tone": "friendly" or "urgent"}
- escalate_to_human: {"reason": short text}
- no_action: {"reason": short text}

Reply with JSON only, containing: payment_id (copy it from the context),
action, parameters, and summary (one or two sentences explaining the decision)."""


class OllamaDecisionMaker:
    def __init__(self, base_url=None, model=None, timeout=None):
        self.base_url = (base_url or os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.2:3b")
        self.timeout = timeout or float(os.getenv("OLLAMA_TIMEOUT", "120"))

    def propose(self, context: RecoveryContext) -> RecoveryProposal:
        payload = {
            "model": self.model,
            "stream": False,
            "format": RecoveryProposal.model_json_schema(),
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self._context_json(context)},
            ],
        }
        content = self._call_ollama(payload)

        try:
            proposal = RecoveryProposal.model_validate_json(content)
        except ValidationError as exc:
            raise InvalidModelOutputError(f"Model output failed validation: {exc}") from exc

        if proposal.payment_id != context.failed_payment.id:
            raise InvalidModelOutputError(
                "Model proposal refers to a different payment than requested"
            )
        return proposal

    @staticmethod
    def _context_json(context: RecoveryContext) -> str:
        # The model does not need the customer's name or email.
        data = context.model_dump(mode="json")
        data["customer"] = {"id": context.customer.id}
        return json.dumps(data)

    def _call_ollama(self, payload: dict) -> str:
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OllamaUnavailableError(f"Ollama request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise InvalidModelOutputError("Ollama returned a non-JSON response") from exc

        content = body.get("message", {}).get("content") if isinstance(body, dict) else None
        if not isinstance(content, str):
            raise InvalidModelOutputError("Ollama response had no message content")
        return content
