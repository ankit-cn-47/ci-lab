from __future__ import annotations

import ipaddress
import xml.etree.ElementTree as ET
from typing import Any

try:
    import libvirt
except ImportError:  # pragma: no cover - optional dependency
    libvirt = None

from .config import die


def network_exists(conn, name: str) -> bool:
    try:
        return any(network.name() == name for network in conn.listAllNetworks(0))
    except libvirt.libvirtError as exc:
        die(f"Unable to list libvirt networks while looking for {name}: {exc}")


def network_xml(cfg: dict[str, Any], reservations: list[tuple[str, str, str]]) -> str:
    n = cfg["network"]
    name = n["name"]
    subnet = ipaddress.ip_network(n["subnet"], strict=True)
    dns_config = ""
    if n.get("dns_domain") and n.get("dns_forwarder"):
        dns_config = (
            f"  <domain name='{n['dns_domain']}' localOnly='no'/>\n"
            f"  <dns><forwarder domain='{n['dns_domain']}' addr='{n['dns_forwarder']}'/></dns>\n"
        )
    hosts = "\n".join(f"      <host mac='{mac}' ip='{ip}'/>" for _, mac, ip in reservations)
    dhcp_hosts = f"\n{hosts}\n" if hosts else ""
    return f"""<network>
  <name>{name}</name>
  <forward mode='nat'/>
  <bridge name='virbr-{name[:8]}' stp='on' delay='0'/>
{dns_config}  
  <ip address='{n["gateway"]}' prefix='{subnet.prefixlen}'>
    <dhcp>
      <range start='{n["dhcp_start"]}' end='{n["dhcp_end"]}'/>
{dhcp_hosts}    </dhcp>
  </ip>
</network>"""


def reconcile_network_dns_xml(xml: str, cfg: dict[str, Any]) -> tuple[str, bool]:
    network_config = cfg["network"]
    domain_name = network_config.get("dns_domain")
    forwarder = network_config.get("dns_forwarder")
    if not domain_name or not forwarder:
        return xml, False

    root = ET.fromstring(xml)
    changed = False

    domain = root.find("./domain")
    if domain is None:
        domain = ET.Element("domain")
        children = list(root)
        insert_at = next(
            (index for index, child in enumerate(children) if child.tag in {"dns", "ip"}),
            len(children),
        )
        root.insert(insert_at, domain)
        changed = True
    if domain.get("name") != domain_name or domain.get("localOnly") != "no":
        domain.set("name", domain_name)
        domain.set("localOnly", "no")
        changed = True

    dns = root.find("./dns")
    if dns is None:
        dns = ET.Element("dns")
        children = list(root)
        insert_at = next(
            (index for index, child in enumerate(children) if child.tag == "ip"),
            len(children),
        )
        root.insert(insert_at, dns)
        changed = True

    domain_forwarders = [
        item for item in dns.findall("forwarder") if item.get("domain") == domain_name
    ]
    if len(domain_forwarders) != 1 or domain_forwarders[0].get("addr") != forwarder:
        for item in domain_forwarders:
            dns.remove(item)
        dns.insert(0, ET.Element("forwarder", {"domain": domain_name, "addr": forwarder}))
        changed = True

    if not changed:
        return xml, False
    return ET.tostring(root, encoding="unicode"), True


def reconcile_dhcp_reservations(
    net,
    reservations: list[tuple[str, str, str]],
    dry_run: bool = False,
) -> None:
    if not reservations:
        return

    current_root = ET.fromstring(net.XMLDesc(0))
    current_hosts = current_root.findall("./ip/dhcp/host")
    if libvirt is None:
        die("libvirt-python is not installed.")

    if dry_run:
        for name, mac, ip in reservations:
            print(f"  DHCP reservation {name}: {ip} ({mac})")
        return

    flags = 0
    if net.isPersistent():
        flags |= libvirt.VIR_NETWORK_UPDATE_AFFECT_CONFIG
    if net.isActive():
        flags |= libvirt.VIR_NETWORK_UPDATE_AFFECT_LIVE

    for name, mac, ip in reservations:
        exact_match = any(
            host.get("mac", "").lower() == mac.lower() and host.get("ip") == ip
            for host in current_hosts
        )
        conflicting_hosts = [
            host for host in current_hosts
            if (
                host.get("mac", "").lower() == mac.lower() or host.get("ip") == ip
            ) and not (
                host.get("mac", "").lower() == mac.lower() and host.get("ip") == ip
            )
        ]

        for host in conflicting_hosts:
            if host.get("mac"):
                selector = f"<host mac='{host.get('mac')}'/>"
            else:
                selector = f"<host ip='{host.get('ip')}'/>"
            try:
                net.update(
                    libvirt.VIR_NETWORK_UPDATE_COMMAND_DELETE,
                    libvirt.VIR_NETWORK_SECTION_IP_DHCP_HOST,
                    0,
                    selector,
                    flags,
                )
            except libvirt.libvirtError as exc:
                die(f"Failed to remove conflicting DHCP reservation for {name}: {exc}")

        if not exact_match:
            host_xml = f"<host mac='{mac}' ip='{ip}'/>"
            try:
                net.update(
                    libvirt.VIR_NETWORK_UPDATE_COMMAND_ADD_LAST,
                    libvirt.VIR_NETWORK_SECTION_IP_DHCP_HOST,
                    0,
                    host_xml,
                    flags,
                )
            except libvirt.libvirtError as exc:
                die(f"Failed to add DHCP reservation for {name} ({ip}): {exc}")

        current_hosts = ET.fromstring(net.XMLDesc(0)).findall("./ip/dhcp/host")


def ensure_network(
    conn,
    cfg: dict[str, Any],
    reservations: list[tuple[str, str, str]],
    dry_run: bool = False,
) -> None:
    name = cfg["network"]["name"]
    if network_exists(conn, name):
        print(f"  network {name}: exists")
        net = conn.networkLookupByName(name)
        network_definition, dns_changed = reconcile_network_dns_xml(net.XMLDesc(0), cfg)
        if dns_changed:
            print("  network DNS forwarding: update (network restart required)")
            if not dry_run:
                try:
                    if net.isActive():
                        net.destroy()
                    if net.isPersistent():
                        net.undefine()
                    net = conn.networkDefineXML(network_definition)
                    if net is None:
                        die(f"Failed to redefine network {name}")
                    net.setAutostart(1)
                    net.create()
                except libvirt.libvirtError as exc:
                    die(f"Failed to update DNS configuration for network {name}: {exc}")
        reconcile_dhcp_reservations(net, reservations, dry_run)
        return

    print(f"  network {name}: create")
    if dry_run:
        return

    net = conn.networkDefineXML(network_xml(cfg, reservations))
    if net is None:
        die(f"Failed to define network {name}")
    net.setAutostart(1)
    net.create()
