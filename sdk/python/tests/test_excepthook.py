"""
tests/test_excepthook.py -- sys.excepthook / threading.excepthook
installation, chaining, REPL-skip behavior, and the scoped
context-manager/decorator alternatives.
"""

import sys
import threading
import time

import pytest

from alertmanager_sdk import excepthook as excepthook_module
from alertmanager_sdk.excepthook import (
    exception_hook_scope,
    install_exception_hooks,
    report_exceptions,
    uninstall_exception_hooks,
)


@pytest.fixture(autouse=True)
def _restore_hooks():
    """Every test gets a clean slate and leaves one behind, regardless of outcome."""
    original_sys_hook = sys.excepthook
    original_threading_hook = threading.excepthook
    yield
    uninstall_exception_hooks()
    sys.excepthook = original_sys_hook
    threading.excepthook = original_threading_hook
    excepthook_module._installed = False
    excepthook_module._previous_sys_excepthook = None
    excepthook_module._previous_threading_excepthook = None


@pytest.fixture
def fake_client(mocker):
    client = mocker.MagicMock()
    client.default_source = "test-source"
    return client


class TestInstall:
    def test_installs_both_hooks(self, fake_client):
        install_exception_hooks(fake_client)

        assert sys.excepthook is not sys.__excepthook__
        assert threading.excepthook is not threading.__excepthook__

    def test_reports_main_thread_uncaught_exception(self, fake_client):
        install_exception_hooks(fake_client)

        try:
            raise ValueError("boom")
        except ValueError:
            exc_type, exc_value, exc_tb = sys.exc_info()

        sys.excepthook(exc_type, exc_value, exc_tb)

        fake_client.send.assert_called_once()
        kwargs = fake_client.send.call_args.kwargs
        assert kwargs["level"] == "critical"
        assert kwargs["category"] == "application"
        assert "ValueError" in kwargs["title"]
        assert "boom" in kwargs["message"]

    def test_reports_background_thread_uncaught_exception(self, fake_client):
        """
        The genuinely separate gap: sys.excepthook alone does not see
        thread exceptions at all. This proves threading.excepthook does.
        """
        install_exception_hooks(fake_client)

        def _raises():
            raise RuntimeError("thread boom")

        t = threading.Thread(target=_raises, name="worker-1")
        t.start()
        t.join()
        time.sleep(0.05)  # let the background excepthook callback run

        fake_client.send.assert_called_once()
        kwargs = fake_client.send.call_args.kwargs
        assert "RuntimeError" in kwargs["title"]
        assert "worker-1" in kwargs["title"]

    def test_chains_previously_installed_sys_excepthook(self, fake_client, mocker):
        previous_hook = mocker.MagicMock()
        sys.excepthook = previous_hook

        install_exception_hooks(fake_client)

        try:
            raise ValueError("boom")
        except ValueError:
            exc_type, exc_value, exc_tb = sys.exc_info()
        sys.excepthook(exc_type, exc_value, exc_tb)

        previous_hook.assert_called_once_with(exc_type, exc_value, exc_tb)

    def test_skips_installation_in_interactive_session(self, fake_client, mocker):
        mocker.patch("alertmanager_sdk.excepthook._is_interactive", return_value=True)
        original_hook = sys.excepthook

        install_exception_hooks(fake_client)

        assert sys.excepthook is original_hook

    def test_reporting_failure_does_not_prevent_chained_hook_from_running(self, fake_client, mocker):
        fake_client.send.side_effect = RuntimeError("network is down")
        previous_hook = mocker.MagicMock()
        sys.excepthook = previous_hook

        install_exception_hooks(fake_client)

        try:
            raise ValueError("boom")
        except ValueError:
            exc_type, exc_value, exc_tb = sys.exc_info()
        sys.excepthook(exc_type, exc_value, exc_tb)  # must not raise

        previous_hook.assert_called_once_with(exc_type, exc_value, exc_tb)


class TestUninstall:
    def test_restores_pre_install_hooks(self, fake_client):
        original_sys_hook = sys.excepthook
        original_threading_hook = threading.excepthook

        install_exception_hooks(fake_client)
        uninstall_exception_hooks()

        assert sys.excepthook is original_sys_hook
        assert threading.excepthook is original_threading_hook

    def test_uninstall_without_install_is_a_noop(self):
        uninstall_exception_hooks()  # must not raise


class TestExceptionHookScope:
    def test_installs_only_inside_the_block(self, fake_client):
        original_hook = sys.excepthook

        with exception_hook_scope(fake_client):
            assert sys.excepthook is not original_hook

        assert sys.excepthook is original_hook

    def test_restores_on_exception_exit(self, fake_client):
        original_hook = sys.excepthook

        with pytest.raises(RuntimeError):
            with exception_hook_scope(fake_client):
                raise RuntimeError("inside the block")

        assert sys.excepthook is original_hook


class TestReportExceptionsDecorator:
    def test_reports_and_reraises(self, fake_client):
        @report_exceptions(fake_client)
        def flaky():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            flaky()

        fake_client.send.assert_called_once()
        assert fake_client.send.call_args.kwargs["level"] == "critical"

    def test_success_path_does_not_report(self, fake_client):
        @report_exceptions(fake_client)
        def fine():
            return 42

        assert fine() == 42
        fake_client.send.assert_not_called()

    def test_custom_level_and_category_honored(self, fake_client):
        @report_exceptions(fake_client, level="warning", category="business")
        def flaky():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            flaky()

        kwargs = fake_client.send.call_args.kwargs
        assert kwargs["level"] == "warning"
        assert kwargs["category"] == "business"
