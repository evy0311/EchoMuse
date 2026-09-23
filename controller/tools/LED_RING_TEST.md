# LED ring validation

See [Home Assistant LED ring indicator](../../docs/ha-led-ring.md) for entity,
effect, priority and timeout behavior. The light uses the existing `led_anim`
protocol and requires no firmware changes.

## Automated checks

From `controller/`:

```sh
python -m pytest tests/
python -m unittest discover -s integration_tests -p test_led_light.py -v
```

The first suite includes the standard-library-only light state and ownership
rules. The second needs the full controller dependencies and exercises real
ESPHome protobuf messages, connection lifecycle and controller voice cleanup.
CI runs it in the existing controller smoke-test job.

## Isolated ESPHome lab

The lab advertises the same light and effects, with a recording callback in
place of a physical device. It does not access a device or start a voice turn.
Run from `controller/` with the controller dependencies:

```sh
PYTHONPATH=. SERVER_IP=127.0.0.1 python tools/light_test_server.py
```

Connect HA's ESPHome integration to the lab host at port **16099**. Its device
name is **EchoMuse LED Lab** and its entity is **LED Ring**. Only one API client
may connect at a time; disconnect HA before running the client smoke test.

In a second terminal, with the test-only dependency `aioesphomeapi==45.3.1`:

```sh
python tools/light_smoke_test.py 127.0.0.1 16099
```

This checks discovery, RGB/brightness, effects, state feedback, reconnect and
the 60-second expiry using the same client library as HA. It refuses to control
any device whose friendly name is not **EchoMuse LED Lab** and leaves the lab
off. Allow about a minute for the expiry check.

`docker-compose.mac.yml` is a local development override for Macs without
Docker host networking. It publishes the controller and lab ports and mounts
the edited Python files. Use it alongside the base Compose file:

```sh
docker compose -f docker-compose.yml -f docker-compose.mac.yml --profile light-test up -d --no-build
```

Use the controller's LAN IP and each real device's assigned ESPHome port in HA;
16099 is only the lab. Bridge networking may require manual host/port entries.
The override maps ten device ports.

## Hardware acceptance

On a device already connected to the test controller:

1. Confirm on/off, RGB, brightness and each native effect through HA.
2. Start a voice turn while the light is on. Listening, thinking, response and
   outcome indications must win; an unexpired setting may resume afterward.
3. Change colour or turn the light off during a conversation; check the latest
   setting is used after it ends. A turn lasting over 60 seconds must not
   restore an expired setting.
4. Check microphone mute and physical volume changes retain their red ring and
   cyan arc. Exercise a timer alarm as another higher-priority owner.
5. Send one on command, then stop sending light commands. Confirm the physical
   ring expires and HA reports off within approximately 60 seconds. Repeat with
   solid colour and an animated effect, including loss of the HA connection.
6. Restart the controller or disconnect/reconnect the device: the manual light
   starts off. A controller failure must still let the device TTL expire.

Automated tests verify policy and wire messages; visual appearance and physical
mute/volume hand-back require this hardware pass.
