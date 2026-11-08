from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path

try:
    import libvirt
except ImportError:  # pragma: no cover - optional dependency
    libvirt = None

from .config import die, load_config, merge_defaults, require_libvirt, stable_mac_address, validate_config
from .network import ensure_network, network_exists
from .virt import connect, destroy_vm, ensure_vm, existing_vm_mac, pool_path, storage_pool, vm_exists


def cmd_validate(args) -> None:
    cfg = load_config(Path(args.config))
    errors = validate_config(cfg)
    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        raise SystemExit(1)
    print("Configuration is valid.")


def cmd_plan(args) -> None:
    cfg = load_config(Path(args.config))
    errors = validate_config(cfg)
    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        raise SystemExit(1)

    uri = cfg["libvirt"].get("uri", "qemu:///system")
    conn = connect(uri)
    try:
        print(f"libvirt: {uri}")
        print(f"network: {cfg['network']['name']}")
        pool = storage_pool(conn, cfg["libvirt"].get("storage_pool", "default"))
        print(f"storage: {pool.name()} -> {pool_path(pool)}")
        print("VMs:")
        for raw in cfg["vms"]:
            vm = merge_defaults(raw, cfg.get("defaults", {}))
            state = "exists" if vm_exists(conn, vm["name"]) else "create"
            print(
                f"  {vm['name']}: {state}, "
                f"{vm['vcpus']} vCPU, {vm['memory_gb']} GB RAM, "
                f"{vm['disk_gb']} GB qcow2, IP={vm.get('ip', 'DHCP')}"
            )
    finally:
        conn.close()


def cmd_apply(args) -> None:
    cfg = load_config(Path(args.config))
    errors = validate_config(cfg)
    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        raise SystemExit(1)

    conn = connect(cfg["libvirt"].get("uri", "qemu:///system"))
    try:
        vms = [merge_defaults(raw, cfg.get("defaults", {})) for raw in cfg["vms"]]
        reservations: list[tuple[str, str, str]] = []
        for vm in vms:
            existing_mac = existing_vm_mac(conn, vm["name"], cfg["network"]["name"])
            if vm.get("ip") and vm_exists(conn, vm["name"]) and existing_mac is None:
                die(
                    f"VM {vm['name']} exists but has no interface on network "
                    f"{cfg['network']['name']}; cannot reserve its IP."
                )
            vm["mac_address"] = existing_mac or stable_mac_address(vm["name"])
            if vm.get("ip"):
                reservations.append((vm["name"], vm["mac_address"], str(ipaddress.ip_address(vm["ip"]))))

        print("Network:")
        ensure_network(conn, cfg, reservations)
        print("VMs:")
        for vm in vms:
            ensure_vm(conn, cfg, vm)
        print("Apply complete.")
    finally:
        conn.close()


def cmd_destroy(args) -> None:
    cfg = load_config(Path(args.config))
    errors = validate_config(cfg)
    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        raise SystemExit(1)

    if not args.yes:
        answer = input("Destroy VMs and their disks defined in this config? [y/N] ")
        if answer.lower() != "y":
            print("Cancelled.")
            return

    conn = connect(cfg["libvirt"].get("uri", "qemu:///system"))
    try:
        pool = storage_pool(conn, cfg["libvirt"].get("storage_pool", "default"))
        p = pool_path(pool)
        for raw in cfg["vms"]:
            vm = merge_defaults(raw, cfg.get("defaults", {}))
            destroy_vm(conn, vm["name"], p)

        name = cfg["network"]["name"]
        if network_exists(conn, name):
            net = conn.networkLookupByName(name)
            if net.isActive():
                net.destroy()
            net.undefine()
            print(f"  network {name}: removed")
    finally:
        conn.close()


def cmd_status(args) -> None:
    require_libvirt()
    conn = connect(args.uri)
    try:
        domains = conn.listAllDomains(0)
        if not domains:
            print("No VMs found.")
            return

        print(f"{'NAME':30} {'STATE':12} {'VCPU':>4} {'MEM(MB)':>8}")
        print("-" * 60)
        for d in sorted(domains, key=lambda x: x.name()):
            state, maxmem, mem, nrvcpu, cpu = d.info()
            state_map = {
                libvirt.VIR_DOMAIN_RUNNING: "running",
                libvirt.VIR_DOMAIN_BLOCKED: "blocked",
                libvirt.VIR_DOMAIN_PAUSED: "paused",
                libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown",
                libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
                libvirt.VIR_DOMAIN_CRASHED: "crashed",
            }
            print(f"{d.name():30} {state_map.get(state, str(state)):12} {nrvcpu:4} {mem // 1024:8}")
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Declarative libvirt/KVM VM provisioner")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate")
    p.add_argument("config")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("plan")
    p.add_argument("config")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("apply")
    p.add_argument("config")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("destroy")
    p.add_argument("config")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_destroy)

    p = sub.add_parser("status")
    p.add_argument("--uri", default="qemu:///system")
    p.set_defaults(func=cmd_status)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
