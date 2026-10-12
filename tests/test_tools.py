import pytest

from app.llm.tools import (
    TOOLS,
    InvalidToolArgumentsError,
    Tool,
    ToolExecutionError,
    UnknownToolError,
    NoArgs,
    dispatch,
    tool_schemas,
)

EXPECTED = {"schedule_retry", "request_payment_update", "send_reminder",
            "escalate_to_human", "no_action"}


def test_allowlist_is_exactly_the_five_tools():
    assert set(TOOLS) == EXPECTED


def test_schemas_never_expose_payment_id():
    schemas = tool_schemas()
    assert {s["function"]["name"] for s in schemas} == EXPECTED
    for s in schemas:
        assert "payment_id" not in s["function"]["parameters"].get("properties", {})


@pytest.mark.parametrize("name,args", [
    ("schedule_retry", {"retry_after_hours": 24}),
    ("request_payment_update", {}),
    ("send_reminder", {"tone": "friendly"}),
    ("escalate_to_human", {"reason": "suspected fraud"}),
    ("no_action", {"reason": "customer contacted support"}),
])
def test_valid_calls_return_simulated_results(name, args):
    result = dispatch(name, args, "pay_1")
    assert result["simulated"] is True
    assert result["tool"] == name
    assert result["payment_id"] == "pay_1"
    assert "No real" in result["note"]


def test_digit_string_hours_are_converted_to_int():
    result = dispatch("schedule_retry", {"retry_after_hours": "24"}, "pay_1")
    assert "24 hours" in result["detail"]


def test_json_string_arguments_are_accepted():
    assert dispatch("schedule_retry", '{"retry_after_hours": 5}', "pay_1")["simulated"]
    assert dispatch("request_payment_update", None, "pay_1")["simulated"]


@pytest.mark.parametrize("name", ["charge_card", "send_email", "", None, 5, "SCHEDULE_RETRY",
                                  "__import__('os').system('x')"])
def test_unknown_tools_rejected(name):
    with pytest.raises(UnknownToolError):
        dispatch(name, {}, "pay_1")


@pytest.mark.parametrize("name,args", [
    ("schedule_retry", {}),                                         # missing
    ("schedule_retry", {"retry_after_hours": 0}),                   # range
    ("schedule_retry", {"retry_after_hours": 999}),                 # range
    ("schedule_retry", {"retry_after_hours": "24.5"}),              # decimal string
    ("schedule_retry", {"retry_after_hours": "twenty-four"}),       # word
    ("schedule_retry", {"retry_after_hours": " 24"}),               # padded string
    ("schedule_retry", {"retry_after_hours": "0"}),                 # digit string out of range
    ("schedule_retry", {"retry_after_hours": "99999"}),             # too long
    ("schedule_retry", {"retry_after_hours": 24.0}),                # float
    ("schedule_retry", {"retry_after_hours": True}),                # bool
    ("schedule_retry", {"retry_after_hours": 24, "amount": 5}),     # extra key
    ("send_reminder", {"tone": "threatening"}),                     # enum
    ("escalate_to_human", {"reason": ""}),                          # empty
    ("no_action", {}),                                              # missing
    ("request_payment_update", {"x": 1}),                           # extra key
    ("schedule_retry", "not json"),                                 # bad JSON
    ("schedule_retry", [1, 2]),                                     # not an object
])
def test_malformed_arguments_rejected(name, args):
    with pytest.raises(InvalidToolArgumentsError):
        dispatch(name, args, "pay_1")


def test_model_supplied_payment_id_must_match_bound_id():
    assert dispatch("no_action", {"reason": "x", "payment_id": "pay_1"}, "pay_1")["payment_id"] == "pay_1"
    with pytest.raises(InvalidToolArgumentsError):
        dispatch("no_action", {"reason": "x", "payment_id": "pay_other"}, "pay_1")


def test_result_uses_application_payment_id():
    result = dispatch("no_action", {"reason": "x", "payment_id": "pay_1"}, "pay_1")
    assert result["payment_id"] == "pay_1"


def test_tool_function_failure_is_wrapped():
    def boom(payment_id, args):
        raise RuntimeError("secret internal detail")

    tools = {"boom": Tool("boom", "d", NoArgs, boom)}
    with pytest.raises(ToolExecutionError) as exc:
        dispatch("boom", {}, "pay_1", tools)
    assert "secret internal detail" not in str(exc.value)
