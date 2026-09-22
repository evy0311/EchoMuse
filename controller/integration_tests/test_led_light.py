"""Run with controller dependencies: python -m unittest discover -s integration_tests -p test_led_light.py"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('SERVER_IP', '127.0.0.1')
import em_led_light as light
import em_esphome as esp
from esphome.vendor import api_pb2 as pb


def command(**kw):
    return pb.LightCommandRequest(key=light.LIGHT_KEY, **kw)


def server():
    return esp.DeviceESPhomeServer('led-test', 'LED test', '02:00:00:00:00:04',
                                   'test', 0, SimpleNamespace(name='Test', languages=['en']))


class RingTests(unittest.IsolatedAsyncioTestCase):
    async def test_rgb_brightness_partial_off_on(self):
        ring = light.RingLight()
        ring.sender = AsyncMock()
        await ring.command(command(has_state=True, state=True, has_brightness=True,
                                   brightness=0.5, has_rgb=True, red=1, green=0, blue=0.25))
        pixels = ring.sender.call_args.args[0]
        self.assertEqual(len(pixels), 12)
        self.assertEqual(pixels[0], {'id': 0, 'r': 128, 'g': 0, 'b': 32})
        await ring.command(command(has_state=True, state=False))
        self.assertEqual(ring.sender.call_args.args[0][0]['r'], 0)
        await ring.command(command(has_state=True, state=True))
        self.assertEqual(ring.sender.call_args.args[0], pixels)
        await ring.command(command(has_color_brightness=True, color_brightness=0.5))
        self.assertEqual(ring.sender.call_args.args[0][0]['r'], 64)

    async def test_failure_does_not_acknowledge_or_store_proposed_state(self):
        ring = light.RingLight()
        for sender in (None, AsyncMock(side_effect=OSError('closed'))):
            ring.sender = sender
            with self.assertRaises((RuntimeError, OSError)):
                await ring.command(command(has_state=True, state=True))
            self.assertFalse(ring.state.state)

    async def test_test_mode_never_touches_hardware(self):
        ring = light.RingLight()
        ring.sender = AsyncMock(side_effect=AssertionError('hardware touched'))
        state = await ring.command(command(has_state=True, state=True), test=True)
        self.assertTrue(state.state)
        ring.sender.assert_not_called()

    async def test_clamping_invalid_values_and_unsupported_mode(self):
        ring = light.RingLight()
        await ring.command(command(has_brightness=True, brightness=2), test=True)
        self.assertEqual(ring.state.brightness, 1)
        await ring.command(command(has_brightness=True, brightness=-1), test=True)
        self.assertEqual(ring.state.brightness, 0)
        for fields in [dict(has_brightness=True, brightness=float('nan')),
                       dict(has_rgb=True, red=float('inf')),
                       dict(has_color_mode=True, color_mode=pb.COLOR_MODE_WHITE),
                       dict(has_flash_length=True, flash_length=100)]:
            with self.assertRaises(ValueError):
                await ring.command(command(**fields), test=True)

    async def test_external_activity_during_send_wins(self):
        ring = light.RingLight()
        async def send(_):
            ring.release()
        ring.sender = send
        await ring.command(command(has_state=True, state=True))
        self.assertFalse(ring.state.state)

    async def test_commands_are_serialised(self):
        ring = light.RingLight()
        frames = []
        async def send(pixels):
            await asyncio.sleep(0)
            frames.append(pixels)
        ring.sender = send
        await asyncio.gather(
            ring.command(command(has_state=True, state=True, has_rgb=True, red=1)),
            ring.command(command(has_brightness=True, brightness=0.5)),
            ring.command(command(has_state=True, state=False)))
        self.assertEqual([p[0]['r'] for p in frames], [255,128,0])

    async def test_device_callback_guards_and_json(self):
        device = SimpleNamespace(voice_lock=asyncio.Lock(), timer_alarm_ringing=False,
                                 muted=False, control_ws=SimpleNamespace(send=AsyncMock()))
        pixels = light.RingState(state=True).pixels()
        await light.send_to_device(device, pixels)
        self.assertEqual(json.loads(device.control_ws.send.call_args.args[0]),
                         dict(type='leds', leds=pixels, listening=False))
        for field in ('muted', 'timer_alarm_ringing'):
            setattr(device, field, True)
            with self.assertRaises(RuntimeError):
                await light.send_to_device(device, pixels)
            setattr(device, field, False)
        async with device.voice_lock:
            with self.assertRaises(RuntimeError):
                await light.send_to_device(device, pixels)
        device.control_ws.send.side_effect = OSError('closed')
        with self.assertRaises(OSError):
            await light.send_to_device(device, pixels)

    async def test_actual_satellite_discovery_commands_reconnect_and_release(self):
        srv = server()
        srv.set_capabilities(['leds'])
        with patch.object(esp, 'HA_LED_RING_MODE', 'device'):
            sat = srv._protocol_factory()
            sat._send_one = lambda msg: responses.append(msg)
            responses = []
            entities = list(sat.handle_message(pb.ListEntitiesRequest()))
            lights = [m for m in entities if isinstance(m, pb.ListEntitiesLightResponse)]
            self.assertEqual(len(lights), 1)
            self.assertEqual(lights[0].key, 4)
            self.assertEqual(list(lights[0].supported_color_modes), [pb.COLOR_MODE_RGB])
            self.assertIsInstance(entities[-1], pb.ListEntitiesDoneResponse)
            self.assertTrue(any(isinstance(m, pb.LightStateResponse) for m in
                                sat.handle_message(pb.SubscribeStatesRequest())))
            srv.light.sender = AsyncMock()
            list(sat.handle_message(command(has_state=True, state=True)))
            await asyncio.gather(*sat._light_tasks)
            self.assertTrue(responses[-1].state)
            wrong = command(has_state=True, state=False); wrong.key=99
            list(sat.handle_message(wrong))
            self.assertTrue(srv.light.state.state)
            srv._on_satellite_disconnected(sat)
            new = srv._protocol_factory()
            new._send_one = responses.append
            states=list(new.handle_message(pb.SubscribeStatesRequest()))
            self.assertTrue(next(m for m in states if isinstance(m,pb.LightStateResponse)).state)
            with patch.dict(esp._servers, {srv.device_id: srv}):
                esp.release_manual_light(srv.device_id)
            self.assertFalse(responses[-1].state)
            srv.light.sender = None
            await new._light_command(command(has_state=True, state=True))
            self.assertFalse(responses[-1].state)

    async def test_opt_in_and_capability_gate(self):
        srv = server(); sat=srv._protocol_factory()
        for mode, caps, expected in [('off',['leds'],False),('device',[],False),
                                     ('device',['leds'],True),('test',[],True)]:
            with patch.object(esp, 'HA_LED_RING_MODE', mode):
                srv.set_capabilities(caps)
                messages=list(sat.handle_message(pb.ListEntitiesRequest()))
                self.assertEqual(any(isinstance(m,pb.ListEntitiesLightResponse)
                                     for m in messages), expected)


if __name__ == '__main__':
    unittest.main()
