# Proxmox Cluster Maintenance Scripts

`update.sh` is a Bash helper for updating every node in a Proxmox VE cluster from a Proxmox node or a remote controller.

`rolling-reboot.sh` is a Bash helper for safely rebooting Proxmox VE cluster nodes one at a time after evacuating guests and checking cluster/Ceph health.

It detects cluster members with `pvecm nodes`, connects to each node over SSH as `root`, runs package maintenance one node at a time, displays a live status table, and prints a final per-node summary with package and kernel information.

## What It Does

- Detects Proxmox VE cluster nodes automatically.
- Verifies SSH access to each node before updating it.
- Applies updates one node at a time; parallel apply is refused.
- Stops the queue on the first node failure or missing worker summary.
- Checks every cluster member, quorum, Ceph, package state and unchanged boot IDs at each boundary.
- Refuses package installation on nodes with running guests, an active CRM, or an armed LRM watchdog.
- Supports dry-run checks without applying package changes.
- Supports selecting or excluding specific nodes.
- Runs `apt-get update`.
- Simulates `apt-get dist-upgrade` to list packages expected to be installed, upgraded, or removed.
- Runs `apt-get -y --no-remove dist-upgrade` when package changes are available.
- Retains package caches and old kernels for recovery; cleanup is a separate operation.
- Keeps private per-node diagnostic logs and prints the failing command’s final output.
- Reports the Proxmox VE version for each node.
- Checks whether a reboot is likely required by comparing the running kernel with the latest installed kernel and checking `/var/run/reboot-required`.
- Reports the reason a reboot is needed when detected.
- Shows a final summary for each node, including package changes and failed update steps.

## Requirements

- A Proxmox VE cluster node.
- Bash 4 or newer.
- Root privileges.
- Python 3 on the controller and every node.
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

Package installation always runs one node at a time. `--jobs 2` and `--parallel`
are available only with `--dry-run`.

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

The script does not initiate reboots. An active HA watchdog can still reset an
unsafe host, so apply now requires the maintenance conditions below. Unexpected
boot-ID changes stop the queue even when cluster health has already recovered.

### Automatic HA preparation in Semaphore

Semaphore preview uses existing APT lists, which may be stale, and plans HA preparation. It does not
migrate guests, change services, install packages or reboot nodes. Semaphore apply
uses `--auto-maintenance` to process one node at a time:

Each boundary verifies unchanged boot IDs and consistent Ceph monitor evidence.
A recovered monitor election is recorded and requires two fresh, healthy
observations at the same epoch. A higher election epoch alone does not fail the
rollout. Snapshots crossing an election are read again until observers agree;
persistent disagreement times out. Epoch regression and unexpected reboots still
stop it.

The shared checks used by normal preview and apply wait up to ten minutes for
existing dpkg activity, without deleting locks or interrupting another package
process. A transient read timeout is retried up to three times. APT installation
also waits for its frontend lock. Known `MON_DOWN`/`SLOW_OPS` warnings with monitor
quorum wait up to five minutes for full recovery and two stable observations;
no next-node action starts while waiting. Missing OSDs, unsafe PGs, lost Corosync
quorum, muted checks, package damage and stale HA heartbeats remain hard failures.

The initial check also validates the entire selected rollout against existing HA
maintenance. If NODE-1 is already held, selecting NODE-1 and NODE-2 is blocked
before any node job, APT command or HA change: restoring NODE-1's original settings
would preserve that hold and prevent NODE-2 from proceeding. A scoped
`--nodes NODE-1` run is allowed and still preserves the pre-existing hold. Inspect
and explicitly resolve an existing hold before a wider rollout; the updater does
not clear it automatically.

HA can start returning a guest after a node leaves maintenance. The next node
waits for normal migration/relocation and start/stop transitions to finish, rather
than treating that handoff as an update failure. Readiness and maintenance exit
require two consecutive fresh observations. Waiting uses the existing bounded
maintenance timeout; service errors, fencing/recovery/unknown states, quorum or
unsafe Ceph states and reboots still stop immediately.
Logs name the service or migration task being awaited. No next-node maintenance
request or APT command starts while the handoff is pending.

An active scheduled `vzdump` backup also waits within that bounded readiness
period. No evacuation or package write begins until the backup task finishes.
Other conflicting/unknown task types still stop the run. An existing maintenance
hold still requires explicit recovery; the updater does not clear someone else's
hold to make a full selection proceed.

1. Verify healthy peers, memory headroom, stable HA services and no conflicting
   Proxmox tasks or pending HA commands. Running guests outside HA require manual
   evacuation; the script does not guess their destination or storage options.
2. Request HA node maintenance and wait for all running guests and migration
   workers to leave. HA respects its placement/storage rules;
   migration refusal or timeout stops the run. Container migrations can briefly
   restart containers.
3. Gracefully stop the target CRM, wait for master handover when necessary and
   verify all local watchdog clients have closed using the running multiplexer's
   socket ownership and active-client marker. Some HA versions display
   `maintenance mode, watchdog active` even after the LRM closes its connection;
   that inferred label does not prove the actual connection state.
   Add a runtime CRM mask to keep
   package triggers from starting it during installation.
4. Verify the prepared target before refreshing APT metadata. Disable persistent
   APT binary caches so their rebuild uses memory. Refresh metadata, simulate
   changes and refuse removals. Even a no-change refresh requires protection:
   metadata/cache writes can stall the boot mirror before package installation.
5. Recheck cluster health, boot IDs, package state and the prepared target, then
   install with `--no-remove`. Preserve APT output and verify the result.
6. Remove only the mask introduced by this run, restore the CRM service's prior
   active state, release only maintenance introduced by this run and wait for HA
   actions and cluster health to settle before starting the next node.

The controller retains a private `<node>.maintenance.json` recovery record in
its printed diagnostic directory, recording original settings and each intended
change before issuing it. Pre-existing maintenance and masks are preserved. On
failure or interruption it stops the queue and leaves preparation in place for
inspection. It does not blindly release maintenance after an uncertain or failed
package operation. Check remote APT/dpkg and that recovery record before retrying;
restore the recorded original settings only after validating node health.

APT refresh, simulation and installation run in uniquely named transient systemd
services on the target. A supervisor inside each service must receive controller
heartbeats before it starts package work. EOF requests cancellation immediately;
a missing heartbeat revokes the lease after 15 seconds. PID 1 terminates the
service's entire control group, including dpkg children that create their own
sessions or ignore SSH hangup. A killed Semaphore runner also signals its Linux
worker, and losing the local helper closes the remote lease.

The controller records `<node>.apt.<identity>.json` beside the diagnostic logs
before requesting the remote service. It verifies the owned service has no
remaining processes and that the SSH start/wait request has ended before recording
`quiescent`. If connectivity or blocked kernel I/O prevents verification, the
record says `unverified`, the queue stops and HA preparation is retained. A stopped
Semaphore task alone does not prove package work has ended. The runner waits for
its helpers instead of forcibly killing them after a fixed grace period. Package
processes are not sent SIGKILL; cancellation can leave packages needing repair.

For an uncertain stop, use the exact unit identity in that record and verify its
`Description`, `ActiveState`, `ControlGroup` and remaining processes on the target.
Do not guess a unit name, clear maintenance or retry package operations until
termination and package state have been checked. The service and lease use
unified cgroup v2 and systemd 254 or newer; native validation uses Proxmox's systemd
257. Tests cover EOF, invalid/expired leases, ownership checks, failed termination
proof, runner death, cancellation during package work and preventing the next node.

Reference: [systemd process termination](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html).

For the same automatic flow from a Linux controller:

```bash
./update.sh --auto-maintenance --cluster-host root@pve1 --dry-run
./update.sh --auto-maintenance --cluster-host root@pve1
```

Direct `update.sh` without `--auto-maintenance` retains manual preparation mode.
For one selected node, enable HA node maintenance, wait for evacuation and LRM
watchdog release, gracefully stop its CRM and apply a runtime mask, then run
`./update.sh --nodes <prepared-node>`. After success, restore only settings you
introduced. Never kill HA daemons or watchdog-mux.

All modes check every detected cluster member, including nodes excluded from
installation. They require quorum, fresh HA heartbeats, no boot-ID changes,
clean/unlocked package databases, Ceph HEALTH_OK with no muted checks, every OSD
up/in and all PGs active+clean. Normal scrub/deep-scrub flags are permitted;
recovery, degradation, remapping and unknown states still block the operation.
The updater never disarms HA cluster-wide or requests a host reboot.

These controls address unsafe updating of active HA nodes. The underlying cause
of the previous CRM/LRM stalls is still under investigation.

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

Apply always updates one node at a time. `--jobs` and `--parallel` change preview concurrency only.

The script uses `apt-get -y dist-upgrade`, following Proxmox's documented CLI update path. Package prompts are answered automatically where possible, but repository issues, held packages, broken dependencies, or interactive maintainer prompts can still cause failures.

Package commands run with `DEBIAN_FRONTEND=noninteractive` and dpkg options that keep existing config files when a package asks how to handle a changed config file.

SSH commands use batch mode, a 10-second connection timeout, one connection attempt, and server-alive checks to detect dead SSH sessions. Remote commands are also wrapped with `timeout`: quick checks use 120 seconds, package update and maintenance steps use 1800 seconds, and `dist-upgrade` uses 7200 seconds.

Any failed worker, missing summary, failed package operation or failed verification stops the queue. Later nodes are reported as SKIPPED. Package cleanup is separate from updating; the transaction does not run autoremove or clean.

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

An exceptional operator rollout is available through
`semaphore/operator_apply.sh --acknowledge-storage-stalls --resume-node NODE-1
--resume-state /path/to/NODE-1.maintenance.json --repair-package libpve-notify-perl`.
Use a separate, explicitly authorized Semaphore template for this entry point;
the normal preview/apply templates retain their full health and reboot checks. This
operator path resumes only a verified updater-owned, evacuated preparation,
recovers the named interrupted package and finishes one node's APT operations
before waiting for full Ceph health. It acknowledges election changes only after
all four observers are healthy, boot identities are unchanged and package audits
are clean. It then restores the owned HA/CRM settings and proceeds sequentially.
Package removals and held-back upgrades are refused; kernels are installed
without rebooting hosts. Run independent quorum, boot and HA monitoring throughout
the operator job. This does not repair a storage latency problem.

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

The updater runs from Semaphore with `--jobs 1 --auto-maintenance`. **Preview**
simulates packages from existing APT lists, which may be stale, and prints the automatic maintenance
plan; it does not move guests or change HA/services. **Apply** prepares and restores
one node at a time as described above. Container migration can restart containers;
review this workload impact before applying. Neither mode requests a host reboot.
Keep the release pinned and argument overrides disabled. A scoped template may
select a subset with `--nodes` without changing the approved cluster inventory.

Run the adapter and script failure-path checks on a Linux controller:

```bash
python3 -m unittest discover -s semaphore -p 'test_*.py' -v
```

The tests use fake SSH; they never connect to a cluster or run package maintenance.
