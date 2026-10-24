"""Shared verified TLS setup must not reload native certificate stores per request."""
import asyncio
import http.client as client
import ssl
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session import chatgpt_http as http
from vlt.session.chatgpt_voice import NativeVoice


class TLSTests(unittest.TestCase):
    def setUp(self):
        self.built, self.contexts = [], []
        self.original = client.HTTPSConnection.__init__
        self.observed_init = lambda connection, *args, **kwargs: self.connection(connection,*args,**kwargs)
        self.cache = patch.object(http, '_TLS_CONTEXT', None, create=True)
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def factory(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.contexts.append(context)
        return context

    def connection(self, connection, *args, **kwargs):
        self.original(connection,*args,**kwargs)
        response = Mock(status=200, length=None, headers=Message(), will_close=True)
        response.read1.side_effect = [b'ok', b'']
        connection.connect = lambda: setattr(connection, 'sock', Mock())
        connection.request = Mock()
        connection.getresponse = Mock(return_value=response)
        connection.close = Mock()
        self.built.append(connection)

    def test_fixed_hosts_share_one_verified_context_and_keep_separate_connections(self):
        with patch('ssl.create_default_context', side_effect=self.factory) as factory, \
             patch('ssl._create_default_https_context', side_effect=factory), \
             patch('http.client.HTTPSConnection.__init__', new=self.observed_init), patch('threading.Timer'):
            for operation in ('refresh','transcribe','responses','transcribe'):
                self.assertEqual(http.request(operation,b'data',{}),b'ok')
        self.assertEqual(factory.call_count,1)
        self.assertEqual(len(self.built),4)
        self.assertEqual({c.host for c in self.built},{'auth.openai.com','chatgpt.com'})
        for connection in self.built:
            self.assertIs(connection._context,self.contexts[0])
            self.assertTrue(connection._context.check_hostname)
            self.assertEqual(connection._context.verify_mode,ssl.CERT_REQUIRED)
            connection.close.assert_called_once()

    def test_simultaneous_bidirectional_requests_initialize_once(self):
        entered, release = threading.Event(), threading.Event()
        def delayed_factory():
            entered.set()
            self.assertTrue(release.wait(3))
            return self.factory()
        with patch('ssl.create_default_context', side_effect=delayed_factory) as factory, \
             patch('ssl._create_default_https_context', side_effect=factory), \
             patch('http.client.HTTPSConnection.__init__', new=self.observed_init), patch('threading.Timer'):
            with ThreadPoolExecutor(max_workers=4) as pool:
                jobs = [pool.submit(http.request,'transcribe',b'data',{}) for _ in range(4)]
                try:
                    self.assertTrue(entered.wait(3))
                finally:
                    release.set()
                self.assertEqual([job.result(3) for job in jobs],[b'ok']*4)
        self.assertEqual(factory.call_count,1)
        self.assertTrue(all(c._context is self.contexts[0] for c in self.built))

    def test_failed_context_creation_is_sanitized_and_not_cached(self):
        with patch('ssl.create_default_context', side_effect=OSError('private-cert-path')) as factory, \
             patch('ssl._create_default_https_context', side_effect=factory), \
             patch('http.client.HTTPSConnection.__init__', new=self.observed_init):
            with self.assertRaises(Exception) as caught:
                http.request('transcribe',b'data',{})
        self.assertIsInstance(caught.exception,RuntimeError)
        self.assertEqual(str(caught.exception),'http-uncertain')
        self.assertIsNone(http._TLS_CONTEXT)
        self.assertEqual(self.built,[])
        with patch('ssl.create_default_context', side_effect=self.factory), \
             patch('ssl._create_default_https_context', side_effect=self.factory), \
             patch('http.client.HTTPSConnection.__init__', new=self.observed_init), patch('threading.Timer'):
            self.assertEqual(http.request('transcribe',b'data',{}),b'ok')
        self.assertIs(http._TLS_CONTEXT,self.contexts[-1])

    def test_invalid_destination_is_rejected_before_tls_setup(self):
        with patch('ssl.create_default_context') as factory, \
             patch('ssl._create_default_https_context', side_effect=factory):
            with self.assertRaisesRegex(RuntimeError,'^request-invalid$'):
                http.request('https://evil.example',b'data',{})
        factory.assert_not_called()

    def test_voice_sideband_reuses_verified_context_without_native_ca_reload(self):
        context = self.factory()
        async def probe():
            voice = NativeVoice(SimpleNamespace(owner=SimpleNamespace(generation=1)))
            ready = asyncio.get_running_loop().create_future()
            ready.set_result(Mock())
            with patch.object(http,'_TLS_CONTEXT',context,create=True), \
                 patch('vlt.session.chatgpt_voice.client.connect',return_value=ready) as connect:
                await voice._connect('wss://api.openai.com/v1/live/test',
                    SimpleNamespace(access='fake-access',account='fake-account'))
                self.assertIs(connect.call_args.kwargs.get('ssl'),context)
                self.assertIs(connect.call_args.kwargs['proxy'],None)
        asyncio.run(probe())


if __name__ == '__main__':
    unittest.main()
