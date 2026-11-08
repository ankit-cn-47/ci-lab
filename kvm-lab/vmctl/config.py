from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency
    yaml = None

try:
    import libvirt
except ImportError:  # pragma: no cover - optional dependency
    libvirt = None

DEFAULTS = {
    "memory_gb": 2,
    "vcpus": 2,
    "disk_gb": 20,
    "autostart": True,
    "username": "lab",
}


def die(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def require_libvirt() -> None:
    if libvirt is None:
        die("libvirt-python is not installed. Install the distro package, e.g. python3-libvirt.")


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        die(f"Configuration file does not exist: {path}")

    try:
        raw = path.read_text()
        if path.suffix.lower() == ".json":
            data = json.loads(raw)
        elif path.suffix.lower() in {".yaml", ".yml"}:
            if yaml is None:
                die("PyYAML is not installed. Install it with: pip install -r requirements.txt")
            data = yaml.safe_load(raw)
        else:
            die("Configuration must use .yaml, .yml or .json")
    except (OSError, json.JSONDecodeError, yaml.YAMLError if yaml else Exception) as exc:
        die(f"Unable to parse {path}: {exc}")

    if not isinstance(data, dict):
        die("Top-level configuration must be an object/mapping.")
    return data


def get(cfg: dict[str, Any], key: str, default: Any = None) -> Any:
    value = cfg.get(key, default)
    return default if value is None else value


def merge_defaults(vm: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    result = dict(defaults)
    result.update(vm)
    return result


def read_ssh_authorized_keys_file(path: str) -> list[str]:
    key_path = Path(os.path.expanduser(path))
    try:
        keys = [line.strip() for line in key_path.read_text().splitlines() if line.strip()]
    except OSError as exc:
        raise ValueError(f"Unable to read SSH public key file {key_path}: {exc}") from exc
    if not keys:
        raise ValueError(f"SSH public key file is empty: {key_path}")
    return keys


def stable_mac_address(vm_name: str) -> str:
    suffix = hashlib.sha256(vm_name.encode("utf-8")).hexdigest()[:6]
    return "52:54:00:" + ":".join(suffix[i:i + 2] for i in range(0, 6, 2))


def validate_config(cfg: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    lv = cfg.get("libvirt", {})
    if not isinstance(lv, dict):
        errors.append("libvirt must be a mapping.")
        lv = {}

    uri = lv.get("uri", "qemu:///system")
    pool = lv.get("storage_pool", "default")
    base_image = lv.get("base_image")

    if not pool:
        errors.append("libvirt.storage_pool must not be empty.")
    if not base_image:
        errors.append("libvirt.base_image is required.")
    elif not Path(os.path.expanduser(base_image)).exists():
        errors.append(f"Base image does not exist: {base_image}")

    network = cfg.get("network", {})
    if not isinstance(network, dict):
        errors.append("network must be a mapping.")
        network = {}

    required_network = ["name", "subnet", "gateway", "dhcp_start", "dhcp_end"]
    for key in required_network:
        if not network.get(key):
            errors.append(f"network.{key} is required.")

    try:
        subnet = ipaddress.ip_network(network.get("subnet", "0.0.0.0/32"), strict=True)
        gateway = ipaddress.ip_address(network.get("gateway", "0.0.0.0"))
        dhcp_start = ipaddress.ip_address(network.get("dhcp_start", "0.0.0.0"))
        dhcp_end = ipaddress.ip_address(network.get("dhcp_end", "0.0.0.0"))
        dns_forwarder = network.get("dns_forwarder")
        if gateway not in subnet:
            errors.append("network.gateway must be inside network.subnet.")
        if dhcp_start not in subnet or dhcp_end not in subnet:
            errors.append("DHCP range must be inside network.subnet.")
        if int(dhcp_start) > int(dhcp_end):
            errors.append("network.dhcp_start must be <= network.dhcp_end.")
        if dns_forwarder:
            try:
                dns_addr = ipaddress.ip_address(dns_forwarder)
                if dns_addr not in subnet:
                    errors.append("network.dns_forwarder must be inside network.subnet.")
            except ValueError:
                errors.append("network.dns_forwarder must be a valid IP address.")
    except ValueError as exc:
        errors.append(f"Invalid network address: {exc}")

    dns_domain = network.get("dns_domain")
    if bool(dns_domain) != bool(network.get("dns_forwarder")):
        errors.append("network.dns_domain and network.dns_forwarder must be configured together.")
    elif dns_domain and (
        not isinstance(dns_domain, str)
        or not dns_domain.strip()
        or any(not label or len(label) > 63 for label in dns_domain.rstrip(".").split("."))
    ):
        errors.append("network.dns_domain must be a valid DNS domain name.")

    defaults = cfg.get("defaults", {})
    if not isinstance(defaults, dict):
        errors.append("defaults must be a mapping.")
        defaults = {}

    vms = cfg.get("vms")
    if not isinstance(vms, list) or not vms:
        errors.append("vms must be a non-empty list.")
        vms = []

    seen_names: set[str] = set()
    seen_ips: set[str] = set()
    seen_macs: set[str] = set()
    for idx, raw_vm in enumerate(vms):
        if not isinstance(raw_vm, dict):
            errors.append(f"vms[{idx}] must be a mapping.")
            continue
        vm = merge_defaults(raw_vm, defaults)
        name = vm.get("name")
        cloud_init = vm.get("cloud_init", {})
        if isinstance(cloud_init, dict) and cloud_init.get("ssh_authorized_keys_file"):
            key_file = cloud_init["ssh_authorized_keys_file"]
            if not isinstance(key_file, str):
                errors.append(f"{name or idx}.cloud_init.ssh_authorized_keys_file must be a path string.")
            else:
                try:
                    read_ssh_authorized_keys_file(key_file)
                except ValueError as exc:
                    errors.append(f"{name or idx}.cloud_init: {exc}")

        if not name:
            errors.append(f"vms[{idx}].name is required.")
        elif name in seen_names:
            errors.append(f"Duplicate VM name: {name}")
        else:
            seen_names.add(name)
            mac = stable_mac_address(name)
            if mac in seen_macs:
                errors.append(f"Generated MAC address collision: {mac}")
            seen_macs.add(mac)

        for field in ("vcpus", "memory_gb", "disk_gb"):
            try:
                if float(vm.get(field, 0)) <= 0:
                    errors.append(f"{name or idx}.{field} must be > 0.")
            except (TypeError, ValueError):
                errors.append(f"{name or idx}.{field} must be numeric.")

        ip = vm.get("ip")
        if ip:
            try:
                addr = ipaddress.ip_address(ip)
                if addr not in subnet:
                    errors.append(f"{name}.ip {ip} is outside {subnet}.")
                if str(addr) in seen_ips:
                    errors.append(f"Duplicate VM IP: {ip}")
                seen_ips.add(str(addr))
            except ValueError:
                errors.append(f"{name}.ip is invalid: {ip}")

    if errors:
        return errors

    if str(gateway) in seen_ips:
        errors.append("A VM IP cannot equal the network gateway.")

    if not isinstance(uri, str) or not uri:
        errors.append("libvirt.uri must be a non-empty string.")

    return errors
