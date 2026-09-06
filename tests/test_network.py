import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from roomcast.config import Config
from roomcast.network import Network
from roomcast.roku import Roku


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_hint_recovers_and_persists_serial_bound_address(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                "10.0.0.2",
                "paired",
                lan_interface="wlan0",
                address_cache=directory + "/address.json",
            )
            roku = Roku(config.roku_ip, config.roku_serial)
            network = Network(config, roku)
            network.probe = AsyncMock(side_effect=lambda ip: ip == "10.0.0.3")
            network.ssdp = AsyncMock(return_value=["10.0.0.3"])
            network.route = AsyncMock(return_value="10.0.0.4")
            try:
                self.assertEqual(await network.ensure(), "10.0.0.3")
                self.assertEqual(roku.base, "http://10.0.0.3:8060")
                self.assertEqual(network.local_address, "10.0.0.4")
                self.assertIn(
                    '"serial": "paired"', Path(config.address_cache).read_text()
                )
                network.ssdp.reset_mock()
                await network.ensure()
                network.ssdp.assert_not_awaited()
            finally:
                await roku.close()

    async def test_missing_tv_fails_without_selecting_other_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                "10.0.0.2",
                "paired",
                lan_interface="wlan0",
                address_cache=directory + "/address.json",
                discovery_networks=["10.0.0.0/30"],
            )
            roku = Roku(config.roku_ip, config.roku_serial)
            network = Network(config, roku)
            network.probe = AsyncMock(return_value=False)
            network.ssdp = AsyncMock(return_value=[])
            network.route = AsyncMock()
            try:
                with self.assertRaisesRegex(ValueError, "not found"):
                    await network.ensure()
                network.route.assert_not_awaited()
            finally:
                await roku.close()

    def test_discovery_scope_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "1024"):
            Config(
                "10.0.0.2",
                "paired",
                lan_interface="wlan0",
                discovery_networks=["10.0.0.0/16"],
            )
