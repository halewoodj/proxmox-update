# Proxmox Cluster Maintenance Scripts

`update.sh` is a Bash helper for updating every node in a Proxmox VE cluster from a Proxmox node or a remote controller.

`rolling-reboot.sh` is a Bash helper for safely rebooting Proxmox VE cluster nodes one at a time after evacuating guests and checking cluster/Ceph health.

It detects cluster members with `pvecm nodes`, connects to each node over SSH as `root`, runs package maintenance with configurable concurrency, displays a live status table, and prints a final per-node summary with package and kernel information.

## What It Does

- Detects Proxmox VE cluster nodes automatically.
- Verifies SSH access to each node before updating it.
- Updates one node at a time by default.
- Supports dry-run checks without applying package changes.
- Supports selecting or excluding specific nodes.
- Runs `apt-get update`.
- Simulates `apt-get dist-upgrade` to list packages expected to be installed, upgraded, or removed.
- Runs `apt-get -y dist-upgrade` when package changes are available.
- Runs `apt-get -y autoremove`.
- Runs `apt-get clean`.
- Reports the Proxmox VE version for each node.
- Checks whether a reboot is likely required by comparing the running kernel with the latest installed kernel and checking `/var/run/reboot-required`.
- Reports the reason a reboot is needed when detected.
- Shows a final summary for each node, including package changes and failed update steps.

## Requirements

- A Proxmox VE cluster node.
- Bash 4 or newer.
- Root privileges.
- `pvecm` on the local Proxmox node, or on the remote node selected with `--cluster-host`.
- SSH key-based root login from the controller to every cluster node.
- Working Proxmox/Debian package repositories on each node.

## Usage

Review the script before running it:

```bash
less update.sh
```

Make it executable:

```bash
chmod +x update.sh
```

Run it as root from a Proxmox VE cluster node:

```bash
./update.sh
```

By default, the script updates one node at a time. To update more than one node at once, set an explicit job count:

```bash
./update.sh --jobs 2
```

To update all detected nodes at once, use:

```bash
./update.sh --parallel
```

To check available package changes and reboot status without applying package changes, use:

```bash
./update.sh --dry-run
```

From a remote Linux controller with Bash 4+, use:

```bash
./update.sh --cluster-host root@pve1 --dry-run
```

The controller must resolve and connect directly to every detected node name.
Non-interactive runs emit status changes and the final summary without repeatedly
redrawing a terminal dashboard. Any node failure produces a non-zero exit code.

To process only specific nodes, use a comma-separated list:

```bash
./update.sh --nodes pve1,pve3
```

To skip specific nodes, use:

```bash
./update.sh --exclude pve2
```

You can combine these options:

```bash
./update.sh --dry-run --nodes pve1,pve3 --jobs 2
```

The script does not reboot nodes automatically. If the final summary reports that one or more nodes require a reboot, reboot them manually in a controlled order that is appropriate for your cluster workloads.

## Rolling Reboots

Review the script before running it:

```bash
less rolling-reboot.sh
```

Make it executable:

```bash
chmod +x rolling-reboot.sh
```

Run a dry run first:

```bash
./rolling-reboot.sh --dry-run
```

Run the rolling reboot from a Proxmox VE cluster node:

```bash
./rolling-reboot.sh
```

The script reboots one selected node at a time. Before each reboot it:

- Confirms cluster quorum is healthy.
- Confirms Ceph reports `HEALTH_OK`.
- Migrates VMs and containers away from the node.
- Verifies no selected-scope guests remain on that node.
- Reboots the node.
- Waits for the node, Proxmox services, quorum, and Ceph `HEALTH_OK` to recover before continuing.

By default, `rolling-reboot.sh` migrates all VMs/CTs it finds on each node, including stopped guests. To drain only running workloads, use:

```bash
./rolling-reboot.sh --running-only
```

To reboot only specific nodes, use:

```bash
./rolling-reboot.sh --nodes pve1,pve3
```

To exclude nodes from rebooting, use:

```bash
./rolling-reboot.sh --exclude pve2
```

For guests on local storage, default migration commands may fail. The script stops instead of rebooting if a guest cannot be migrated. If your cluster needs explicit migration options, you can pass a target storage name:

```bash
./rolling-reboot.sh --target-storage local-zfs
```

For QEMU guests that require local disk migration, you can also pass:

```bash
./rolling-reboot.sh --qm-with-local-disks
```

If the node running the script is selected for reboot, it is moved to the end of the order. After it is evacuated and its reboot is scheduled, the script exits because its own host is rebooting.

`rolling-reboot.sh` does not migrate guests back to their original nodes after the rolling reboot. It leaves guests where the successful migration commands placed them.

## Important Notes

The script updates one node at a time unless you pass `--jobs` or `--parallel`. Increasing concurrency can be useful in lab clusters or planned maintenance windows, but it may be risky for production clusters if multiple nodes host critical guests, storage services, or quorum-sensitive workloads.

The script uses `apt-get -y dist-upgrade`, following Proxmox's documented CLI update path. Package prompts are answered automatically where possible, but repository issues, held packages, broken dependencies, or interactive maintainer prompts can still cause failures.

Package commands run with `DEBIAN_FRONTEND=noninteractive` and dpkg options that keep existing config files when a package asks how to handle a changed config file.

SSH commands use batch mode, a 10-second connection timeout, one connection attempt, and server-alive checks to detect dead SSH sessions. Remote commands are also wrapped with `timeout`: quick checks use 120 seconds, package update and maintenance steps use 1800 seconds, and `dist-upgrade` uses 7200 seconds.

If `apt-get update` or the upgrade simulation fails on a node, that node is skipped for the remaining upgrade steps and reported as failed in the final summary. If `dist-upgrade` fails, the script stops package maintenance for that node instead of continuing to `autoremove` or `clean`. Failures from later maintenance steps are also reported in the summary.

In dry-run mode, the script still runs `apt-get update`, upgrade simulation, and reboot checks on selected nodes. It does not run `dist-upgrade`, `autoremove`, or `clean`.

The package change list in the final summary comes from the pre-upgrade simulation. If the real upgrade behaves differently because repositories change, packages are held, or dependency resolution changes during execution, the summary may not perfectly match what was installed.

Reboot reasons are reported as `kernel mismatch`, `/var/run/reboot-required`, or both.

For rolling reboots, the Ceph gate requires `ceph health` to begin with `HEALTH_OK` before a node is rebooted and after each remote node returns. Clusters with persistent Ceph warnings will intentionally block until the warning is fixed or until you explicitly run with `--skip-ceph-check`.

Migration target selection is automatic. For each guest, the reboot script tries available cluster nodes in order of lowest current guest count and stops if the guest cannot be moved away from the node being rebooted.

## Suggested Improvements

- Record detailed per-node logs instead of discarding command output with `&>/dev/null`.

## Safety Checklist

Before running this on a live cluster:

1. Confirm you have working backups.
2. Confirm cluster quorum is healthy.
3. Confirm SSH root access works to all nodes.
4. Confirm repositories are configured correctly.
5. Migrate or stop workloads as needed.
6. Plan any required reboots.

## Verification

The script has been checked for Bash syntax with:

```bash
bash -n update.sh
bash -n rolling-reboot.sh
```

## Semaphore

Use a **Bash** task template with `semaphore/run.sh` as the entry point. The
controller needs Bash, Python 3 and OpenSSH. Store these in a Semaphore variable
group:

| Variable | Value |
| --- | --- |
| `PVE_NODES_JSON` | JSON object mapping every approved cluster node name to its management IPv4 address |
| `PVE_CLUSTER_HOST` | One node name from that inventory |
| `PVE_KNOWN_HOSTS` | Verified Ed25519 host-key lines, indexed by those exact node names |
| `PVE_SSH_PRIVATE_KEY` | **Secret environment variable** containing the dedicated root SSH key |

Create separate templates with arguments `["preview"]` and `["apply"]`.
Keep branch and argument overrides disabled, leave autorun disabled, and set the
project's maximum parallel tasks to **1**. Pin each repository to a reviewed release
tag. No recurring maintenance schedule is required.

Before either mode starts, the adapter verifies SSH hostnames, exact cluster
membership, quorum and Ceph `HEALTH_OK`. It refuses unknown/extra cluster nodes.
The templates use the full configured inventory. A separately configured template
can pass `--nodes pve1,pve2` to select a subset of that approved inventory.

The adapter writes its SSH key into a private temporary directory, removes the key
from subprocess environments and cleans the directory when the task finishes. It
uses strict host-key checking, direct node connections, and a shared controller
lock so update and reboot tasks cannot overlap on the same runner. Give this
project only to operators authorised to maintain those hypervisors. Restrict the
key to the Semaphore controller's source IP and disable forwarding and PTY access.

The updater runs from Semaphore with `--jobs 1`. **Preview** refreshes APT
metadata, simulates upgrades and reports kernel/reboot status; it does not install,
remove or clean packages. **Apply** performs package maintenance. Neither mode
reboots nodes. Run the separate rolling-reboot preview and apply tasks when needed.

Run the adapter and script failure-path checks on a Linux controller:

```bash
python3 -m unittest discover -s semaphore -p 'test_*.py' -v
```

The tests use fake SSH; they never connect to a cluster or run package maintenance.
