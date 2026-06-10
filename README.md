# Proxmox Cluster Update Script

`update.sh` is a Bash helper for updating every node in a Proxmox VE cluster from a single Proxmox node.

It detects cluster members with `pvecm nodes`, connects to each node over SSH as `root`, runs package maintenance in parallel, displays a live status table, and prints a final per-node summary with package and kernel information.

## What It Does

- Detects Proxmox VE cluster nodes automatically.
- Verifies SSH access to each node before updating it.
- Runs `apt update`.
- Simulates `apt-get full-upgrade` to list packages expected to change.
- Runs `apt -y full-upgrade` when upgrades are available.
- Runs `apt -y autoremove`.
- Runs `apt clean`.
- Checks whether a reboot is likely required by comparing the running kernel with the latest installed kernel and checking `/var/run/reboot-required`.
- Shows a final summary for each node, including failed update steps.

## Requirements

- A Proxmox VE cluster node.
- Bash 4 or newer.
- Root privileges.
- `pvecm` available on the host where the script is launched.
- SSH key-based root login from the launching node to every cluster node.
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

The script does not reboot nodes automatically. If the final summary reports that one or more nodes require a reboot, reboot them manually in a controlled order that is appropriate for your cluster workloads.

## Important Notes

The script runs updates on all detected nodes in parallel. This is convenient, but it may be risky for production clusters if every node hosts critical guests, storage services, or quorum-sensitive workloads. For production environments, consider updating one node at a time after migrating or shutting down affected workloads.

The script uses `apt -y full-upgrade`, so package prompts are answered automatically where possible. Repository issues, held packages, broken dependencies, or interactive maintainer prompts can still cause failures.

SSH commands use batch mode, a 10-second connection timeout, one connection attempt, and server-alive checks to detect dead SSH sessions. These settings do not forcibly terminate a remote package command that is still running but waiting internally.

If `apt update` fails on a node, that node is skipped for the remaining upgrade steps and reported as failed in the final summary. Failures from later maintenance steps are also reported in the summary.

The package list in the final summary comes from the pre-upgrade simulation. If the real upgrade behaves differently because repositories change, packages are held, or dependency resolution changes during execution, the summary may not perfectly match what was installed.

## Suggested Improvements

- Add a serial mode, or a configurable concurrency limit, so production clusters can update one node at a time.
- Add a dry-run mode that only performs SSH checks, `apt update`, upgrade simulation, and reboot detection.
- Record detailed per-node logs instead of discarding command output with `&>/dev/null`.
- Use `DEBIAN_FRONTEND=noninteractive` and explicit `apt-get` options for more predictable unattended upgrades.
- Add a remote command timeout for package-management commands that are still connected but waiting indefinitely.
- Add an option to exclude specific nodes or target only selected nodes.

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
```
