"""Idle LED indicator policy, independent of ESPHome and controller imports.

Every accepted on command leases the ring for 60 seconds. Only a new HA light
command renews that lease; status overlays and reconnects never extend it.
The device renders and expires every pattern, including solid colours.
"""
import asyncio
import colorsys
from dataclasses import dataclass, replace
import json
import logging
import math
import time

LIGHT_KEY = 4  # Append-only alongside media=1, button=2, lux=3.
LEASE_SECONDS = 60
EFFECTS = ('None', 'Spin', 'Slow spin', 'Rotate', 'Pulse', 'Breathe', 'Rainbow', 'Meter')
log = logging.getLogger('echomuse.esphome.light')


def unit(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Light values must be finite')
    return min(1.0, max(0.0, value))


@dataclass(frozen=True)
class RingState:
    state: bool = False
    brightness: float = 1.0
    color_brightness: float = 1.0
    red: float = 1.0
    green: float = 1.0
    blue: float = 1.0
    effect: str = 'None'

    def update(self, **changes):
        for field in ('brightness', 'color_brightness', 'red', 'green', 'blue'):
            if field in changes:
                changes[field] = unit(changes[field])
        if 'effect' in changes:
            changes['effect'] = changes['effect'] or 'None'
            if changes['effect'] not in EFFECTS:
                raise ValueError('Unknown LED ring effect')
        return replace(self, **changes)

    def animation(self, ttl=LEASE_SECONDS):
        if not self.state:
            return {'pattern': 'off', 'listening': False}
        if ttl <= 0:
            raise ValueError('An on pattern must have a finite positive TTL')
        scale = 255 * self.brightness * self.color_brightness
        rgb = [round(getattr(self, c) * scale) for c in ('red', 'green', 'blue')]
        spec = {'pattern': 'solid', 'colors': [rgb], 'listening': False, 'ttlSec': ttl}
        if self.effect in ('Spin', 'Slow spin'):
            spec.update(pattern='spin', colors=[rgb, [round(c * 0.2) for c in rgb]],
                        periodMs=100 if self.effect == 'Spin' else 250)
        elif self.effect == 'Rotate':
            spec.update(pattern='rotate', colors=[rgb] * 3 + [[0, 0, 0]] * 9,
                        periodMs=120)
        elif self.effect in ('Pulse', 'Breathe'):
            spec.update(pattern='pulse', periodMs=1200 if self.effect == 'Pulse' else 3000)
        elif self.effect == 'Rainbow':
            palette = [[round(c * scale) for c in colorsys.hsv_to_rgb(i / 12, 1, 1)]
                       for i in range(12)]
            spec.update(pattern='rotate', colors=palette, periodMs=120)
        elif self.effect == 'Meter':
            spec.update(pattern='meter')
        return spec


class RingLight:
    def __init__(self, *, clock=time.monotonic, on_change=None):
        self.state = RingState()
        self.sender = None
        self.ready = lambda: True
        self.on_change = on_change
        self.clock = clock
        self.expires_at = None
        self.revision = 0
        self.lock = asyncio.Lock()
        self.suspended = False
        self.overlay_revision = 0
        self._expiry_handle = None
        self._restore_task = None

    def _cancel_restore(self):
        if self._restore_task is not None:
            self._restore_task.cancel()
            self._restore_task = None

    def _cancel_expiry(self):
        if self._expiry_handle is not None:
            self._expiry_handle.cancel()
            self._expiry_handle = None

    def _publish(self):
        if self.on_change is not None:
            self.on_change(self.state)

    def _expire(self):
        """Mirror the device's deadline without painting over a status ring."""
        self._expiry_handle = None
        if self.expires_at is None:
            return
        remaining = self.expires_at - self.clock()
        if remaining > 0:
            self._expiry_handle = asyncio.get_running_loop().call_later(remaining, self._expire)
            return
        self.expires_at = None
        self.state = replace(self.state, state=False)
        self._publish()

    def suspend(self):
        """Status LEDs take the physical ring; the HA lease keeps counting down."""
        self.overlay_revision += 1
        self.suspended = True
        self._cancel_restore()

    def release(self):
        """A device disconnect/reconnect or server shutdown forgets the lease."""
        self._cancel_restore()
        self._cancel_expiry()
        self.expires_at = None
        self.suspended = False
        self.revision += 1
        self.state = replace(self.state, state=False)

    async def _write(self, state, ttl=LEASE_SECONDS):
        if self.sender is None:
            raise RuntimeError('LED device is disconnected')
        if not self.ready():
            raise RuntimeError('LED ring is in use by voice, timer or mute')
        await self.sender(state.animation(ttl))

    async def command(self, **changes):
        revision = self.revision
        async with self.lock:
            if revision != self.revision:
                raise RuntimeError('LED device connection changed')
            proposed = self.state.update(**changes)
            if self.sender is None:
                raise RuntimeError('LED device is disconnected')
            # Empty ESPHome commands do not renew a lease.
            if not changes:
                return
            if not self.suspended:
                await self._write(proposed)
            if revision != self.revision:
                return
            self.state = proposed
            self._cancel_expiry()
            self.expires_at = self.clock() + LEASE_SECONDS if proposed.state else None
            if proposed.state:
                self._expiry_handle = asyncio.get_running_loop().call_later(
                    LEASE_SECONDS, self._expire)

    async def restore(self):
        """Resume a still-valid HA setting with only its remaining lifetime."""
        async with self.lock:
            if not self.suspended:
                return True
            if not self.ready():
                return False
            revision, overlay = self.revision, self.overlay_revision
            if self.state.state:
                remaining = math.floor(self.expires_at - self.clock())
                if remaining < 1:
                    # Firmware TTLs are whole seconds. Do not round a nearly
                    # expired lease up, or replay it as ttlSec=0 (forever).
                    self.expires_at = self.clock()
                    self._cancel_expiry()
                    self._expire()
                else:
                    await self._write(self.state, remaining)
            # An off/expired lease has nothing to restore. The status owner's
            # cleanup or its own TTL clears the ring without a competing off.
            if revision == self.revision and overlay == self.overlay_revision:
                self.suspended = False
                return True
            return False

    def schedule_restore(self, delay=0):
        if not self.suspended or self._restore_task is not None:
            return

        async def resume_when_idle():
            try:
                # Cleanup may still own voice_lock. Outcome cues keep their
                # full TTL before a manual light can return.
                await asyncio.sleep(delay)
                while not await self.restore():
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Could not restore idle LED ring setting')

        task = asyncio.create_task(resume_when_idle())
        self._restore_task = task

        def finished(done):
            if self._restore_task is done:
                self._restore_task = None

        task.add_done_callback(finished)


async def send_animation_to_device(device, animation):
    """Use the existing firmware renderer and its mute/volume suppressions."""
    if not device.led_anim_capable:
        raise RuntimeError('Device does not support LED animations')
    if device.voice_lock.locked() or device.timer_alarm_ringing or device.muted:
        raise RuntimeError('LED ring is in use by voice, timer or mute')
    # The check and uncontended acquire do not yield. Voice waits for an
    # in-flight HA write, then replaces it with the listening pattern.
    async with device.voice_lock:
        # Device.send_control swallows errors; do not acknowledge a failed
        # socket write as a successful light change.
        await device.control_ws.send(json.dumps({'type': 'led_anim', 'anim': animation}))
