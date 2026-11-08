# vmctl

A small declarative Python tool for provisioning local libvirt/KVM VMs from YAML or JSON.

It creates:

- libvirt VMs
- thin/dynamic qcow2 disks using a base cloud image as a backing file
- a libvirt NAT network
- cloud-init NoCloud seed ISOs
- configurable vCPU/RAM/disk/name/IP
- VM autostart
- idempotent `apply`
- `plan`, `validate`, `status`, and `destroy`

The intended use is a local enterprise/platform-engineering lab on Linux/Zorin OS.

## 1. Host prerequisites

On Ubuntu/Zorin-like systems:

```bash
sudo apt update
sudo apt install -y \
  qemu-kvm \
  libvirt-daemon-system \
  libvirt-clients \
  libvirt-dev \
  build-essential \
  pkg-config \
  virt-manager \
  qemu-utils \
  cloud-image-utils \
  python3-pip \
  python3-venv
```

Install `uv` if it is not already installed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then verify:

```bash
uv --version
```

Enable libvirt:

```bash
sudo systemctl enable --now libvirtd
```

Add your user to the libvirt group:

```bash
sudo usermod -aG libvirt "$USER"
```

Log out/in afterwards.

Verify:

```bash
virsh -c qemu:///system list --all
```

## 2. Prepare the Ubuntu cloud image

This tool creates VMs from an Ubuntu cloud image. The Ubuntu Server installer ISO
(`ubuntu-22.04.3-live-server-amd64.iso`) is not used by this workflow.

Download the Ubuntu 24.04 cloud image:

```text
/var/lib/libvirt/images/ubuntu-24.04-server-cloudimg-amd64.img
```

Example:

```bash
sudo wget -O /var/lib/libvirt/images/ubuntu-24.04-server-cloudimg-amd64.img \
  https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
```

Check it:

```bash
qemu-img info /var/lib/libvirt/images/ubuntu-24.04-server-cloudimg-amd64.img
```

The cloud-image path is configurable in the spec under `libvirt.base_image`.
Both `examples/lab.yaml` and `examples/lab.json` include this setting. If you
save the image elsewhere, update that value to its path; `~` is expanded to
your home directory. For example:

```yaml
libvirt:
  base_image: ~/images/noble-server-cloudimg-amd64.img
```

## 3. Create the Python environment

```bash
git clone <your-repository>
cd <project-root>

uv sync
source .venv/bin/activate
```

`uv sync` installs both the `libvirt-python` binding and PyYAML declared in `pyproject.toml`. On Debian/Ubuntu/Zorin, the `libvirt-dev`, `build-essential`, and `pkg-config` packages installed above provide the headers and build tools needed for the Python binding.

## 4. Validate

```bash
uv run vmctl validate examples/lab.yaml
```

Expected:

```text
Configuration is valid.
```

## 5. Plan

```bash
uv run vmctl plan examples/lab.yaml
```

This does not create anything.

## 6. Apply

Because the default libvirt system connection and storage pool normally require elevated permissions:

```bash
sudo -E env HOME="$HOME" .venv/bin/python vmctl.py apply examples/lab.yaml
```

The tool creates:

```text
devops-lab-network
lab-dns.qcow2
lab-dns-seed.iso
lab-ci.qcow2
lab-ci-seed.iso
lab-harbor.qcow2
lab-harbor-seed.iso
lab-observability.qcow2
lab-observability-seed.iso
lab-sonarqube.qcow2
lab-sonarqube-seed.iso
```

The qcow2 disks use the configured Ubuntu image as a backing file. Their
logical capacities are set in the spec and do not consume the full capacity
immediately.

## 7. Check status

```bash
virsh -c qemu:///system list --all
```

or:

```bash
uv run vmctl status
```

## 8. Destroy

This removes the VMs, their generated qcow2 disks and the lab network:

```bash
sudo -E env HOME="$HOME" .venv/bin/python vmctl.py destroy examples/lab.yaml
```

Use `--yes` to skip the confirmation:

```bash
sudo -E env HOME="$HOME" .venv/bin/python vmctl.py destroy examples/lab.yaml --yes
```

## Configuration

### VM

```yaml
- name: lab-example
  vcpus: 4
  memory_gb: 6
  disk_gb: 100
  ip: 10.10.10.40
  autostart: true
```

### Cloud-init

```yaml
cloud_init:
  hostname: lab-example
  username: lab
  packages:
    - qemu-guest-agent
  runcmd:
    - systemctl enable --now qemu-guest-agent
```

Supported cloud-init fields in v1:

- `hostname`
- `username`
- `password`
- `ssh_authorized_keys`
- `ssh_authorized_keys_file`
- `packages`
- `write_files`
- `runcmd`

For security, prefer SSH keys over passwords.

Example:

```yaml
cloud_init:
  username: lab
  ssh_authorized_keys_file: ~/.ssh/id_ed25519.pub
```

`ssh_authorized_keys_file` reads the public-key text from the specified file
when generating the cloud-init seed. The example uses your local
`~/.ssh/id_ed25519.pub`; make sure this file exists for the user running
`vmctl` (including when using `sudo -E`). Keep the private key
`~/.ssh/id_ed25519` on your client and never put it in the VM configuration.
The key is installed for the configured cloud-init `username`. Changing the
key or username in the spec does not update a VM that has already completed
its first boot; existing guests need an in-guest account update or recreation.

## Networking

The default configuration creates a libvirt NAT network:

```text
10.10.10.0/24
```

with:

```text
gateway:    10.10.10.1
DHCP range: 10.10.10.100-10.10.10.200
```

The example configures these addresses:

```text
DNS:            10.10.10.2
CI:             10.10.10.10
Harbor:         10.10.10.20
Observability:  10.10.10.30
SonarQube:      10.10.10.40
```

During `vmctl apply`, each VM with an `ip` gets a stable MAC address and a
matching libvirt DHCP reservation. The reservation is applied to both the
live network and its persistent definition when the network already exists,
so rerunning `apply` reconciles the configured VM addresses without restarting
the network. VMs continue to use DHCP; Ubuntu receives the reserved address
from libvirt.

The network's `dns_domain` and `dns_forwarder` settings configure libvirt DNS
to forward `lab.home.arpa` queries to the Technitium VM at `10.10.10.2`.
Guests continue using libvirt's DNS proxy, which forwards that internal zone
to Technitium and uses the host's configured upstream DNS for other domains.
When these DNS settings change on an existing network, `vmctl apply` briefly
restarts the network to apply them; guest network connectivity is interrupted
while it is recreated.

Changing a VM's configured IP updates its reservation. Reservations for VMs
removed from the spec are not automatically deleted, so review the libvirt
network definition if you remove VMs from the configuration.

Run `sudo -E env HOME="$HOME" .venv/bin/python vmctl.py apply examples/lab.yaml`
to create or update the network and VMs. Existing VMs keep their current
network MAC address so reservations match their interfaces; newly created VMs
receive a stable generated MAC address.

### Dedicated service VMs

The VM provisioner creates clean Ubuntu guests. Cloud-init creates the `lab`
account, installs its SSH key, and enables the QEMU guest agent; application
stacks and Docker are configured separately by the Ansible project in the
sibling `ansible-vm-configs` directory.

The network is configured to forward `lab.home.arpa` DNS queries to
Technitium at `10.10.10.2`. Until Ansible installs and starts Technitium,
queries for that zone will not resolve. Once it is running, create the
`lab.home.arpa` zone and A records for the lab guests, for example:

| Name | Address |
|---|---|
| `lab-dns.lab.home.arpa` | `10.10.10.2` |
| `lab-ci.lab.home.arpa` | `10.10.10.10` |
| `lab-harbor.lab.home.arpa` | `10.10.10.20` |
| `lab-observability.lab.home.arpa` | `10.10.10.30` |
| `lab-sonarqube.lab.home.arpa` | `10.10.10.40` |

Record creation is intentionally separate from `vmctl apply`; automate it in
Ansible after Technitium is installed and configured. Until those records are
added, libvirt DHCP reservations still provide the addresses, but the names
will not resolve in the `lab.home.arpa` zone.

Configure SonarQube and its database on `lab-sonarqube` through Ansible. Its
assigned address is `10.10.10.40`; the service URL and storage layout depend on
the deployment choices made in those Ansible roles.

Together, the five configured VMs request 28 GB of RAM. Check available host
memory before applying the full spec; adjust per-VM resources if needed.

## Architecture

The intended lab architecture is:

```text
                         Zorin OS
                            |
                         libvirt
                            |
                   lab-network 10.10.10.0/24
                            |
          +-----------------+-----------------+
          |                 |                 |
       CI VM            Harbor VM       Observability VM
       10.10.10.10      10.10.10.20       10.10.10.30
          |                 |                 |
       Gitea/            Registry          Prometheus
       Woodpecker                         Grafana
       Trivy                             Loki
       Semgrep                           Tempo
       Gitleaks                          OTel
          |
          +-------------------+
                              |
                            Kind
                       Kubernetes cluster
                              |
                     Astronomy Shop
                       Argo CD
                       Security
                      AI debugger
```

This separation is intentional: CI, registry and observability represent shared enterprise platform services, while Kind represents an application environment.

## Safety notes

- Do not expose libvirt/KVM management ports to the Internet.
- Do not put passwords in a committed YAML file.
- Use SSH keys for cloud-init.
- The default network is NATed and is suitable for a local lab.
- The tool does not modify your existing VMs unless their names collide with the requested names.
- `destroy` deletes generated VM disks, so review the configuration before running it.

## Roadmap

Potential v2 improvements:

1. Real libvirt DHCP reservations for requested static IPs.
2. Multiple networks per VM.
3. Separate management/application networks.
4. VLAN support.
5. Cloud-init network configuration.
6. VM cloning from arbitrary base images.
7. CPU pinning/NUMA configuration.
8. Memory ballooning.
9. Secure boot/TPM options.
10. Terraform-like state.
11. `diff` and reconciliation.
12. SSH readiness checks.
13. Ansible bootstrap hooks.
14. Kubeadm cluster provisioning.
15. Automated DNS records.
16. Virtual router/firewall VM for network-failure experiments.
