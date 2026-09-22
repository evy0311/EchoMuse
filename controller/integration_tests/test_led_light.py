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
                esp.suspend_manual_light(srv.device_id)
            self.assertTrue(srv.light.state.state)
            self.assertTrue(srv.light.suspended)
            srv.light.release()
            srv.light.sender = None
            await new._light_command(command(has_state=True, state=True))
            self.assertFalse(responses[-1].state)

    async def test_pattern_specs_and_brightness(self):
        ring = light.RingLight()
        ring.sender = AsyncMock()
        ring.animation_sender = AsyncMock()
        for effect, pattern, period in [('Spin', 'spin', 100), ('Slow spin', 'spin', 250),
                                        ('Pulse', 'pulse', 1200), ('Breathe', 'pulse', 3000),
                                        ('Rainbow', 'rotate', 120)]:
            response = await ring.command(command(has_state=True, state=True,
                has_effect=True, effect=effect, has_rgb=True, red=1, green=0, blue=0,
                has_brightness=True, brightness=0.5))
            anim = ring.animation_sender.call_args.args[0]
            self.assertEqual((anim['pattern'], anim['periodMs']), (pattern, period))
            self.assertFalse(anim['listening'])
            self.assertEqual(response.effect, effect)
            self.assertEqual(anim['colors'][0], [128, 0, 0])
            if effect == 'Rainbow':
                self.assertEqual(len(anim['colors']), 12)
                self.assertEqual(anim['colors'][4], [0, 128, 0])
                self.assertEqual(anim['colors'][8], [0, 0, 128])
        ring.sender.assert_not_called()

    async def test_off_and_none_cancel_animation_with_solid_frame(self):
        ring=light.RingLight(); ring.sender=AsyncMock(); ring.animation_sender=AsyncMock()
        await ring.command(command(has_state=True,state=True,has_effect=True,effect='Spin'))
        await ring.command(command(has_state=True,state=False))
        self.assertTrue(all(p['r']==p['g']==p['b']==0 for p in ring.sender.call_args.args[0]))
        await ring.command(command(has_state=True,state=True))
        self.assertEqual(ring.animation_sender.await_count,2)
        await ring.command(command(has_effect=True,effect='None'))
        self.assertEqual(ring.state.effect,'None')
        self.assertEqual(ring.sender.call_args.args[0][0]['r'],255)

    async def test_exact_echo_red_reference_and_leaving_preset(self):
        ring=light.RingLight(); ring.sender=AsyncMock()
        await ring.command(command(has_brightness=True,brightness=0.1,
                                   has_color_brightness=True,color_brightness=0.2))
        response=await ring.command(command(has_state=True,state=True,has_effect=True,effect='Echo red'))
        self.assertEqual(ring.sender.call_args.args[0],
                         [{'id':i,'r':180,'g':0,'b':0} for i in range(12)])
        self.assertAlmostEqual(response.brightness,180/255,places=6)
        self.assertEqual(response.color_brightness,1)
        await ring.command(command(has_brightness=True,brightness=1))
        self.assertEqual(ring.state.effect,'None')
        self.assertEqual(ring.sender.call_args.args[0][0]['r'],255)

    async def test_animation_failures_leave_state_unchanged(self):
        ring=light.RingLight(); ring.sender=AsyncMock()
        for sender in (None,AsyncMock(side_effect=OSError('closed'))):
            ring.animation_sender=sender
            with self.assertRaises((RuntimeError,OSError)):
                await ring.command(command(has_state=True,state=True,has_effect=True,effect='Spin'))
            self.assertFalse(ring.state.state)
            self.assertEqual(ring.state.effect,'None')
        with self.assertRaises(ValueError):
            await ring.command(command(has_effect=True,effect='unknown'),test=True)

    async def test_effects_are_advertised_by_capability(self):
        srv=server();sat=srv._protocol_factory()
        with patch.object(esp,'HA_LED_RING_MODE','device'):
            for caps,effects in [(['leds'],light.STATIC_EFFECTS),
                                 (['leds','led_anim'],light.EFFECTS)]:
                srv.set_capabilities(caps)
                info=next(m for m in sat.handle_message(pb.ListEntitiesRequest())
                          if isinstance(m,pb.ListEntitiesLightResponse))
                self.assertEqual(tuple(info.effects),effects)
        self.assertEqual(tuple(light.entity(test=True).effects),light.EFFECTS)

    async def test_animation_callback_guards_and_wire_message(self):
        device=SimpleNamespace(voice_lock=asyncio.Lock(),timer_alarm_ringing=False,
            muted=False,led_anim_capable=True,control_ws=SimpleNamespace(send=AsyncMock()))
        anim=light.RingState(state=True,effect='Breathe').animation()
        await light.send_animation_to_device(device,anim)
        self.assertEqual(json.loads(device.control_ws.send.call_args.args[0]),
                         {'type':'led_anim','anim':anim})
        for flag in ('muted','timer_alarm_ringing'):
            setattr(device,flag,True)
            with self.assertRaises(RuntimeError):
                await light.send_animation_to_device(device,anim)
            setattr(device,flag,False)
        device.led_anim_capable=False
        with self.assertRaises(RuntimeError):
            await light.send_animation_to_device(device,anim)

    async def test_suspended_commands_update_desired_state_without_painting(self):
        ring=light.RingLight(); ring.sender=AsyncMock(); ring.animation_sender=AsyncMock()
        await ring.command(command(has_state=True,state=True,has_rgb=True,red=1))
        ring.sender.reset_mock()
        ring.suspend()
        await ring.command(command(has_effect=True,effect='Breathe',has_brightness=True,brightness=0.3))
        ring.sender.assert_not_called(); ring.animation_sender.assert_not_called()
        self.assertTrue(ring.state.state)
        await ring.restore()
        self.assertEqual(ring.animation_sender.call_args.args[0]['pattern'],'pulse')
        self.assertEqual(ring.animation_sender.call_args.args[0]['colors'][0],[77,0,0])
        ring.suspend()
        await ring.command(command(has_state=True,state=False))
        await ring.restore()
        self.assertTrue(all(p['r']==p['g']==p['b']==0 for p in ring.sender.call_args.args[0]))

    async def test_restore_waits_and_old_cleanup_cannot_interrupt_new_turn(self):
        ring=light.RingLight(); ring.sender=AsyncMock()
        await ring.command(command(has_state=True,state=True)); ring.sender.reset_mock()
        busy=True
        ring.ready=lambda: not busy
        ring.suspend(); ring.schedule_restore()
        old_task=ring._restore_task
        await asyncio.sleep(0)
        ring.sender.assert_not_called()
        ring.suspend()
        await asyncio.sleep(0)
        self.assertTrue(old_task.cancelled())
        busy=False
        ring.schedule_restore()
        await asyncio.wait_for(ring._restore_task,1)
        ring.sender.assert_awaited_once()
        self.assertFalse(ring.suspended)

    async def test_outcome_cue_delay_is_not_shortened_by_outer_cleanup(self):
        ring=light.RingLight();ring.sender=AsyncMock()
        ring.suspend();ring.schedule_restore(delay=0.05)
        cue_task=ring._restore_task
        ring.schedule_restore()
        self.assertIs(cue_task,ring._restore_task)
        await asyncio.sleep(0.01)
        ring.sender.assert_not_called()
        await asyncio.wait_for(cue_task,1)
        ring.sender.assert_awaited_once()

    async def test_disconnect_cancels_pending_restore(self):
        ring=light.RingLight();ring.sender=AsyncMock()
        ring.suspend();ring.schedule_restore(delay=0.05)
        task=ring._restore_task
        ring.release();ring.sender=None
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.assertFalse(ring.state.state)
        self.assertFalse(ring.suspended)

    async def test_real_controller_voice_cleanup_restores_manual_light(self):
        import em_controller as ctl
        for scenario in ('normal','pattern','off_during_turn','colour_during_turn',
                         'continuation','barge','error','early_error','cancel'):
            with self.subTest(scenario=scenario):
                srv=server()
                ws=SimpleNamespace(send=AsyncMock())
                device=ctl.Device(srv.device_id,'127.0.0.1',['leds','led_anim'],ws)
                ring=srv.light
                ring.sender=lambda pixels: light.send_to_device(device,pixels)
                ring.animation_sender=lambda anim: light.send_animation_to_device(device,anim)
                ring.ready=lambda: not (device.voice_lock.locked() or device.timer_alarm_ringing or device.muted)
                initial=command(has_state=True,state=True,has_rgb=True,red=1,green=0,blue=0,
                                has_brightness=True,brightness=0.4)
                if scenario=='pattern':
                    initial.has_effect=True;initial.effect='Spin'
                await ring.command(initial)
                initial_state=ring.state
                calls=0
                async def run_turn(**kwargs):
                    nonlocal calls
                    calls+=1
                    self.assertTrue(ring.suspended)
                    self.assertTrue(device.voice_lock.locked())
                    if scenario=='off_during_turn':
                        await ring.command(command(has_state=True,state=False))
                    if scenario=='colour_during_turn':
                        await ring.command(command(has_rgb=True,red=0,green=0,blue=1))
                    if scenario=='barge' and calls==1:
                        device.barge_detected=True
                    if scenario=='error':
                        raise OSError('pipeline failed')
                    if scenario=='cancel':
                        raise asyncio.CancelledError()
                    return scenario=='continuation' and calls==1
                with patch.dict(esp._servers,{srv.device_id:srv}), \
                     patch.object(esp,'HA_LED_RING_MODE','device'), \
                     patch.object(esp,'trigger_voice_turn',side_effect=run_turn), \
                     patch.object(ctl,'_push_device_state',new=AsyncMock(
                         side_effect=RuntimeError('early failure') if scenario=='early_error' else None)), \
                     patch.object(ctl.em_player,'interrupt',new=AsyncMock()), \
                     patch.object(ctl.em_player,'resume_interrupted',new=AsyncMock()):
                    if scenario in ('error','early_error','cancel'):
                        with self.assertRaises((OSError,RuntimeError,asyncio.CancelledError)):
                            await ctl._run_voice_locked(device,is_wakeword=True)
                    else:
                        await ctl._run_voice_locked(device,is_wakeword=True)
                    self.assertIsNotNone(ring._restore_task)
                    await asyncio.wait_for(ring._restore_task,1)
                self.assertFalse(ring.suspended)
                self.assertEqual(calls,2 if scenario in ('continuation','barge') else
                                 (0 if scenario=='early_error' else 1))
                if scenario not in ('off_during_turn','colour_during_turn'):
                    self.assertEqual(ring.state,initial_state)
                frames=[json.loads(c.args[0]) for c in ws.send.call_args_list]
                frames=[m for m in frames if m['type'] in ('leds','led_anim')]
                last=frames[-1]
                if scenario=='pattern':
                    self.assertEqual(last['anim'],initial_state.animation())
                elif scenario=='off_during_turn':
                    self.assertTrue(all(p['r']==p['g']==p['b']==0 for p in last['leds']))
                elif scenario=='colour_during_turn':
                    self.assertEqual(last['leds'][0],{'id':0,'r':0,'g':0,'b':102})
                else:
                    self.assertEqual(last['leds'],initial_state.pixels())

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
