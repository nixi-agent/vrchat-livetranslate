"""设备枚举与名称解析测试（注入假设备列表，无需真实硬件）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vlt.devices import (
    DeviceInfo,
    enumerate_audio_out_devices,
    enumerate_loopback_devices,
    enumerate_mic_devices,
    format_device_display,
    resolve_device_name,
)

FAKE_SD_DEVICES = [
    {"name": "Microphone (Realtek Audio)", "max_input_channels": 2,
     "max_output_channels": 0, "default_samplerate": 48000},
    {"name": "Steam Streaming Speakers", "max_input_channels": 2,
     "max_output_channels": 0, "default_samplerate": 48000},
    {"name": "VoiceMeeter Input (VB-Audio)", "max_input_channels": 0,
     "max_output_channels": 2, "default_samplerate": 48000},
    {"name": "CABLE Input (VB-Audio)", "max_input_channels": 0,
     "max_output_channels": 2, "default_samplerate": 44100},
    {"name": "Speakers (Realtek Audio)", "max_input_channels": 0,
     "max_output_channels": 6, "default_samplerate": 48000},
]

FAKE_LOOPBACK_DEVICES = [
    {"index": 10, "name": "Steam Streaming Speakers (loopback)",
     "defaultSampleRate": 48000, "maxInputChannels": 2},
    {"index": 11, "name": "Speakers (Realtek Audio) (loopback)",
     "defaultSampleRate": 48000, "maxInputChannels": 6},
]


class TestResolveExact(unittest.TestCase):
    def test_exact_match_input(self):
        idx = resolve_device_name(
            "Microphone (Realtek Audio)", "input", devices=FAKE_SD_DEVICES)
        self.assertEqual(idx, 0)

    def test_exact_match_output(self):
        idx = resolve_device_name(
            "VoiceMeeter Input (VB-Audio)", "output", devices=FAKE_SD_DEVICES)
        self.assertEqual(idx, 2)


class TestResolveCaseInsensitive(unittest.TestCase):
    def test_case_insensitive(self):
        idx = resolve_device_name(
            "steam streaming speakers", "input", devices=FAKE_SD_DEVICES)
        self.assertEqual(idx, 1)

    def test_case_insensitive_output(self):
        idx = resolve_device_name(
            "voiceMEETER input (vb-audio)", "output", devices=FAKE_SD_DEVICES)
        self.assertEqual(idx, 2)


class TestResolveSubstring(unittest.TestCase):
    def test_substring_match(self):
        idx = resolve_device_name("realtek", "input", devices=FAKE_SD_DEVICES)
        self.assertEqual(idx, 0)

    def test_substring_match_output(self):
        idx = resolve_device_name("cable", "output", devices=FAKE_SD_DEVICES)
        self.assertEqual(idx, 3)


class TestResolveNotFound(unittest.TestCase):
    def test_returns_none(self):
        idx = resolve_device_name("nonexistent device", "input",
                                  devices=FAKE_SD_DEVICES)
        self.assertIsNone(idx)

    def test_does_not_raise(self):
        idx = resolve_device_name("nope", "output", devices=FAKE_SD_DEVICES)
        self.assertIsNone(idx)


class TestResolveSpecialChars(unittest.TestCase):
    def test_parentheses(self):
        idx = resolve_device_name(
            "Microphone (Realtek Audio)", "input", devices=FAKE_SD_DEVICES)
        self.assertEqual(idx, 0)

    def test_plus_sign(self):
        fake = [
            {"name": "Device A+B", "max_input_channels": 1,
             "max_output_channels": 0, "default_samplerate": 48000},
        ]
        idx = resolve_device_name("Device A+B", "input", devices=fake)
        self.assertEqual(idx, 0)

    def test_brackets(self):
        fake = [
            {"name": "Device [USB]", "max_input_channels": 1,
             "max_output_channels": 0, "default_samplerate": 48000},
        ]
        idx = resolve_device_name("Device [USB]", "input", devices=fake)
        self.assertEqual(idx, 0)

    def test_no_regex_injection(self):
        fake = [
            {"name": "Normal Device", "max_input_channels": 1,
             "max_output_channels": 0, "default_samplerate": 48000},
        ]
        idx = resolve_device_name(".*", "input", devices=fake)
        self.assertIsNone(idx)


class TestResolveEmpty(unittest.TestCase):
    def test_empty_string(self):
        idx = resolve_device_name("", "input", devices=FAKE_SD_DEVICES)
        self.assertIsNone(idx)

    def test_none_like_empty(self):
        idx = resolve_device_name("", "output", devices=FAKE_SD_DEVICES)
        self.assertIsNone(idx)


class TestFormatDisplay(unittest.TestCase):
    def test_with_sample_rate(self):
        info = DeviceInfo(index=0, name="Steam Streaming Speakers",
                          sample_rate=48000, channels=2, kind="input")
        self.assertEqual(format_device_display(info),
                         "Steam Streaming Speakers (48000Hz)")

    def test_without_sample_rate(self):
        info = DeviceInfo(index=0, name="Unknown Device",
                          sample_rate=0, channels=2, kind="input")
        self.assertEqual(format_device_display(info), "Unknown Device")

    def test_parse_back_name(self):
        display = format_device_display(DeviceInfo(
            index=0, name="VoiceMeeter Input", sample_rate=48000,
            channels=2, kind="output"))
        name = display.split(" (")[0]
        self.assertEqual(name, "VoiceMeeter Input")


class TestEnumerateEmpty(unittest.TestCase):
    def test_empty_mic(self):
        result = enumerate_mic_devices(devices=[])
        self.assertEqual(result, [])

    def test_empty_output(self):
        result = enumerate_audio_out_devices(devices=[])
        self.assertEqual(result, [])

    def test_empty_loopback(self):
        result = enumerate_loopback_devices(devices=[])
        self.assertEqual(result, [])

    def test_no_input_in_list(self):
        devs = [
            {"name": "Output Only", "max_input_channels": 0,
             "max_output_channels": 2, "default_samplerate": 48000},
        ]
        result = enumerate_mic_devices(devices=devs)
        self.assertEqual(result, [])


class TestResolveLoopback(unittest.TestCase):
    def test_exact_match_loopback(self):
        idx = resolve_device_name(
            "Steam Streaming Speakers (loopback)", "loopback",
            devices=FAKE_LOOPBACK_DEVICES)
        self.assertEqual(idx, 10)

    def test_substring_loopback(self):
        idx = resolve_device_name("steam", "loopback",
                                  devices=FAKE_LOOPBACK_DEVICES)
        self.assertEqual(idx, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
