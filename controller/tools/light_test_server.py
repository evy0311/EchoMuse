"""Standalone simulated ring: no device, database writes or voice pipeline.

Add an ESPHome integration at the Mac's LAN IP, port 16099. Uses the same
entity discovery/command handler as the real EchoMuse satellite.
"""
import asyncio
import logging
import os
from types import SimpleNamespace

os.environ['EM_HA_LED_RING'] = 'test'
from em_esphome import DeviceESPhomeServer, EchoMuseSatellite
from esphome.vendor import api_pb2 as pb


class TestSatellite(EchoMuseSatellite):
    def handle_message(self, msg):
        for response in super().handle_message(msg):
            if isinstance(response, pb.DeviceInfoResponse):
                response.friendly_name = 'EchoMuse LED Lab'
                response.voice_assistant_feature_flags = 0
            # A light-only lab entry keeps HA from starting voice setup.
            if isinstance(response, (pb.ListEntitiesMediaPlayerResponse,
                                     pb.MediaPlayerStateResponse)):
                continue
            yield response

    async def _light_command(self, msg):
        await super()._light_command(msg)
        logging.info('SIMULATED ring: %s', self._owning_server.light.state)


class TestServer(DeviceESPhomeServer):
    def _protocol_factory(self):
        if self._active_satellite is not None:
            return super()._protocol_factory()
        satellite = TestSatellite(self.device_id, self.label, self.mac_address,
                                  self.oww_model_id, self._on_satellite_disconnected,
                                  self.oww_model_info, self)
        self._active_satellite = satellite
        return satellite


async def main():
    server = TestServer('echomuse-led-lab-0001', 'EchoMuse LED Lab',
                        '02:EC:40:00:00:01', 'test', 16099,
                        SimpleNamespace(name='Test', languages=['en']))
    await server.start('0.0.0.0')
    logging.info('Simulated LED light listening on port 16099; no physical LEDs used')
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
