"""
tests/test_validation.py -- validate_alert()'s field-by-field rules,
mirrored exactly from src/alert_router.py's own validate_alert() in the
main alertManager repo. One class per rule-group, per the house convention
for validation-rule-style modules.
"""

import pytest

from alertmanager_sdk.exceptions import AlertManagerValidationError
from alertmanager_sdk.validation import is_valid_alert, validate_alert

VALID_ALERT = {
    "timestamp": 1732982400,
    "level": "info",
    "category": "application",
    "title": "Status",
    "message": "OK",
    "source": "test-suite",
}


def _alert(**overrides):
    alert = dict(VALID_ALERT)
    alert.update(overrides)
    return alert


class TestRequiredFields:
    @pytest.mark.parametrize("field", ["timestamp", "level", "category", "title", "message", "source"])
    def test_missing_field_raises(self, field):
        alert = _alert()
        del alert[field]
        with pytest.raises(AlertManagerValidationError, match=field):
            validate_alert(alert)


class TestTimestamp:
    def test_valid_int_passes(self):
        validate_alert(_alert(timestamp=1732982400))

    def test_float_rejected(self):
        with pytest.raises(AlertManagerValidationError, match="timestamp"):
            validate_alert(_alert(timestamp=1732982400.123))

    def test_string_rejected(self):
        with pytest.raises(AlertManagerValidationError, match="timestamp"):
            validate_alert(_alert(timestamp="2024-01-15T10:30:00Z"))

    def test_bool_rejected_deliberately_stricter_than_server(self):
        """
        bool is an int subclass in Python, so the server's own naive
        isinstance(x, int) check would technically accept True/False as a
        timestamp. This SDK is deliberately stricter -- see
        validation.py's module docstring.
        """
        with pytest.raises(AlertManagerValidationError, match="timestamp"):
            validate_alert(_alert(timestamp=True))


class TestSource:
    def test_non_string_rejected(self):
        with pytest.raises(AlertManagerValidationError, match="source"):
            validate_alert(_alert(source={"name": "x"}))


class TestLevel:
    def test_non_string_rejected(self):
        with pytest.raises(AlertManagerValidationError, match="level"):
            validate_alert(_alert(level=1))

    @pytest.mark.parametrize("level", ["critical", "warning", "notice", "info", "debug"])
    def test_canonical_levels_accepted(self, level):
        result = validate_alert(_alert(level=level))
        assert result["level"] == level

    @pytest.mark.parametrize(
        "alias,expected",
        [
            ("error", "critical"),
            ("err", "critical"),
            ("crit", "critical"),
            ("warn", "warning"),
            ("inf", "info"),
            ("dbg", "debug"),
        ],
    )
    def test_aliases_resolve(self, alias, expected):
        result = validate_alert(_alert(level=alias))
        assert result["level"] == expected

    def test_trim_and_lowercase(self):
        result = validate_alert(_alert(level=" ERROR "))
        assert result["level"] == "critical"

    def test_invalid_level_raises_with_valid_list_in_message(self):
        with pytest.raises(AlertManagerValidationError, match="critical"):
            validate_alert(_alert(level="bogus"))


class TestCategory:
    def test_non_string_rejected(self):
        with pytest.raises(AlertManagerValidationError, match="category"):
            validate_alert(_alert(category=1))

    @pytest.mark.parametrize(
        "category",
        [
            "security",
            "system",
            "weather",
            "automation",
            "personal",
            "network",
            "application",
            "news",
            "business",
            "hardware",
            "environmental",
        ],
    )
    def test_all_valid_categories_accepted(self, category):
        result = validate_alert(_alert(category=category))
        assert result["category"] == category

    def test_trim_and_lowercase(self):
        result = validate_alert(_alert(category=" SYSTEM "))
        assert result["category"] == "system"

    def test_invalid_category_raises(self):
        with pytest.raises(AlertManagerValidationError, match="business"):
            validate_alert(_alert(category="bogus"))


class TestClient:
    def test_absent_left_alone(self):
        alert = _alert()
        assert "client" not in alert
        result = validate_alert(alert)
        assert "client" not in result

    def test_none_left_alone(self):
        result = validate_alert(_alert(client=None))
        assert result["client"] is None

    def test_non_string_rejected(self):
        with pytest.raises(AlertManagerValidationError, match="client"):
            validate_alert(_alert(client=123))

    def test_trim_and_lowercase(self):
        result = validate_alert(_alert(client=" ProdSecurity "))
        assert result["client"] == "prodsecurity"


class TestTitleAndMessage:
    """
    Explicit "prove the absence of a feature" tests: neither the server
    nor this mirror validates title/message content at all. Easy for a
    future contributor to "fix" by adding validation that doesn't match
    the server -- these tests exist to catch that.
    """

    def test_empty_strings_pass(self):
        validate_alert(_alert(title="", message=""))

    def test_non_string_title_and_message_pass_through_unvalidated(self):
        # Deliberately not type-checked, matching the server exactly.
        validate_alert(_alert(title=123, message=None))


class TestNoMutationOfInput:
    def test_original_dict_is_unmodified(self):
        original = _alert(level=" ERROR ", category=" SYSTEM ", client=" ProdSecurity ")
        snapshot = dict(original)

        result = validate_alert(original)

        assert original == snapshot
        assert result != original


class TestIsValidAlert:
    def test_true_for_valid_alert(self):
        assert is_valid_alert(_alert()) is True

    def test_false_for_invalid_alert(self):
        assert is_valid_alert(_alert(level="bogus")) is False
