import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from client import client


class _ProbeSocket:
    def __init__(self, local_ip="192.168.10.25"):
        self.local_ip = local_ip
        self.connected_to = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def connect(self, address):
        self.connected_to = address

    def getsockname(self):
        return (self.local_ip, 50000)


class TestClientIdentity(unittest.TestCase):
    def test_route_local_ip_for_url_uses_server_destination(self):
        probe = _ProbeSocket("10.20.30.40")

        with patch.object(client.socket, "socket", return_value=probe):
            local_ip = client._route_local_ip_for_url("http://signage.example.local:5080")

        self.assertEqual(local_ip, "10.20.30.40")
        self.assertEqual(probe.connected_to, ("signage.example.local", 5080))

    def test_mac_address_for_local_ip_returns_matching_interface_mac(self):
        fake_psutil = SimpleNamespace(
            net_if_addrs=lambda: {
                "Wi-Fi": [
                    SimpleNamespace(address="192.168.1.50"),
                    SimpleNamespace(address="aa-bb-cc-dd-ee-ff"),
                ],
                "Ethernet": [
                    SimpleNamespace(address="10.20.30.40"),
                    SimpleNamespace(address="00-11-22-33-44-55"),
                ],
            }
        )

        with patch.object(client.importlib, "import_module", return_value=fake_psutil):
            mac_address = client._mac_address_for_local_ip("10.20.30.40")

        self.assertEqual(mac_address, "00:11:22:33:44:55")

    def test_primary_mac_prefers_server_route_over_adapter_fallback(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            client, "_get_server_route_mac_address", return_value="00:11:22:33:44:55"
        ), patch.object(client, "_get_windows_adapter_mac_address", return_value="aa:bb:cc:dd:ee:ff"):
            mac_address = client._get_primary_mac_address()

        self.assertEqual(mac_address, "00:11:22:33:44:55")

    def test_configured_mac_still_overrides_detected_route(self):
        with patch.dict(os.environ, {"CLIENT_MAC_ADDRESS": "66-77-88-99-aa-bb"}, clear=True), patch.object(
            client, "_get_server_route_mac_address", return_value="00:11:22:33:44:55"
        ):
            mac_address = client._get_primary_mac_address()

        self.assertEqual(mac_address, "66:77:88:99:aa:bb")


if __name__ == "__main__":
    unittest.main()
