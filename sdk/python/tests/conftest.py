"""
Shared pytest fixtures for the Alert Manager SDK test suite. Mirrors
tests/test_email_pathway.py's own conventions in the main alertManager
repo: a `mocker`-based response-builder helper, and an autouse fixture
patching `time.sleep` so retry-backoff tests never actually sleep.
"""

import json

import pytest


@pytest.fixture(autouse=True)
def _no_real_sleep(mocker):
    """Patches time.sleep in _http's own namespace so retry-backoff tests (2s/8s schedule) run instantly."""
    return mocker.patch("alertmanager_sdk._http.time.sleep")


@pytest.fixture
def make_response(mocker):
    """
    Factory fixture: builds a mocker.MagicMock standing in for a
    requests.Response, with `.status_code`/`.headers`/`.text`/`.json()`
    set consistently. Mirrors test_email_pathway.py's `_response()`
    helper, extended with a `.json()` mock since this SDK parses JSON
    bodies (unlike Graph's plain-text error bodies).
    """

    def _make(status_code, json_body=None, headers=None, text=None):
        resp = mocker.MagicMock()
        resp.status_code = status_code
        resp.headers = headers or {}
        if json_body is not None:
            resp.json.return_value = json_body
            resp.text = text if text is not None else json.dumps(json_body)
        else:
            resp.json.side_effect = ValueError("no JSON body")
            resp.text = text if text is not None else ""
        return resp

    return _make


@pytest.fixture
def fake_session(mocker):
    """A mocker.MagicMock(spec=requests.Session) -- .post()/.get() are asserted against directly in tests."""
    import requests

    return mocker.MagicMock(spec=requests.Session)
