"""Normal native WS close handshakes finish TLS without waiting for the idle TCP timeout."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from websockets.client import ClientProtocol
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.protocol import OPEN
from websockets.uri import parse_uri

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_voice import NativeVoice, client


class VoiceShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def connection(self):
        class Attempt:
            def __await__(self):
                async def opened():
                    return SimpleNamespace()
                return opened().__await__()

        voice = NativeVoice(SimpleNamespace(owner=SimpleNamespace(generation=0)))
        with patch.object(client, 'connect', return_value=Attempt()) as connect:
            await voice._connect('wss://api.openai.com/v1/live/test',
                                 SimpleNamespace(access='offline-token', account='offline-account'))
        factory = connect.call_args.kwargs.get('create_connection', client.ClientConnection)
        protocol = ClientProtocol(parse_uri('wss://api.openai.com/v1/live/test'), state=OPEN)
        connection = factory(protocol, close_timeout=2)
        transport = Mock(spec=asyncio.Transport)
        transport.can_write_eof.return_value = False
        connection.connection_made(transport)
        connection.process_event(Response(101, 'Switching Protocols', Headers()))
        self.addCleanup(connection.connection_lost, None)
        return connection, transport

    async def test_normal_close_receipt_finishes_transport_without_aborting(self):
        connection, transport = await self.connection()
        connection.protocol.send_close(1000)
        connection.send_data()
        connection.data_received(b'\x88\x02\x03\xe8')
        self.assertEqual(connection.protocol.close_rcvd.code, 1000)
        self.assertEqual(connection.protocol.close_sent.code, 1000)
        transport.close.assert_called_once()
        transport.abort.assert_not_called()

    async def test_peer_first_close_keeps_existing_drain_and_failure_order(self):
        connection, transport = await self.connection()
        transport.reset_mock()
        connection.data_received(b'\x88\x02\x03\xe8')
        self.assertEqual(connection.protocol.close_sent.code, 1000)
        self.assertTrue(connection.protocol.close_rcvd_then_sent)
        transport.write.assert_called_once()
        transport.close.assert_not_called()
        transport.abort.assert_not_called()

    async def test_missing_abnormal_or_malformed_close_keeps_bounded_failure_path(self):
        for frame in (b'\x81\x02ok', b'\x88\x02\x03\xe9', b'\x88\x01\x00'):
            with self.subTest(frame=frame):
                connection, transport = await self.connection()
                connection.data_received(frame)
                transport.close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
