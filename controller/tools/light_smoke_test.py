"""Exercise a simulated light with HA's aioesphomeapi client (test-only dependency).

Usage: python tools/light_smoke_test.py HOST [PORT]
Only point this at the standalone LED Lab, never a real device.
"""
import asyncio
import sys
from aioesphomeapi import APIClient, LightInfo, LightState, ColorMode


async def main(host, port):
    client = APIClient(host, port, None)
    updates = asyncio.Queue()
    async def next_state():
        return await asyncio.wait_for(updates.get(), 5)
    def on_state(state):
        if isinstance(state, LightState):
            updates.put_nowait(state)
    try:
        await client.connect(login=True)
        info, entities, _ = await client.device_info_and_list_entities()
        assert info.friendly_name == 'EchoMuse LED Lab', 'Refusing to test a real device'
        lights = [entity for entity in entities if isinstance(entity, LightInfo)]
        assert len(lights) == 1 and lights[0].key == 4
        assert ColorMode.RGB in lights[0].supported_color_modes
        assert info.voice_assistant_feature_flags == 0
        assert len(entities) == 1
        client.subscribe_states(on_state)
        await next_state()
        client.light_command(4, state=True, brightness=0.25,
                             color_mode=ColorMode.RGB, rgb=(1, 0, 0.5))
        state = await next_state()
        assert state.state and abs(state.brightness - 0.25) < 0.001
        assert (state.red, state.green, state.blue) == (1, 0, 0.5)
        print('PASS: discovery, RGB mode, on, brightness and colour over TCP')
        await client.disconnect()
        client = APIClient(host, port, None)
        await client.connect(login=True)
        await client.device_info_and_list_entities()
        client.subscribe_states(on_state)
        state = await next_state()
        assert state.state and abs(state.brightness - 0.25) < 0.001
        print('PASS: state retained across HA client reconnect')
        expected_effects = {'None', 'Spin', 'Slow spin', 'Rotate', 'Pulse', 'Breathe', 'Rainbow'}
        assert set(lights[0].effects) == expected_effects
        for effect in sorted(expected_effects):
            client.light_command(4, state=True, effect=effect)
            state = await next_state()
            assert state.effect == effect
        print('PASS: all native effects discovered and acknowledged')
        client.light_command(4, state=True, effect='None')
        assert (await next_state()).state
        state = await asyncio.wait_for(updates.get(), 65)
        assert not state.state
        print('PASS: solid pattern expires and publishes OFF without more HA commands')
        client.light_command(4, state=False)
        state = await next_state()
        assert not state.state
        print('PASS: off command and state feedback; simulated light left OFF')
    finally:
        await client.disconnect()


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv)>2 else 16099))
