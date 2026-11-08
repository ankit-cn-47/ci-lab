import unittest
from contextlib import redirect_stdout
import io
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import libvirt
import yaml

import vmctl


class FakeNetwork:
    def __init__(self, xml: str) -> None:
        self.xml = xml
        self.updates: list[tuple[int, int, int, str, int]] = []

    def XMLDesc(self, flags: int) -> str:
        return self.xml

    def isPersistent(self) -> bool:
        return True

    def isActive(self) -> bool:
        return True

    def update(self, command: int, section: int, parent: int, xml: str, flags: int) -> int:
        self.updates.append((command, section, parent, xml, flags))
        root = ET.fromstring(self.xml)
        dhcp = root.find("./ip/dhcp")
        entry = ET.fromstring(xml)
        if command == libvirt.VIR_NETWORK_UPDATE_COMMAND_ADD_LAST:
            dhcp.append(entry)
        elif command == libvirt.VIR_NETWORK_UPDATE_COMMAND_DELETE:
            for host in list(dhcp.findall("host")):
                if (
                    entry.get("mac") and host.get("mac") == entry.get("mac")
                ) or (
                    entry.get("ip") and host.get("ip") == entry.get("ip")
                ):
                    dhcp.remove(host)
        self.xml = ET.tostring(root, encoding="unicode")
        return 0


class FakeNetworkResource:
    def __init__(self, xml: str, active: bool = True, persistent: bool = True) -> None:
        self.xml = xml
        self.active = active
        self.persistent = persistent
        self.autostart = False

    def name(self) -> str:
        return ET.fromstring(self.xml).findtext("name")

    def XMLDesc(self, flags: int) -> str:
        return self.xml

    def isActive(self) -> bool:
        return self.active

    def isPersistent(self) -> bool:
        return self.persistent

    def destroy(self) -> None:
        self.active = False

    def undefine(self) -> None:
        self.persistent = False

    def setAutostart(self, enabled: int) -> None:
        self.autostart = bool(enabled)

    def create(self) -> None:
        self.active = True


class DhcpReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_network_xml = (
            "<network><ip><dhcp>"
            "<range start='192.0.2.100' end='192.0.2.200'/>"
            "</dhcp></ip></network>"
        )

    def test_mac_is_stable_and_valid(self) -> None:
        mac = vmctl.stable_mac_address("lab-ci")
        self.assertEqual(mac, vmctl.stable_mac_address("lab-ci"))
        self.assertRegex(mac, r"^52:54:00:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}$")

    def test_missing_vm_check_does_not_use_erroring_name_lookup(self) -> None:
        class FakeDomain:
            def name(self) -> str:
                return "existing-vm"

        class FakeConnection:
            def listAllDomains(self, flags: int) -> list[FakeDomain]:
                return [FakeDomain()]

        conn = FakeConnection()
        self.assertFalse(vmctl.vm_exists(conn, "lab-ci"))
        self.assertTrue(vmctl.vm_exists(conn, "existing-vm"))

    def test_missing_network_check_does_not_use_erroring_name_lookup(self) -> None:
        class FakeNetworkHandle:
            def name(self) -> str:
                return "existing-network"

        class FakeConnection:
            def listAllNetworks(self, flags: int) -> list[FakeNetworkHandle]:
                return [FakeNetworkHandle()]

        conn = FakeConnection()
        self.assertFalse(vmctl.network_exists(conn, "lab-network"))
        self.assertTrue(vmctl.network_exists(conn, "existing-network"))

    def test_network_and_vm_xml_share_reserved_mac(self) -> None:
        config = vmctl.load_config(Path("examples/lab.yaml"))
        vm = vmctl.merge_defaults(config["vms"][0], config["defaults"])
        mac = vmctl.stable_mac_address(vm["name"])
        reservation_xml = ET.fromstring(
            vmctl.network_xml(config, [(vm["name"], mac, vm["ip"])])
        )
        domain_xml = ET.fromstring(
            vmctl.vm_xml(
                {**vm, "mac_address": mac},
                Path("/tmp/disk.qcow2"),
                Path("/tmp/seed.iso"),
                config["network"]["name"],
            )
        )

        self.assertEqual(
            reservation_xml.find("./ip/dhcp/host").get("mac"),
            domain_xml.find("./devices/interface/mac").get("address"),
        )
        self.assertEqual(
            reservation_xml.find("./ip/dhcp/host").get("ip"),
            vm["ip"],
        )

    def test_network_xml_configures_conditional_dns_forwarding(self) -> None:
        config = vmctl.load_config(Path("examples/lab.yaml"))
        network = ET.fromstring(vmctl.network_xml(config, []))

        self.assertEqual(
            network.find("./domain").get("name"),
            "lab.home.arpa",
        )
        self.assertEqual(
            network.find("./dns/forwarder").attrib,
            {"domain": "lab.home.arpa", "addr": "10.10.10.2"},
        )

    def test_existing_network_dns_update_preserves_other_network_settings(self) -> None:
        config = vmctl.load_config(Path("examples/lab.yaml"))
        old_xml = (
            "<network><name>devops-lab-network</name>"
            "<forward mode='nat'/><dns><forwarder addr='192.0.2.53'/>"
            "</dns><ip address='10.10.10.1' prefix='24'>"
            "<dhcp><range start='10.10.10.100' end='10.10.10.200'/>"
            "</dhcp></ip></network>"
        )

        updated_xml, changed = vmctl.reconcile_network_dns_xml(old_xml, config)
        updated = ET.fromstring(updated_xml)
        unchanged_xml, changed_again = vmctl.reconcile_network_dns_xml(updated_xml, config)

        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual(updated.findtext("./name"), "devops-lab-network")
        self.assertEqual(updated.find("./forward").get("mode"), "nat")
        self.assertEqual(updated.find("./ip/dhcp/range").get("start"), "10.10.10.100")
        self.assertEqual(updated.find("./dns/forwarder").get("addr"), "10.10.10.2")
        self.assertEqual(updated.findall("./dns/forwarder")[1].get("addr"), "192.0.2.53")

    def test_apply_redefines_existing_network_for_dns_forwarder(self) -> None:
        config = vmctl.load_config(Path("examples/lab.yaml"))

        class FakeConnection:
            def __init__(self) -> None:
                self.network = FakeNetworkResource(
                    "<network><name>devops-lab-network</name>"
                    "<forward mode='nat'/><ip address='10.10.10.1' prefix='24'/>"
                    "</network>"
                )
                self.redefined: FakeNetworkResource | None = None

            def listAllNetworks(self, flags: int) -> list[FakeNetworkResource]:
                return [self.network]

            def networkLookupByName(self, name: str) -> FakeNetworkResource:
                return self.network

            def networkDefineXML(self, xml: str) -> FakeNetworkResource:
                self.redefined = FakeNetworkResource(xml, active=False, persistent=True)
                self.network = self.redefined
                return self.redefined

        conn = FakeConnection()
        old_network = conn.network
        with redirect_stdout(io.StringIO()):
            vmctl.ensure_network(conn, config, [])

        self.assertFalse(old_network.isActive())
        self.assertIsNotNone(conn.redefined)
        assert conn.redefined is not None
        self.assertTrue(conn.redefined.isActive())
        self.assertTrue(conn.redefined.autostart)
        self.assertEqual(
            ET.fromstring(conn.redefined.XMLDesc(0)).find("./dns/forwarder").get("addr"),
            "10.10.10.2",
        )

    def test_public_key_is_added_to_configured_cloud_init_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "id_ed25519.pub"
            public_key = "ssh-ed25519 AAAATEST ankit@machine"
            key_path.write_text(public_key + "\n", encoding="utf-8")
            vm = {
                "name": "lab-ci",
                "cloud_init": {
                    "username": "lab",
                    "ssh_authorized_keys_file": str(key_path),
                },
            }
            user_data_path, _ = vmctl.cloud_init_files(vm, Path(tmp))
            cloud_config = yaml.safe_load(
                user_data_path.read_text(encoding="utf-8").split("\n", 1)[1]
            )

        lab_user = next(
            user
            for user in cloud_config["users"]
            if isinstance(user, dict) and user.get("name") == "lab"
        )
        self.assertEqual(lab_user["ssh_authorized_keys"], [public_key])
        self.assertNotIn("ssh_authorized_keys", cloud_config)

    def test_reservation_add_is_idempotent(self) -> None:
        net = FakeNetwork(self.base_network_xml)
        reservation = [("lab-ci", "52:54:00:00:00:01", "192.0.2.10")]

        vmctl.reconcile_dhcp_reservations(net, reservation)
        vmctl.reconcile_dhcp_reservations(net, reservation)

        hosts = ET.fromstring(net.xml).findall("./ip/dhcp/host")
        self.assertEqual(len(net.updates), 1)
        self.assertEqual([(host.get("mac"), host.get("ip")) for host in hosts], [
            ("52:54:00:00:00:01", "192.0.2.10")
        ])
        self.assertEqual(
            net.updates[0][4],
            libvirt.VIR_NETWORK_UPDATE_AFFECT_CONFIG
            | libvirt.VIR_NETWORK_UPDATE_AFFECT_LIVE,
        )

    def test_reservation_ip_change_replaces_old_mapping(self) -> None:
        net = FakeNetwork(self.base_network_xml)

        vmctl.reconcile_dhcp_reservations(
            net, [("lab-ci", "52:54:00:00:00:01", "192.0.2.10")]
        )
        vmctl.reconcile_dhcp_reservations(
            net, [("lab-ci", "52:54:00:00:00:01", "192.0.2.11")]
        )

        hosts = ET.fromstring(net.xml).findall("./ip/dhcp/host")
        self.assertEqual([(host.get("mac"), host.get("ip")) for host in hosts], [
            ("52:54:00:00:00:01", "192.0.2.11")
        ])
        self.assertEqual(
            [update[0] for update in net.updates[-2:]],
            [
                libvirt.VIR_NETWORK_UPDATE_COMMAND_DELETE,
                libvirt.VIR_NETWORK_UPDATE_COMMAND_ADD_LAST,
            ],
        )


if __name__ == "__main__":
    unittest.main()
