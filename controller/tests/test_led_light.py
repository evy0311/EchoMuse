"""Issue #66: idle-only native animations and a bounded HA lease.

This suite imports only the standard library and em_led_light; it runs in the
same minimal environment as the rest of the controller's CI tests.
"""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import em_led_light as light


class StateTests(unittest.TestCase):
    def test_rgb_brightness_and_partial_updates(self):
        state = light.RingState().update(state=True, brightness=0.5,
                                        red=1, green=0, blue=0.25)
        self.assertEqual(state.animation()['colors'], [[128, 0, 32]])
        dim = state.update(color_brightness=0.5)
        self.assertEqual(dim.animation()['colors'], [[64, 0, 16]])
        self.assertEqual(dim.update(state=False).animation()['pattern'], 'off')
        self.assertEqual(dim.update(state=False).update(state=True), dim)

    def test_all_native_patterns_have_ttl_and_never_claim_listening(self):
        expected = {'None': 'solid', 'Spin': 'spin', 'Slow spin': 'spin',
                    'Rotate': 'rotate', 'Pulse': 'pulse', 'Breathe': 'pulse',
                    'Rainbow': 'rotate', 'Meter': 'meter'}
        self.assertEqual(set(light.EFFECTS), set(expected))
        for effect, pattern in expected.items():
            with self.subTest(effect=effect):
                anim = light.RingState(state=True, effect=effect).animation()
                self.assertEqual(anim['pattern'], pattern)
                self.assertGreater(anim['ttlSec'], 0)
                self.assertLessEqual(anim['ttlSec'], 60)
                self.assertIs(anim['listening'], False)
        self.assertEqual(light.RingState(state=True, effect='Pulse').animation()['periodMs'], 1200)
        self.assertEqual(light.RingState(state=True, effect='Breathe').animation()['periodMs'], 3000)

    def test_rainbow_palette_obeys_brightness(self):
        anim = light.RingState(state=True, effect='Rainbow', brightness=0.5).animation()
        self.assertEqual(len(anim['colors']), 12)
        self.assertEqual([anim['colors'][i] for i in (0, 4, 8)],
                         [[128, 0, 0], [0, 128, 0], [0, 0, 128]])

    def test_invalid_values_and_finite_clamping(self):
        for field in ('brightness', 'color_brightness', 'red', 'green', 'blue'):
            self.assertEqual(getattr(light.RingState().update(**{field: 2}), field), 1)
            self.assertEqual(getattr(light.RingState().update(**{field: -1}), field), 0)
            for value in (float('nan'), float('inf'), -float('inf')):
                with self.assertRaises(ValueError):
                    light.RingState().update(**{field: value})
        with self.assertRaises(ValueError):
            light.RingState().update(effect='not an effect')
        self.assertEqual(light.RingState().update(effect='').effect, 'None')
        with self.assertRaises(ValueError):
            light.RingState(state=True).animation(ttl=0)


class RingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 100.0
        self.updates = []
        self.ring = light.RingLight(clock=lambda: self.now, on_change=self.updates.append)
        self.ring.sender = AsyncMock()

    async def asyncTearDown(self):
        self.ring.release()

    async def test_solid_on_off_and_effect_replacement_use_only_native_specs(self):
        await self.ring.command(state=True, red=1, green=0, blue=0.25, brightness=0.5)
        anim = self.ring.sender.call_args.args[0]
        self.assertEqual(anim['pattern'], 'solid')
        self.assertEqual(anim['colors'], [[128, 0, 32]])
        await self.ring.command(effect='Spin')
        self.assertEqual(self.ring.sender.call_args.args[0]['pattern'], 'spin')
        await self.ring.command(state=False)
        self.assertEqual(self.ring.sender.call_args.args[0]['pattern'], 'off')
        self.assertIsNone(self.ring.expires_at)
        await self.ring.command(state=True)
        self.assertEqual(self.ring.sender.call_args.args[0]['pattern'], 'spin')

    async def test_failure_does_not_acknowledge_or_renew(self):
        await self.ring.command(state=True)
        before, deadline = self.ring.state, self.ring.expires_at
        self.now += 10
        for sender in (None, AsyncMock(side_effect=OSError('closed'))):
            self.ring.sender = sender
            with self.assertRaises((RuntimeError, OSError)):
                await self.ring.command(brightness=0.5)
            self.assertEqual(self.ring.state, before)
            self.assertEqual(self.ring.expires_at, deadline)

    async def test_expiry_updates_ha_without_a_network_off(self):
        await self.ring.command(state=True, effect='Spin')
        self.now += 60
        self.ring._expire()
        self.assertFalse(self.ring.state.state)
        self.assertIsNone(self.ring.expires_at)
        self.assertEqual(self.updates[-1], self.ring.state)
        self.ring.sender.assert_awaited_once()

    async def test_new_command_renews_but_empty_command_does_not(self):
        await self.ring.command(state=True)
        first_timer = self.ring._expiry_handle
        self.now += 30
        await self.ring.command(brightness=0.5)
        self.assertTrue(first_timer.cancelled())
        self.assertEqual(self.ring.expires_at, 190)
        self.now += 10
        await self.ring.command()
        self.assertEqual(self.ring.expires_at, 190)
        self.assertEqual(self.ring.sender.await_count, 2)

    async def test_suspension_keeps_latest_command_without_painting(self):
        await self.ring.command(state=True)
        self.ring.suspend()
        self.ring.sender.reset_mock()
        self.ring.ready = lambda: False
        await self.ring.command(effect='Breathe', brightness=0.3, red=1, green=0, blue=0)
        self.ring.sender.assert_not_called()
        self.assertFalse(await self.ring.restore())
        self.ring.ready = lambda: True
        self.now += 20
        self.assertTrue(await self.ring.restore())
        anim = self.ring.sender.call_args.args[0]
        self.assertEqual(anim['pattern'], 'pulse')
        self.assertEqual(anim['colors'], [[76, 0, 0]])
        self.assertEqual(anim['ttlSec'], 40)
        self.assertEqual(self.ring.expires_at, 160)

    async def test_expired_suspended_pattern_cannot_return_after_a_long_turn(self):
        await self.ring.command(state=True)
        self.ring.suspend()
        self.ring.sender.reset_mock()
        self.now += 61
        self.assertTrue(await self.ring.restore())
        self.ring.sender.assert_not_called()
        self.assertFalse(self.ring.state.state)
        self.assertFalse(self.ring.suspended)

    async def test_subsecond_remainder_never_becomes_infinite_ttl(self):
        await self.ring.command(state=True)
        self.ring.suspend()
        self.now += 59.5
        self.ring.sender.reset_mock()
        await self.ring.restore()
        self.ring.sender.assert_not_called()
        self.assertFalse(self.ring.state.state)

    async def test_off_during_overlay_does_not_clear_status_ring(self):
        await self.ring.command(state=True)
        self.ring.suspend()
        self.ring.sender.reset_mock()
        await self.ring.command(state=False)
        await self.ring.restore()
        self.ring.sender.assert_not_called()
        self.assertFalse(self.ring.state.state)

    async def test_busy_device_refuses_unsuspended_command(self):
        self.ring.ready = lambda: False
        with self.assertRaises(RuntimeError):
            await self.ring.command(state=True)
        self.ring.sender.assert_not_called()
        self.assertFalse(self.ring.state.state)

    async def test_reset_during_send_cannot_commit_stale_state(self):
        async def disconnect(_):
            self.ring.release()
        self.ring.sender = disconnect
        await self.ring.command(state=True)
        self.assertFalse(self.ring.state.state)
        self.assertIsNone(self.ring.expires_at)

    async def test_queued_command_cannot_cross_a_device_reconnect(self):
        await self.ring.lock.acquire()
        task = asyncio.create_task(self.ring.command(state=True))
        await asyncio.sleep(0)
        self.ring.release()
        self.ring.lock.release()
        with self.assertRaises(RuntimeError):
            await task
        self.ring.sender.assert_not_called()

    async def test_commands_are_serialised(self):
        frames = []
        async def send(anim):
            await asyncio.sleep(0)
            frames.append(anim)
        self.ring.sender = send
        await asyncio.gather(self.ring.command(state=True),
                             self.ring.command(brightness=0.5),
                             self.ring.command(state=False))
        self.assertEqual([a['pattern'] for a in frames], ['solid', 'solid', 'off'])
        self.assertEqual(frames[1]['colors'], [[128, 128, 128]])

    async def test_new_status_cancels_old_restore(self):
        await self.ring.command(state=True)
        self.ring.sender.reset_mock()
        self.ring.suspend()
        self.ring.schedule_restore(delay=10)
        task = self.ring._restore_task
        self.ring.suspend()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.ring.sender.assert_not_called()
        self.ring.schedule_restore()
        await asyncio.wait_for(self.ring._restore_task, 1)
        self.ring.sender.assert_awaited_once()

    async def test_restore_waits_for_idle_and_preserves_outcome_delay(self):
        await self.ring.command(state=True)
        self.ring.sender.reset_mock()
        self.ring.ready = lambda: False
        self.ring.suspend()
        self.ring.schedule_restore(delay=0.02)
        task = self.ring._restore_task
        self.ring.schedule_restore()
        self.assertIs(self.ring._restore_task, task)
        await asyncio.sleep(0.03)
        self.ring.sender.assert_not_called()
        self.ring.ready = lambda: True
        await asyncio.wait_for(task, 1)
        self.ring.sender.assert_awaited_once()

    async def test_disconnect_cancels_expiry_and_restore(self):
        await self.ring.command(state=True)
        expiry = self.ring._expiry_handle
        self.ring.suspend()
        self.ring.schedule_restore(delay=10)
        task = self.ring._restore_task
        self.ring.release()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.assertTrue(expiry.cancelled())
        self.assertFalse(self.ring.state.state)


class DeviceCallbackTests(unittest.IsolatedAsyncioTestCase):
    def device(self):
        return SimpleNamespace(voice_lock=asyncio.Lock(), timer_alarm_ringing=False,
                               muted=False, led_anim_capable=True,
                               control_ws=SimpleNamespace(send=AsyncMock()))

    async def test_native_wire_message_and_failure_propagation(self):
        device = self.device()
        anim = light.RingState(state=True).animation()
        await light.send_animation_to_device(device, anim)
        self.assertEqual(json.loads(device.control_ws.send.call_args.args[0]),
                         {'type': 'led_anim', 'anim': anim})
        device.control_ws.send.side_effect = OSError('closed')
        with self.assertRaises(OSError):
            await light.send_animation_to_device(device, anim)
        self.assertFalse(device.voice_lock.locked())

    async def test_mute_timer_voice_and_old_firmware_refuse_writes(self):
        device = self.device()
        anim = light.RingState(state=True).animation()
        for flag in ('muted', 'timer_alarm_ringing'):
            setattr(device, flag, True)
            with self.assertRaises(RuntimeError):
                await light.send_animation_to_device(device, anim)
            setattr(device, flag, False)
        async with device.voice_lock:
            with self.assertRaises(RuntimeError):
                await light.send_animation_to_device(device, anim)
        device.led_anim_capable = False
        with self.assertRaises(RuntimeError):
            await light.send_animation_to_device(device, anim)
        device.control_ws.send.assert_not_called()

    async def test_voice_takes_ring_after_inflight_manual_write(self):
        device = self.device()
        entered, finish = asyncio.Event(), asyncio.Event()
        order = []
        async def send(_):
            entered.set()
            await finish.wait()
            order.append('manual')
        device.control_ws.send = send
        task = asyncio.create_task(light.send_animation_to_device(
            device, light.RingState(state=True).animation()))
        await entered.wait()
        async def turn():
            async with device.voice_lock:
                order.append('listening')
        voice = asyncio.create_task(turn())
        await asyncio.sleep(0)
        finish.set()
        await asyncio.gather(task, voice)
        self.assertEqual(order, ['manual', 'listening'])
