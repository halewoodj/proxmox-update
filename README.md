# Proxmox Cluster Update Script

`update.sh` is a Bash helper for updating every node in a Proxmox VE cluster from a single Proxmox node.

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
- Checks whether a reboot is likely required by comparing the running kernel with the latest installed kernel and checking `/var/run/reboot-required`.
- Shows a final summary for each node, including package changes and failed update steps.

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

## Important Notes

The script updates one node at a time unless you pass `--jobs` or `--parallel`. Increasing concurrency can be useful in lab clusters or planned maintenance windows, but it may be risky for production clusters if multiple nodes host critical guests, storage services, or quorum-sensitive workloads.

The script uses `apt-get -y dist-upgrade`, following Proxmox's documented CLI update path. Package prompts are answered automatically where possible, but repository issues, held packages, broken dependencies, or interactive maintainer prompts can still cause failures.

Package commands run with `DEBIAN_FRONTEND=noninteractive` and dpkg options that keep existing config files when a package asks how to handle a changed config file.

SSH commands use batch mode, a 10-second connection timeout, one connection attempt, and server-alive checks to detect dead SSH sessions. Remote commands are also wrapped with `timeout`: quick checks use 120 seconds, package update and maintenance steps use 1800 seconds, and `dist-upgrade` uses 7200 seconds.

If `apt-get update` or the upgrade simulation fails on a node, that node is skipped for the remaining upgrade steps and reported as failed in the final summary. If `dist-upgrade` fails, the script stops package maintenance for that node instead of continuing to `autoremove` or `clean`. Failures from later maintenance steps are also reported in the summary.

In dry-run mode, the script still runs `apt-get update`, upgrade simulation, and reboot checks on selected nodes. It does not run `dist-upgrade`, `autoremove`, or `clean`.

The package change list in the final summary comes from the pre-upgrade simulation. If the real upgrade behaves differently because repositories change, packages are held, or dependency resolution changes during execution, the summary may not perfectly match what was installed.

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
```
