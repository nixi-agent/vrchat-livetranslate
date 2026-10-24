"""Monitor hot-open and process fallback must honor the active capture endpoint."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt import engine, gui_audio
from vlt.gui import TranslationGUI
from vlt.output.earmonitor import EarMonitor
from vlt.output.micproxy import MicProxy


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.monitors = []
        self.actual_conflict = engine.monitor_capture_conflict
        self.sound = SimpleNamespace(OutputStream=Mock(side_effect=lambda **k:
            SimpleNamespace(start=Mock(), stop=Mock(), close=Mock())),
            query_devices=lambda index=None,**k:{'name':'Headset' if index == 1 else 'Speakers'})
        for item in [patch.dict(sys.modules, sounddevice=self.sound),
                     patch.object(engine.platform, 'IS_WINDOWS', True),
                     patch.object(engine.platform, 'IS_LINUX', False),
                     patch('vlt.devices.resolve_device_name', side_effect=lambda n,*a:1 if n=='Headset' else 0),
                     patch.object(engine.platform, 'output_device_fallbacks', return_value=[]),
                     patch.object(engine, 'monitor_capture_conflict', side_effect=lambda m,c:
                        m.get('device_name') == c.get('loopback_device','').replace(' [Loopback]',''))]:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(lambda:[m.close() for m in self.monitors])

    def monitor(self, name):
        value = EarMonitor(name)
        self.monitors.append(value)
        return value

    def target(self, name='Speakers'):
        return engine.LoopbackTarget(id='9', name=name+' [Loopback]', sample_rate=48000, channels=2)

    async def test_gui_hot_enable_is_blocked_until_same_endpoint_capture_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def pump(*a,**k):
            entered.set()
            await release.wait()
            return 0
        audio = {'monitor':{'enabled':False,'device_name':'Speakers','volume':70}}
        proxy = MicProxy(audio_cfg=audio)
        proxy._opened = True
        self.addCleanup(proxy.close)
        gui = SimpleNamespace(_sync_audio_ctx=lambda:None, _monitor_var=SimpleNamespace(get=lambda:True),
            _monitor_vol_var=None, _vmic_var=SimpleNamespace(get=lambda:True), _proxy=proxy,
            _cfg=SimpleNamespace(output={'audio':audio}), _audio_ctx=SimpleNamespace(), _set_status=Mock())
        with patch.object(engine, 'pick_loopback_target', return_value=self.target()), \
             patch.object(engine, '_pump_capture', side_effect=pump), \
             patch.object(gui_audio, 'save_monitor_config'):
            task = asyncio.create_task(engine._run_loopback_endpoint(SimpleNamespace(),None))
            try:
                await asyncio.wait_for(entered.wait(),1)
                TranslationGUI._on_monitor_change(gui)
                self.assertFalse(proxy.monitor is not None and proxy.monitor.opened,
                                 'GUI hot-enable cannot play into an active endpoint capture')
            finally:
                release.set()
                await task
        self.assertTrue(proxy.restart_monitor(enabled=True,device_name='Speakers',volume=70),
                        'after capture closes, hot-enable is available again')

    async def test_process_failure_stops_conflicting_monitor_before_endpoint_pump(self):
        monitor = self.monitor('Speakers')
        self.assertTrue(monitor.open())
        async def pump(*a,**k):
            self.assertFalse(monitor.opened, 'stop monitor before opening fallback capture')
            return 0
        with patch.object(engine, '_wait_for_vrchat_process', AsyncMock(return_value=123)), \
             patch.object(engine.platform, 'open_process_loopback', side_effect=RuntimeError('fake activation failure')), \
             patch.object(engine, 'pick_loopback_target', return_value=self.target()), \
             patch.object(engine, '_pump_capture', side_effect=pump):
            await engine._run_loopback_process(SimpleNamespace(),None,seconds=.02)

    async def test_different_monitor_endpoint_remains_available(self):
        monitor = self.monitor('Headset')
        async def pump(*a,**k):
            self.assertTrue(monitor.open())
            self.assertTrue(monitor.opened)
            return 0
        with patch.object(engine, 'pick_loopback_target', return_value=self.target()), \
             patch.object(engine, '_pump_capture', side_effect=pump):
            await engine._run_loopback_endpoint(SimpleNamespace(),None)
        self.assertTrue(monitor.opened)

    async def test_conflict_resolves_the_configured_capture_device(self):
        def choose(patterns=None, device_name=None):
            return self.target('Speakers' if device_name == 'Speakers [Loopback]' else 'Other')
        with patch.object(engine, 'pick_loopback_target', side_effect=choose), \
             patch.object(engine, 'resolve_device_name', return_value=0), \
             patch.object(engine, '_output_name_of', return_value='Speakers'):
            self.assertTrue(self.actual_conflict({'enabled':True,'device_name':'Speakers'},
                                                 {'loopback_device':'Speakers [Loopback]'}))

    async def test_cancelled_capture_releases_its_monitor_block(self):
        entered = asyncio.Event()
        async def pump(*a,**k):
            entered.set()
            await asyncio.Event().wait()
        monitor = self.monitor('Speakers')
        with patch.object(engine, 'pick_loopback_target', return_value=self.target()), \
             patch.object(engine, '_pump_capture', side_effect=pump):
            task = asyncio.create_task(engine._run_loopback_endpoint(SimpleNamespace(),None))
            try:
                await asyncio.wait_for(entered.wait(),1)
                self.assertFalse(monitor.open())
            finally:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
        self.assertTrue(monitor.open())

    async def test_known_active_endpoint_is_blocked_even_if_device_enumeration_fails(self):
        monitor = self.monitor('Speakers')
        async def pump(*a,**k):
            self.assertFalse(monitor.open())
            return 0
        with patch.object(engine, 'monitor_capture_conflict', return_value=False), \
             patch.object(engine, 'pick_loopback_target', return_value=self.target()), \
             patch.object(engine, '_pump_capture', side_effect=pump):
            await engine._run_loopback_endpoint(SimpleNamespace(),None)


if __name__ == '__main__':
    unittest.main()
