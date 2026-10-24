"""Offline proof of the POST boundary; no retries or real sockets."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session import chatgpt_http as http
from vlt.session.chatgpt_relay import SubscriptionRelay


class RequestPhaseTests(unittest.TestCase):
    def test_tls_setup_and_connect_failures_are_proven_unsent(self):
        for phase in ('tls', 'connect', 'socket-timeout'):
            with self.subTest(phase=phase):
                connection = Mock()
                failure = OSError('private-transport-details')
                if phase == 'connect':
                    connection.connect.side_effect = failure
                if phase == 'socket-timeout':
                    connection.sock.settimeout.side_effect = failure
                with patch.object(http, 'tls_context', side_effect=RuntimeError('http-uncertain') if phase == 'tls' else None), \
                        patch('http.client.HTTPSConnection', return_value=connection), patch('threading.Timer'):
                    with self.assertRaises(RuntimeError) as caught:
                        http.request('live', b'private-body', {'authorization': 'Bearer private-token'})
                self.assertIs(getattr(caught.exception, 'request_sent', None), False)
                self.assertEqual(str(caught.exception), 'http-uncertain')
                self.assertIsNone(caught.exception.__cause__)
                connection.request.assert_not_called()
                if phase != 'tls':
                    connection.close.assert_called_once()

    def test_partial_post_and_response_failure_are_not_proven_unsent(self):
        for phase in ('post', 'response'):
            with self.subTest(phase=phase):
                connection = Mock()
                getattr(connection, 'request' if phase == 'post' else 'getresponse').side_effect = OSError('private')
                with patch.object(http, 'tls_context'), \
                        patch('http.client.HTTPSConnection', return_value=connection), patch('threading.Timer'):
                    with self.assertRaisesRegex(RuntimeError, '^http-uncertain$') as caught:
                        http.request('live', b'private-body', {})
                self.assertIsNot(getattr(caught.exception, 'request_sent', None), False)
                connection.request.assert_called_once()
                connection.close.assert_called_once()


class RelayPhaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_preserves_transport_proof_without_retry(self):
        owner = SimpleNamespace(generation=1, require_usable=Mock(), credentials=AsyncMock(
            return_value=SimpleNamespace(generation=1, access='private', account='private')))
        connection = Mock()
        connection.connect.side_effect = OSError('private-connect-error')
        with patch.object(http, 'tls_context'), \
                patch('http.client.HTTPSConnection', return_value=connection), patch('threading.Timer'):
            with self.assertRaisesRegex(RuntimeError, '^voice-request$') as caught:
                await SubscriptionRelay(owner)._request('live', b'private-body', {})
        self.assertIs(getattr(caught.exception, 'request_sent', None), False)
        self.assertIsNone(caught.exception.__cause__)
        connection.connect.assert_called_once()
        connection.request.assert_not_called()

    async def test_untrusted_exception_flag_is_not_transport_proof(self):
        error = RuntimeError('private')
        error.request_sent = False
        owner = SimpleNamespace(generation=1, require_usable=Mock(), credentials=AsyncMock(
            return_value=SimpleNamespace(generation=1, access='private', account='private')))
        with patch('vlt.session.chatgpt_relay.request', side_effect=error) as send:
            with self.assertRaisesRegex(RuntimeError, '^voice-request$') as caught:
                await SubscriptionRelay(owner)._request('live', b'private-body', {})
        self.assertIsNot(getattr(caught.exception, 'request_sent', None), False)
        send.assert_called_once()


if __name__ == '__main__':
    unittest.main()
