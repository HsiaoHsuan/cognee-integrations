"""``on_pre_compress``: a best-effort improve before Hermes compresses context.

Mirrors the session-end tests in ``test_lifecycle_contract.py``. Hermes calls
``on_pre_compress(messages)`` before every lossy compaction and expects a string
back (text for the summary prompt); a v1 provider is best-effort, so the hook
must never raise or make compression wait.

Run standalone with ``python3 tests/test_pre_compress.py``.
"""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _char_helpers import fake_backend, make_provider  # noqa: E402
from cognee_integration_hermes import provider as provider_mod  # noqa: E402

_MESSAGES = [{"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}]

# How long to wait for a call that should never arrive (see test_lifecycle_contract).
_NO_CALL_GRACE = 0.2


def _settle(provider, timeout=2.0):
    thread = provider._pre_compress_thread
    if thread is not None:
        thread.join(timeout=timeout)


class TestPreCompress(unittest.TestCase):
    def assert_no_improve(self, fake):
        self.assertFalse(fake.wait("improve", timeout=_NO_CALL_GRACE))
        self.assertEqual(fake.kwargs_for("improve"), [])

    def test_improve_targets_the_dataset_and_the_session(self):
        with fake_backend() as fake:
            provider = make_provider(session_cognee_id="hermes_sP")
            self.assertEqual(provider.on_pre_compress(_MESSAGES), "")
            self.assertTrue(fake.wait("improve", timeout=2.0))
            _settle(provider)
            kwargs = fake.only_call("improve")
        self.assertEqual(kwargs["dataset"], "hermes")
        self.assertEqual(kwargs["session_ids"], ["hermes_sP"])
        self.assertIs(kwargs["background"], False)

    def test_compression_does_not_wait_for_the_improve(self):
        with fake_backend() as fake:
            fake.gates["improve"] = threading.Event()
            provider = make_provider()
            started = time.monotonic()
            provider.on_pre_compress(_MESSAGES)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertTrue(fake.wait("improve", timeout=2.0))
            fake.gates["improve"].set()
            _settle(provider)

    def test_backend_failure_is_swallowed_and_logged(self):
        with fake_backend() as fake:
            fake.errors["improve"] = RuntimeError("down")
            provider = make_provider()
            with self.assertLogs(provider_mod.logger, level="WARNING") as logs:
                self.assertEqual(provider.on_pre_compress(_MESSAGES), "")
                _settle(provider)
        self.assertIn("Cognee pre-compress improve failed: down", "\n".join(logs.output))
        self.assertEqual(provider._consecutive_failures, 1)

    def test_an_unexpected_error_never_reaches_hermes(self):
        with fake_backend():
            provider = make_provider()
            provider._is_breaker_open = mock.Mock(side_effect=RuntimeError("boom"))
            with self.assertLogs(provider_mod.logger, level="WARNING"):
                self.assertEqual(provider.on_pre_compress(_MESSAGES), "")

    def test_a_running_improve_is_not_duplicated(self):
        with fake_backend() as fake:
            fake.gates["improve"] = threading.Event()
            provider = make_provider()
            provider.on_pre_compress(_MESSAGES)
            self.assertTrue(fake.wait("improve", timeout=2.0))
            provider.on_pre_compress(_MESSAGES)
            fake.gates["improve"].set()
            _settle(provider)
            self.assertEqual(len(fake.kwargs_for("improve")), 1)

    def test_the_session_end_improve_stays_armed(self):
        # More turns follow a compaction, so the crash-safe watcher must still
        # improve at exit; only the session-end bridge may disable that.
        with fake_backend() as fake:
            provider = make_provider(watcher_state_path=Path("/tmp/w.json"))
            with mock.patch.object(provider_mod.exit_watcher, "update") as update:
                provider.on_pre_compress(_MESSAGES)
                self.assertTrue(fake.wait("improve", timeout=2.0))
                _settle(provider)
            update.assert_not_called()

    def test_empty_messages_is_a_no_op(self):
        with fake_backend() as fake:
            provider = make_provider()
            self.assertEqual(provider.on_pre_compress([]), "")
            self.assert_no_improve(fake)

    def test_no_session_is_a_no_op(self):
        with fake_backend() as fake:
            provider = make_provider()
            provider._session_cognee_id = ""
            self.assertEqual(provider.on_pre_compress(_MESSAGES), "")
            self.assert_no_improve(fake)

    def test_gated_like_the_session_end_improve(self):
        for kwargs in ({"writes_enabled": False}, {"improve_on_end": False}):
            with self.subTest(**kwargs), fake_backend() as fake:
                provider = make_provider(**kwargs)
                self.assertEqual(provider.on_pre_compress(_MESSAGES), "")
                self.assert_no_improve(fake)

    def test_open_breaker_skips_it(self):
        with fake_backend() as fake:
            provider = make_provider()
            provider._is_breaker_open = lambda: True
            provider.on_pre_compress(_MESSAGES)
            self.assert_no_improve(fake)


if __name__ == "__main__":
    unittest.main()
