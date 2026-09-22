"""Opt-in ESPHome RGB ring experiment; the state describes manual commands.

The firmware owns mute/volume overlays. Voice/timer commands relinquish manual
ownership; this is not hardware readback. Effects run on the device. No persistence or fades yet.
"""
from dataclasses import dataclass, replace
import asyncio
import math
import colorsys

from esphome.vendor import api_pb2 as pb

LIGHT_KEY = 4  # Append-only alongside media=1, button=2, lux=3.
STATIC_EFFECTS = ('None', 'Echo red')
ANIMATED_EFFECTS = ('Spin', 'Slow spin', 'Pulse', 'Breathe', 'Rainbow')
EFFECTS = STATIC_EFFECTS + ANIMATED_EFFECTS


def unit(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Light values must be finite")
    return min(1.0, max(0.0, value))


@dataclass(frozen=True)
class RingState:
    state: bool = False
    brightness: float = 1.0
    color_brightness: float = 1.0
    red: float = 1.0
    green: float = 1.0
    blue: float = 1.0
    effect: str = "None"

    def command(self, msg):
        if msg.has_color_mode and msg.color_mode != pb.COLOR_MODE_RGB:
            raise ValueError("Only RGB mode is supported")
        if msg.has_flash_length:
            raise ValueError("Flash is not supported")
        if msg.has_effect and (msg.effect or 'None') not in EFFECTS:
            raise ValueError("Unknown LED ring effect")
        changes = {}
        if msg.has_state:
            changes['state'] = msg.state
        for field in ('brightness', 'color_brightness'):
            if getattr(msg, 'has_' + field):
                changes[field] = unit(getattr(msg, field))
        if msg.has_rgb:
            changes.update({field: unit(getattr(msg, field))
                            for field in ('red', 'green', 'blue')})
        if msg.has_effect:
            changes['effect'] = msg.effect or 'None'
            if msg.effect == 'Echo red':
                # The firmware's mute ring is exactly RGB(180, 0, 0).
                # This is a colour reference only; it does not mute the mic.
                changes.update(red=1.0, green=0.0, blue=0.0,
                               brightness=180 / 255, color_brightness=1.0)
        elif self.effect == 'Echo red' and (
                msg.has_rgb or msg.has_brightness or msg.has_color_brightness):
            changes['effect'] = 'None'
        # Transition requests apply immediately in this first experiment.
        return replace(self, **changes)

    def pixels(self):
        scale = 255 * self.brightness * self.color_brightness if self.state else 0
        rgb = {c: round(getattr(self, c) * scale)
               for c in ('red', 'green', 'blue')}
        return [{'id': i, 'r': rgb['red'], 'g': rgb['green'], 'b': rgb['blue']}
                for i in range(12)]

    def animation(self):
        if not self.state or self.effect not in ANIMATED_EFFECTS:
            return None
        pixel = self.pixels()[0]
        rgb = [pixel['r'], pixel['g'], pixel['b']]
        # Continuous manual lights match the solid light's lifetime. The
        # firmware stops animations on control disconnect, and controller
        # voice/timer commands replace them. No periodic network frames.
        spec = {'listening': False, 'ttlSec': 0}
        if self.effect in ('Spin', 'Slow spin'):
            spec.update(pattern='spin', colors=[rgb, [round(c * 0.2) for c in rgb]],
                        periodMs=100 if self.effect == 'Spin' else 250)
        elif self.effect in ('Pulse', 'Breathe'):
            spec.update(pattern='pulse', colors=[rgb],
                        periodMs=1200 if self.effect == 'Pulse' else 3000)
        else:
            scale = 255 * self.brightness * self.color_brightness
            palette = [[round(c * scale) for c in colorsys.hsv_to_rgb(i / 12, 1, 1)]
                       for i in range(12)]
            spec.update(pattern='rotate', colors=palette, periodMs=120)
        return spec

    def response(self):
        return pb.LightStateResponse(key=LIGHT_KEY, color_mode=pb.COLOR_MODE_RGB,
                                    **self.__dict__)


def entity(test=False, animated=False):
    return pb.ListEntitiesLightResponse(
        key=LIGHT_KEY, object_id='led_ring',
        name='LED Ring Test' if test else 'LED Ring',
        supported_color_modes=[pb.COLOR_MODE_RGB],
        legacy_supports_brightness=True, legacy_supports_rgb=True,
        icon='mdi:circle-outline',
        effects=EFFECTS if test or animated else STATIC_EFFECTS,
    )


class RingLight:
    def __init__(self):
        self.state = RingState()
        self.sender = None
        self.animation_sender = None
        self.revision = 0
        self.lock = asyncio.Lock()

    def release(self):
        self.revision += 1
        changed = self.state.state
        self.state = replace(self.state, state=False)
        return changed

    async def command(self, msg, *, test=False):
        # Serialise slider updates; fold partial commands against the last
        # successful command, retaining colour and brightness across off/on.
        async with self.lock:
            proposed = self.state.command(msg)
            revision = self.revision
            if not test:
                if self.sender is None:
                    raise RuntimeError('LED device is disconnected')
                animation = proposed.animation()
                if animation is not None:
                    if self.animation_sender is None:
                        raise RuntimeError('Device does not support LED animations')
                    await self.animation_sender(animation)
                else:
                    await self.sender(proposed.pixels())
            if revision == self.revision:
                self.state = proposed
            return self.state.response()


async def send_to_device(device, pixels):
    """Write a solid ring; firmware cancels any prior animation atomically."""
    await _send_message(device, {'type': 'leds', 'leds': pixels, 'listening': False})


async def send_animation_to_device(device, animation):
    if not device.led_anim_capable:
        raise RuntimeError('Device does not support LED animations')
    await _send_message(device, {'type': 'led_anim', 'anim': animation})


async def _send_message(device, message):
    """Send with observable failure, without overriding a voice/timer/mute ring."""
    import json
    if device.voice_lock.locked() or device.timer_alarm_ringing or device.muted:
        raise RuntimeError('LED ring is in use by voice, timer or mute')
    async with device.voice_lock:
        # Device.send_control swallows errors; HA must not acknowledge a
        # successful light change if this write failed.
        await device.control_ws.send(json.dumps(message))
