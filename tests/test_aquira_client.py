from app.integrations.aquira.client import AquiraApiError, unwrap_field_value, validate_response


def test_unwrap_field_value_extracts_value():
    payload = {"Value": "Acme Corp", "Valid": True, "Label": "Company Name", "Access": 2}
    assert unwrap_field_value(payload) == "Acme Corp"


def test_unwrap_field_value_passthrough_for_plain_value():
    assert unwrap_field_value("Plain string") == "Plain string"


def test_validate_response_accepts_successful_envelope():
    payload = {"Success": True, "Error": 0, "Data": [{"ID": 123}], "name": "ok"}
    assert validate_response(payload) == payload


def test_validate_response_rejects_failed_envelope():
    payload = {"Success": False, "Error": -16, "Errors": "Name is required", "name": "bad"}
    try:
        validate_response(payload)
        raise AssertionError("validate_response should have raised AquiraApiError")
    except AquiraApiError as exc:
        assert "Name is required" in str(exc)
