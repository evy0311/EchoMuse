"""Opt-in ESPHome RGB ring experiment; the state describes manual commands.

The firmware owns mute/volume overlays. Voice/timer commands relinquish manual
ownership; this is not hardware readback. No persistence, effects or fades yet.
"""
from dataclasses import dataclass, replace
import asyncio
import math

from esphome.vendor import api_pb2 as pb

LIGHT_KEY = 4  # Append-only alongside media=1, button=2, lux=3.


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

    def command(self, msg):
        if msg.has_color_mode and msg.color_mode != pb.COLOR_MODE_RGB:
            raise ValueError("Only RGB mode is supported")
        if msg.has_flash_length or msg.has_effect:
            raise ValueError("Flash and effects are not supported")
        changes = {}
        if msg.has_state:
            changes['state'] = msg.state
        for field in ('brightness', 'color_brightness'):
            if getattr(msg, 'has_' + field):
                changes[field] = unit(getattr(msg, field))
        if msg.has_rgb:
            changes.update({field: unit(getattr(msg, field))
                            for field in ('red', 'green', 'blue')})
        # Transition requests apply immediately in this first experiment.
        return replace(self, **changes)

    def pixels(self):
        scale = 255 * self.brightness * self.color_brightness if self.state else 0
        rgb = {c: round(getattr(self, c) * scale)
               for c in ('red', 'green', 'blue')}
        return [{'id': i, 'r': rgb['red'], 'g': rgb['green'], 'b': rgb['blue']}
                for i in range(12)]

    def response(self):
        return pb.LightStateResponse(key=LIGHT_KEY, color_mode=pb.COLOR_MODE_RGB,
                                    **self.__dict__)


def entity(test=False):
    return pb.ListEntitiesLightResponse(
        key=LIGHT_KEY, object_id='led_ring',
        name='LED Ring Test' if test else 'LED Ring',
        supported_color_modes=[pb.COLOR_MODE_RGB],
        legacy_supports_brightness=True, legacy_supports_rgb=True,
        icon='mdi:circle-outline',
    )


class RingLight:
    def __init__(self):
        self.state = RingState()
        self.sender = None
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
                await self.sender(proposed.pixels())
            if revision == self.revision:
                self.state = proposed
            return self.state.response()


async def send_to_device(device, pixels):
    """Send with observable failure, without overriding a voice/timer/mute ring."""
    import json
    if device.voice_lock.locked() or device.timer_alarm_ringing or device.muted:
        raise RuntimeError('LED ring is in use by voice, timer or mute')
    async with device.voice_lock:
        # Device.send_control swallows errors; HA must not acknowledge a
        # successful light change if this write failed.
        await device.control_ws.send(json.dumps(
            {'type': 'leds', 'leds': pixels, 'listening': False}))
