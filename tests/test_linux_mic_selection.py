"""Linux GUI selections survive PipeWire description changes; offline only."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt import gui_audio, i18n
from vlt.devices import DeviceInfo
from vlt.platform.linux import _mic_target_node


class Combo:
    tk = SimpleNamespace(splitlist=lambda values: values)

    def configure(self, *, values):
        self.values = values

    def set(self, value):
        self.value = value

    def get(self):
        return self.value

    def cget(self, key):
        assert key == 'values'
        return self.values


class SelectionTests(unittest.TestCase):
    def setUp(self):
        i18n.set_language('zh')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.yaml'
        self.before = '# keep comment\ncapture:\n  mic_device: ""\n  loopback_device: keep-loop\noutput:\n  audio:\n    device_name: keep-out\n'
        self.path.write_text(self.before, encoding='utf-8')
        self.patcher = patch('vlt.config.DEFAULT_CONFIG', self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.cfg = SimpleNamespace(output={'capture': {'mic_device': ''}})
        self.ctx = gui_audio.AudioCtx(mic_combo=Combo(), linux_fixed_audio=True)
        self.names = {}
        self.mic = DeviceInfo(index=0, name='WiVRn (microphone)', sample_rate=48000,
                              channels=1, kind='input', node_name='wivrn.source')

    def scan(self, devices):
        gui_audio.on_device_scan_result(self.ctx, self.cfg, devices, [], [], self.names)

    def test_selection_and_restart_survive_description_drift(self):
        self.scan([self.mic])
        self.ctx.mic_combo.set(self.ctx.mic_combo.values[1])
        gui_audio.on_device_change(self.ctx, self.cfg, self.names)
        saved = yaml.safe_load(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['capture']['mic_device'], 'wivrn.source')
        self.cfg = SimpleNamespace(output={'capture': saved['capture']})
        renamed = DeviceInfo(index=8, name='WiVRn', sample_rate=48000,
                             channels=1, kind='input', node_name='wivrn.source')
        self.scan([renamed])
        self.assertEqual(self.ctx.mic_combo.get(), self.ctx.mic_combo.values[1])
        self.assertIn('WiVRn', self.ctx.mic_combo.get())
        with patch('vlt.devices.enumerate_mic_devices', return_value=[renamed]):
            self.assertEqual(_mic_target_node(self.cfg.output['capture']['mic_device']),
                             'wivrn.source')

    def test_legacy_description_is_migrated_without_other_config_changes(self):
        self.cfg.output['capture']['mic_device'] = self.mic.name
        self.path.write_text(self.before.replace('mic_device: ""',
                                                'mic_device: "WiVRn (microphone)"'),
                             encoding='utf-8')
        self.scan([self.mic])
        saved = self.path.read_text(encoding='utf-8')
        self.assertEqual(yaml.safe_load(saved)['capture']['mic_device'], 'wivrn.source')
        self.assertEqual(self.cfg.output['capture']['mic_device'], 'wivrn.source')
        self.assertEqual(saved.replace('mic_device: wivrn.source', 'mic_device: ""'),
                         self.before)
        self.assertEqual(self.ctx.mic_combo.get(), self.ctx.mic_combo.values[1])

    def test_windows_keeps_device_description(self):
        self.ctx.linux_fixed_audio = False
        self.ctx.loopback_combo = Combo()
        self.ctx.audio_out_combo = Combo()
        self.scan([self.mic])
        self.assertEqual(self.names['mic'], [self.mic.name])
        self.ctx.mic_combo.set(self.ctx.mic_combo.values[1])
        gui_audio.on_device_change(self.ctx, self.cfg, self.names)
        self.assertEqual(self.cfg.output['capture']['mic_device'], self.mic.name)

    def test_no_node_or_unrecognized_legacy_keeps_existing_config(self):
        self.cfg.output['capture']['mic_device'] = 'old description'
        self.scan([self.mic])
        self.assertEqual(self.path.read_text(encoding='utf-8'), self.before)
        self.assertEqual(self.cfg.output['capture']['mic_device'], 'old description')
        no_node = DeviceInfo(index=0, name='old description', sample_rate=48000,
                             channels=1, kind='input')
        self.scan([no_node])
        self.assertEqual(self.names['mic'], ['old description'])
        self.assertEqual(self.cfg.output['capture']['mic_device'], 'old description')

    def test_ambiguous_legacy_description_is_not_migrated(self):
        self.cfg.output['capture']['mic_device'] = self.mic.name
        duplicate = DeviceInfo(index=1, name=self.mic.name, sample_rate=48000,
                               channels=1, kind='input', node_name='other.source')
        self.scan([self.mic, duplicate])
        self.assertEqual(self.path.read_text(encoding='utf-8'), self.before)
        self.assertEqual(self.cfg.output['capture']['mic_device'], self.mic.name)
        with patch('vlt.devices.enumerate_mic_devices', return_value=[self.mic, duplicate]):
            self.assertEqual(_mic_target_node(self.mic.name), '')
            from test_mic_rate import _open_mic_capture
            source = _open_mic_capture('', device_name=self.mic.name)
            self.assertEqual(source.target, '')

    def test_same_description_devices_can_be_selected_individually(self):
        duplicate = DeviceInfo(index=1, name=self.mic.name, sample_rate=48000,
                               channels=1, kind='input', node_name='other.source')
        self.scan([self.mic, duplicate])
        self.assertNotEqual(self.ctx.mic_combo.values[1], self.ctx.mic_combo.values[2])
        self.ctx.mic_combo.set(self.ctx.mic_combo.values[2])
        gui_audio.on_device_change(self.ctx, self.cfg, self.names)
        self.assertEqual(self.cfg.output['capture']['mic_device'], 'other.source')
        with patch('vlt.devices.enumerate_mic_devices', return_value=[self.mic, duplicate]):
            self.assertEqual(_mic_target_node(self.cfg.output['capture']['mic_device']),
                             'other.source')

    def test_stable_node_wins_over_another_devices_description(self):
        self.cfg.output['capture']['mic_device'] = self.mic.node_name
        collision = DeviceInfo(index=1, name=self.mic.node_name, sample_rate=48000,
                               channels=1, kind='input', node_name='other.source')
        self.scan([collision, self.mic])
        self.assertEqual(self.ctx.mic_combo.get(), self.ctx.mic_combo.values[2])
        with patch('vlt.devices.enumerate_mic_devices', return_value=[collision, self.mic]):
            self.assertEqual(_mic_target_node(self.cfg.output['capture']['mic_device']),
                             self.mic.node_name)


if __name__ == '__main__':
    unittest.main()
