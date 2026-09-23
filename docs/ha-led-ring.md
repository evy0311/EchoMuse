# Home Assistant LED ring indicator

EchoMuse exposes **LED Ring** as an RGB light through each device's existing
ESPHome integration. It is an idle status indicator for notifications and
household information. Devices must advertise both `leds` and `led_anim`;
older firmware keeps its normal status ring but does not expose this light.
No separate integration or controller option is required. Reload the device's
ESPHome integration after updating the controller to discover the new entity.

The light supports on/off, brightness, RGB colour and these effects:

| Effect | Device animation | Appearance |
|---|---|---|
| None | `solid` | Solid selected colour |
| Spin / Slow spin | `spin` | Selected colour with a dim trail |
| Rotate | `rotate` | A three-LED arc in the selected colour |
| Pulse / Breathe | `pulse` | Selected colour pulsing over 1.2 / 3 seconds |
| Rainbow | `rotate` | Rotating rainbow; brightness applies, RGB is ignored |

Meter is omitted because the firmware measures voice-response audio before
mixing in music. The normal voice-response meter remains unchanged.

Every effect runs on the device's existing animation engine. Home Assistant
sends a pattern, not a stream of frames. Brightness and colour brightness scale
RGB directly; there is no additional colour calibration. Transitions apply
immediately. For repeated flashing use Pulse; the light does not support HA's
one-shot `flash` parameter.

## Lifetime and priority

**Every on command expires after 60 seconds.** This applies to solid colours
too. A subsequent accepted light command renews that window while the light is
on. A persistent household indicator should repeat its command every 30 seconds.
Ordinary HA keepalives, HA reconnects and voice cleanup do not renew it. This
keeps the firmware's `ttlSec` dead-man switch effective if HA or the controller
stops sending commands. HA reports the light off when the lease expires.

Listening, thinking, response playback, outcome cues and timer alarms take the
ring immediately. Microphone mute and the physical volume arc remain owned by
the firmware. A still-valid light setting resumes after those indications end,
with only the remaining timeout. It cannot come back after its lease expires.
HA changes made during an override update the pending setting; an off command
cancels it without clearing the higher-priority indication.

The HA entity reports the requested ambient setting, including while a status
indication temporarily covers it. It is not LED hardware readback: the existing
protocol has no display acknowledgement. Failed socket writes retain the last
accepted state. Device reconnects and controller restarts clear the setting;
an HA reconnect preserves only its remaining lifetime.

## Example: keep an alarm indicator current

Replace the example entities with your helper and EchoMuse light. This updates
immediately when the helper changes and renews it every 30 seconds while active.
Turning the helper off clears it immediately; if HA stops, it expires on-device.

```yaml
alias: EchoMuse idle alarm indicator
triggers:
  - trigger: state
    entity_id: input_boolean.alarm_indicator
  - trigger: time_pattern
    seconds: "/30"
  - trigger: homeassistant
    event: start
actions:
  - choose:
      - conditions:
          - condition: state
            entity_id: input_boolean.alarm_indicator
            state: "on"
        sequence:
          - action: light.turn_on
            target:
              entity_id: light.garage_led_ring
            data:
              rgb_color: [255, 0, 80]
              brightness_pct: 30
              effect: Spin
    default:
      - action: light.turn_off
        target:
          entity_id: light.garage_led_ring
mode: restart
```

For a single notification, call `light.turn_on` once. Call `light.turn_off` after
a shorter delay if desired; otherwise the 60-second timeout clears it.
