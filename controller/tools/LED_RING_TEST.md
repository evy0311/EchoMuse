# EchoMuse LED-ring experiment

Local project: `/Users/evanhorsley/Documents/Github/EchoMuse`
Branch: `ha-led-ring-test`

## Checkpoint 1: startup recovered

The local `.env` now contains `SERVER_IP=192.168.68.135` instead of `10.x.y.z`.
Docker Desktop host networking was disabled. The separate Mac compose override
uses published TCP ports, preserving the existing database mount at
`/Users/evanhorsley/EchoMuse/controller/data`. The original CPU build edits in
`docker-compose.yml` remain uncommitted and unchanged by this experiment.

The dashboard responds at http://192.168.68.135:8768. This is a new controller
with first-run account setup still required and no approved devices.
The setup token is available locally in the controller startup logs; do not
post those logs publicly. No admin account was created by this experiment.

Start both the real controller and simulated light:

```sh
cd /Users/evanhorsley/Documents/Github/EchoMuse/controller
docker compose -f docker-compose.yml -f docker-compose.mac.yml --profile light-test up -d --no-build
```

Always include both compose files for this Mac setup. The original compose
file alone uses host networking, which is still disabled in Docker Desktop.
Source files are mounted read-only into the container, so controller edits
need a service restart, not an image rebuild. The Dockerfile also includes
the new module for future rebuilds. The lab service uses the existing local
`controller-echomuse-controller` image.

## Checkpoint 2: simulated light

In Home Assistant, open Settings → Devices & services → Add integration →
ESPHome. Enter:

- Host: `192.168.68.135`
- Port: `16099`
- Password/encryption key: leave empty

The device is **EchoMuse LED Lab**, with **LED Ring Test**. It supports on/off,
brightness and RGB. It cannot control any physical LEDs. No HACS change is
needed. It has no voice assistant setup flow.

The lab is left running and its simulated light is off. It allows one client
connection at a time, matching the real controller. Disconnect HA before
running the standalone smoke test.

## Checkpoint 3: physical ring path implemented, hardware test pending

The Mac override enables `EM_HA_LED_RING=device` on the real controller.
A connected Dot advertising `leds` gets the entity **LED Ring** at stable key 4.
Existing entity keys are unchanged. Other installations default to `off`.
Available modes: `off` (default), `test` (state only), `device` (send LED frames).

The new callback sends the existing `leds` control message to all twelve LEDs
with `listening=false`. It scales the RGB channels by brightness and colour
brightness. It does not require a device firmware modification.

Manual writes are refused during voice turns, timer alarms and microphone
mute. Existing controller LED/animation commands relinquish manual ownership
and report this light off. Firmware retains control over mute/volume overlays.
The HA state describes the last accepted manual command, not LED hardware
readback; firmware-only overlays may differ temporarily. There is no device
acknowledgment for an LED frame. A failed socket write does not store a new
state. HA reconnects retain the manual state; device reconnects and controller
restarts reset it. Effects/flash are unsupported; transitions apply immediately.

No physical device was moved or reconfigured. To finish the hardware test,
provide the HA URL/IP, target Dot identity/IP and its current controller.
Back up its existing endpoint/credentials before switching one test Dot.

This Mac setup publishes ports 8767, 8768, 8770, 16001–16010 and 17001–17010.
Port 16099 belongs only to the isolated lab. Real devices use their own assigned
ESPHome ports starting at 16001; add those separately to HA after approval.
More than ten real devices requires extending the mapped port ranges.

Bridge networking does not provide reliable LAN mDNS discovery. Use manual HA
host/port entries and, when moving the target Dot, its supported static controller
endpoint configuration (`docs/configuration.md`, “Static controller endpoint”).
Do not assume the Dot will discover this Mac automatically. Existing TLS device
credentials may belong to its current controller and must be handled during
that move. If the Mac's DHCP address changes, update SERVER_IP and endpoints.

## Verification completed

- Controller startup: running, no restart loop, dashboard HTTP 200 through
  localhost and the Mac LAN address.
- 125 selected tests passed: 9 new light integration tests plus existing host-IP,
  capability, ESPHome identity/ports, volume, timer and wake-ring tests.
- Real aioesphomeapi 45.3.1 client through the Mac's published port: entity
  discovery, RGB mode, on/off, brightness, colour, state feedback and reconnect.
- Physical Dot and actual Home Assistant UI: pending.

The initial regression attempt omitted the device source from its container
mount. Rerunning with the full repository mounted resolved those file-not-found
failures; all selected tests passed.

Full controller dependencies are required for `integration_tests/test_led_light.py`.
It is separate from the project's lightweight `tests/` suite:

```sh
python -m unittest discover -s integration_tests -p test_led_light.py -v
```

`tools/light_smoke_test.py` additionally requires aioesphomeapi==45.3.1:

```sh
python tools/light_smoke_test.py 192.168.68.135 16099
```

It refuses a device whose friendly name is not EchoMuse LED Lab, and leaves
the simulated light off after a successful run.

## Rollback / checkpoints

Original local config, local diff and original commit were saved to:
`/Users/evanhorsley/Documents/Codex/2026-09-22/referenced-chatgpt-conversation-this-is-an/work/checkpoints/before-changes/`

The `.env` backup contains the broken placeholder and any existing settings;
do not restore it blindly. Keep the corrected SERVER_IP.

To stop only the lab:

```sh
docker compose -f docker-compose.yml -f docker-compose.mac.yml --profile light-test stop light-test
```

To disable the experimental physical light, set `EM_HA_LED_RING: "off"` in
`docker-compose.mac.yml`, then recreate only the controller:

```sh
docker compose -f docker-compose.yml -f docker-compose.mac.yml up -d --no-build echomuse-controller
```

For full code rollback, stop both experiment services first, then revert the
experiment commits on the test branch. Preserve your original compose edits,
corrected `.env`, and data directory. Removing the Mac override also removes
its port publishing; the original host-network compose needs Docker Desktop
host networking enabled before it is reachable from the Mac.

References:
- https://www.home-assistant.io/integrations/esphome/
- https://docs.docker.com/engine/network/drivers/host/
