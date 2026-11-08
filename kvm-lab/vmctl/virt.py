from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

try:
    import libvirt
except ImportError:  # pragma: no cover - optional dependency
    libvirt = None

try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency
    yaml = None

from .config import die, read_ssh_authorized_keys_file, require_libvirt, stable_mac_address


def connect(uri: str):
    require_libvirt()
    conn = libvirt.open(uri)
    if conn is None:
        die(f"Unable to connect to libvirt: {uri}")
    return conn


def storage_pool(conn, name: str):
    try:
        return conn.storagePoolLookupByName(name)
    except libvirt.libvirtError:
        die(f"Storage pool not found: {name}")


def pool_path(pool) -> Path:
    xml = pool.XMLDesc(0)
    root = ET.fromstring(xml)
    path = root.findtext("./target/path")
    if not path:
        die(f"Could not determine storage pool path for {pool.name()}")
    return Path(path)


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def ensure_qcow_disk(base_image: Path, disk_path: Path, size_gb: int, dry_run: bool = False) -> None:
    if disk_path.exists():
        return

    disk_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  disk {disk_path.name}: create qcow2 backing image")
    if dry_run:
        return

    if shutil.which("qemu-img") is None:
        die("qemu-img not found. Install qemu-utils.")

    cmd = [
        "qemu-img", "create",
        "-f", "qcow2",
        "-F", "qcow2",
        "-b", str(base_image),
        str(disk_path),
        f"{size_gb}G",
    ]
    result = run(cmd, check=False)
    if result.returncode != 0:
        die(f"qemu-img failed: {result.stderr.strip()}")


def yaml_dump(data: Any) -> str:
    if yaml is None:
        return __import__("json").dumps(data, indent=2)
    return yaml.safe_dump(data, sort_keys=False)


def cloud_init_files(vm: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    ci = vm.get("cloud_init", {})
    if not isinstance(ci, dict):
        ci = {}

    hostname = ci.get("hostname", vm["name"])
    username = ci.get("username", vm.get("username", "lab"))
    password = ci.get("password")
    ssh_keys = ci.get("ssh_authorized_keys", [])
    ssh_key_file = ci.get("ssh_authorized_keys_file")
    packages = ci.get("packages", [])
    runcmd = ci.get("runcmd", [])
    write_files = ci.get("write_files", [])

    if not isinstance(ssh_keys, list):
        ssh_keys = [ssh_keys]
    if ssh_key_file:
        try:
            ssh_keys.extend(read_ssh_authorized_keys_file(ssh_key_file))
        except ValueError as exc:
            die(str(exc))
    if not isinstance(packages, list):
        packages = [packages]
    if not isinstance(runcmd, list):
        runcmd = [runcmd]
    if not isinstance(write_files, list):
        write_files = [write_files]

    user_block = {
        "name": username,
        "sudo": "ALL=(ALL) NOPASSWD:ALL",
        "groups": ["sudo"],
        "shell": "/bin/bash",
        "lock_passwd": password is None,
        "ssh_authorized_keys": ssh_keys,
    }
    if password:
        user_block["plain_text_passwd"] = password

    user_data_obj = {
        "#cloud-config": None,
        "hostname": hostname,
        "manage_etc_hosts": True,
        "users": ["default", user_block],
        "ssh_pwauth": bool(password),
        "package_update": bool(packages),
        "packages": packages,
        "write_files": write_files,
        "runcmd": runcmd,
    }

    lines = ["#cloud-config"]
    user_data_obj.pop("#cloud-config", None)
    if yaml is None:
        die("PyYAML is required for cloud-init generation.")
    lines.append(yaml.safe_dump(user_data_obj, sort_keys=False).rstrip())

    user_data = output_dir / "user-data"
    meta_data = output_dir / "meta-data"
    user_data.write_text("\n".join(lines) + "\n")
    meta_data.write_text(
        f"instance-id: {vm['name']}\n"
        f"local-hostname: {hostname}\n"
    )
    return user_data, meta_data


def create_cloud_init_iso(vm: dict[str, Any], iso_path: Path, dry_run: bool = False) -> None:
    with tempfile.TemporaryDirectory(prefix=f"vmctl-{vm['name']}-") as tmp:
        tmp_path = Path(tmp)
        user_data, meta_data = cloud_init_files(vm, tmp_path)

        if dry_run:
            print(f"  cloud-init ISO {iso_path.name}: create")
            return

        iso_path.parent.mkdir(parents=True, exist_ok=True)

        if shutil.which("cloud-localds"):
            cmd = ["cloud-localds", str(iso_path), str(user_data), str(meta_data)]
        elif shutil.which("genisoimage"):
            cmd = [
                "genisoimage", "-output", str(iso_path),
                "-volid", "cidata", "-joliet", "-rock",
                str(user_data), str(meta_data),
            ]
        elif shutil.which("mkisofs"):
            cmd = [
                "mkisofs", "-output", str(iso_path),
                "-volid", "cidata", "-joliet", "-rock",
                str(user_data), str(meta_data),
            ]
        else:
            die("Need cloud-localds, genisoimage, or mkisofs for cloud-init ISO generation.")

        result = run(cmd, check=False)
        if result.returncode != 0:
            die(f"Failed to create cloud-init ISO: {result.stderr.strip()}")


def find_vm(conn, name: str):
    try:
        return next(
            (domain for domain in conn.listAllDomains(0) if domain.name() == name),
            None,
        )
    except libvirt.libvirtError as exc:
        die(f"Unable to list libvirt domains while looking for {name}: {exc}")


def vm_exists(conn, name: str) -> bool:
    return find_vm(conn, name) is not None


def vm_xml(vm: dict[str, Any], disk_path: Path, iso_path: Path, network_name: str) -> str:
    memory_mib = int(float(vm["memory_gb"]) * 1024)
    vcpus = int(vm["vcpus"])
    name = vm["name"]
    mac = vm.get("mac_address", stable_mac_address(name))

    return f"""<domain type='kvm'>
  <name>{name}</name>
  <memory unit='MiB'>{memory_mib}</memory>
  <currentMemory unit='MiB'>{memory_mib}</currentMemory>
  <vcpu placement='static'>{vcpus}</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
    <apic/>
  </features>
  <cpu mode='host-passthrough'/>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>restart</on_crash>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' discard='unmap'/>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{iso_path}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
    <interface type='network'>
      <source network='{network_name}'/>
      <mac address='{mac}'/>
      <model type='virtio'/>
    </interface>
    <channel type='unix'>
      <source mode='bind' path='/var/lib/libvirt/qemu/{name}.agent'/>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
    <console type='pty'>
      <target type='serial' port='0'/>
    </console>
    <serial type='pty'>
      <target type='isa-serial' port='0'/>
    </serial>
    <graphics type='spice' autoport='yes' listen='127.0.0.1'/>
    <video>
      <model type='virtio'/>
    </video>
    <memballoon model='virtio'/>
  </devices>
</domain>"""


def ensure_vm(conn, cfg: dict[str, Any], vm: dict[str, Any], dry_run: bool = False) -> None:
    pool = storage_pool(conn, cfg["libvirt"].get("storage_pool", "default"))
    storage_path = pool_path(pool)
    base_image = Path(os.path.expanduser(cfg["libvirt"]["base_image"])).resolve()

    name = vm["name"]
    disk_path = storage_path / f"{name}.qcow2"
    iso_path = storage_path / f"{name}-seed.iso"

    if vm_exists(conn, name):
        print(f"  VM {name}: exists")
        return

    ensure_qcow_disk(base_image, disk_path, int(float(vm["disk_gb"])), dry_run)
    create_cloud_init_iso(vm, iso_path, dry_run)

    xml = vm_xml(vm, disk_path, iso_path, cfg["network"]["name"])
    print(f"  VM {name}: define")
    if dry_run:
        return

    domain = conn.defineXML(xml)
    if domain is None:
        die(f"Failed to define VM {name}")

    if vm.get("autostart", True):
        domain.setAutostart(1)

    domain.create()
    print(f"  VM {name}: started")


def existing_vm_mac(conn, vm_name: str, network_name: str) -> str | None:
    domain = find_vm(conn, vm_name)
    if domain is None:
        return None

    root = ET.fromstring(domain.XMLDesc(0))
    for interface in root.findall("./devices/interface"):
        source = interface.find("source")
        mac = interface.find("mac")
        if (
            source is not None
            and source.get("network") == network_name
            and mac is not None
            and mac.get("address")
        ):
            return mac.get("address", "").lower()
    return None


def destroy_vm(conn, name: str, pool_path_value: Path, dry_run: bool = False) -> None:
    if not vm_exists(conn, name):
        print(f"  VM {name}: absent")
        return

    domain = find_vm(conn, name)
    if domain is None:
        return
    print(f"  VM {name}: destroy and undefine")
    if dry_run:
        return

    try:
        if domain.isActive():
            domain.destroy()
    except libvirt.libvirtError:
        pass

    try:
        domain.undefine()
    except libvirt.libvirtError as exc:
        die(f"Could not undefine {name}: {exc}")

    for suffix in (".qcow2", "-seed.iso"):
        path = pool_path_value / f"{name}{suffix}"
        if path.exists():
            path.unlink()
            print(f"  removed {path}")
