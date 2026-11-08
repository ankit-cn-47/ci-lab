# Ansible VM configuration

This repository configures guests provisioned by the separate `kvm-lab`
project. It configures Technitium DNS and Smallstep CA on `lab-dns`
(`10.10.10.2`), distributes the lab CA trust to the VMs and KVM host, and
configures Docker, native Gitea and Woodpecker services, and Nginx on the CI
VM (`lab-ci`, `10.10.10.10`).

The roles are deliberately composable:

- `technitium_dns`: install Technitium directly on the DNS VM as a native
  `systemd` service and create the lab zone and VM/service A records.
- `step_ca_server`: run Smallstep CA on the DNS VM and issue the lab root.
- `ca_trust`: install the public lab CA root in the system trust store on all
  inventoried guests and the KVM host.
- `lab_dns_client`: configure split-DNS routing for `lab.home.arpa` on the
  KVM host. Guests continue to use libvirt's DNS proxy, which forwards that
  zone to Technitium.
- `gitea`: install Gitea as a native `systemd` service with persistent data.
- `docker`: install and enable Docker Engine on the CI VM, used only by
  Woodpecker to execute pipeline steps in containers.
- `woodpecker`: install the server and agent as native `systemd` services;
  only pipeline steps run as Docker containers. The agent's Docker-group
  membership grants it root-equivalent access to the VM.
- `step_ca_client`: issue and automatically renew TLS certificates for the
  Gitea and Woodpecker Nginx virtual hosts.
- `nginx`: terminate HTTPS using the lab CA certificates and proxy the two
  DNS names to the loopback-only application listeners.

Application containers are not installed by `kvm-lab` cloud-init. The
provisioner creates the guests and their base login/QEMU guest agent; this
repository owns in-guest services.

## Prerequisites

On the Ansible controller, create the Python environment and install the
versions pinned in `requirements.txt`:

```bash
python3 -m venv ansible-venv
ansible-venv/bin/python -m pip install -r requirements.txt
ansible-venv/bin/ansible all -m ansible.builtin.ping
```

Run Ansible from this repository's root. The project directory must not be
world-writable or Ansible ignores its `ansible.cfg`. Confirm the effective
permissions with:

```bash
stat -c '%A %a %n' . ansible.cfg inventory inventory/hosts.yml
```

If `chmod` does not change the project directory's permissions, the workspace
may be on a mount that does not preserve Unix permissions. Do not run privileged
playbooks from a world-writable project directory. Either move/copy the project
to a filesystem with normal Unix permissions, or store a private Ansible config
outside the workspace and point it at this project's inventory and roles:

```bash
mkdir -p ~/.config/ansible
cat > ~/.config/ansible/ansible-vm-configs.cfg <<'EOF'
[defaults]
inventory = /home/ankit/data/workspace/ansible-vm-configs/inventory/hosts.yml
roles_path = /home/ankit/data/workspace/ansible-vm-configs/roles
interpreter_python = auto_silent
retry_files_enabled = False

[ssh_connection]
pipelining = True
EOF
chmod 600 ~/.config/ansible/ansible-vm-configs.cfg
export ANSIBLE_CONFIG="$HOME/.config/ansible/ansible-vm-configs.cfg"
ansible all --list-hosts
```

Also check the filesystem and parent directory permissions if the project
directory remains world-writable:

```bash
stat -c '%A %a %n' . ..
findmnt -T . -o TARGET,FSTYPE,OPTIONS
```

The current inventory uses the `lab` account and `~/.ssh/id_ed25519` to reach
`lab-ci`. Adjust `inventory/hosts.yml` if your SSH configuration differs.
The playbook uses `become: true`; the SSH account must have sudo access.

## Configure Gitea and Woodpecker

The configured private zone is `lab.home.arpa`, matching `dns_domain` in
`kvm-lab/examples/lab.yaml`. It is defined once in `group_vars/all.yml`; keep
the KVM network's `dns_domain` in sync if you change it. The service names,
URLs, DNS records, and CA endpoint derive from that zone:

| Service | URL |
|---|---|
| Gitea | `https://gitea.<lab_domain>/` |
| Woodpecker | `https://woodpecker.<lab_domain>/` |

The playbook creates records for the lab VMs, Gitea, Woodpecker, and CA in
Technitium. The KVM host gets split DNS routing for the lab zone; libvirt
continues forwarding guest queries for that zone to `10.10.10.2`. TLS
certificates are issued from Smallstep and renewed automatically by
systemd-managed renewal processes.

Technitium and Smallstep run directly on `lab-dns`; no Docker is installed
there. DNS listens on TCP/UDP port `53`, Technitium's web console is on port
`5380`, and the CA API is on HTTPS port `9000`. Nginx handles HTTP(S) web
traffic, not DNS packets, so DNS is served directly by Technitium. Keep the
web console and CA API reachable only inside the trusted lab network.

### Secrets and first deployment

Create the shared vault file for the DNS admin and CA signing credentials.
Replace all example values with different, randomly generated secrets before
encrypting it:

```bash
cp group_vars/all/vault.yml.example group_vars/all/vault.yml
ansible-vault encrypt group_vars/all/vault.yml
```

Deploy the DNS server and CA, distribute the root certificate to all
inventoried machines, and configure the KVM host's lab DNS routing:

```bash
ansible-playbook playbooks/site.yml \
  --tags dns,ca,ca_trust,dns_client \
  --ask-vault-pass
```

Ansible manages the KVM host locally, so run this playbook from that host.
The Technitium installer configures the dedicated DNS VM to use its local DNS
service. The CA root key is encrypted on the DNS VM with its vault-managed
password, and the online CA issues service certificates. Keep backups of the
CA state and passwords; for stronger isolation, keep a root CA offline and
use Smallstep as an intermediate CA.

Then install Gitea, issue its Nginx certificate, and configure the HTTPS
reverse proxies:

```bash
ansible-playbook playbooks/site.yml \
  --tags docker,gitea,nginx \
  --ask-vault-pass
```

Complete Gitea's initial setup in the browser. Gitea registration is disabled,
so create accounts for the intended users. Create a Gitea OAuth application
using callback URL `https://woodpecker.<lab_domain>/authorize`.

Copy `group_vars/ci/vault.yml.example` to `group_vars/ci/vault.yml`, replace
the OAuth credentials and random Woodpecker agent/gRPC secrets, then encrypt
it:

```bash
cp group_vars/ci/vault.yml.example group_vars/ci/vault.yml
ansible-vault encrypt group_vars/ci/vault.yml
```

Finally deploy Woodpecker with the Vault files available:

```bash
ansible-playbook playbooks/site.yml --ask-vault-pass
```

The local Vault file is excluded by `.gitignore`; never commit unencrypted
credentials. Gitea and Woodpecker listen on loopback-only HTTP ports behind
Nginx. Gitea SSH listens directly on port `2222`. Permit TCP `80`, `443`, and
`2222` as needed. Permit DNS TCP/UDP `53` and the CA's TCP `9000` only within
the lab network. Do not expose Gitea/Woodpecker application ports `3000` and
`8000` outside the VM; Woodpecker gRPC on `9000` is loopback-only on the CI
VM. Woodpecker registration remains closed (`WOODPECKER_OPEN=false`).

Set distinct random values for both the agent and gRPC secrets in the Vault
file.

The Woodpecker agent runs as a dedicated account in the `docker` group so its
pipeline steps execute in containers through Docker Engine. Access to the
Docker socket is effectively root access on `lab-ci`; only trusted
repositories should be enabled for this agent. Workflow concurrency defaults
to two to fit the VM's configured resources.

## Extend to other VMs

Add provisioned guests to groups in `inventory/hosts.yml`, define group
variables under `group_vars/`, and add service-specific roles and plays in
`playbooks/site.yml`. Keep base configuration such as Docker independent from
application roles so it can be reused. Inventory generation from the
provisioner's `examples/lab.yaml` is not implemented yet.
