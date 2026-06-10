#!/bin/bash
# =============================================
# Proxmox VE Cluster Update Script
# =============================================
# - Auto-detects cluster nodes via pvecm
# - Runs updates one node at a time by default
# - Shows a live status dashboard for all nodes
# - Summarises updated packages and kernel info
#
# Requirements:
# - Run as root on a Proxmox VE node
# - SSH key-based login between cluster nodes (root)
# =============================================

# ---- COLORS ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

usage() {
  cat <<EOF
Usage: $0 [options]

Options:
  -j, --jobs N      Number of nodes to update at once (default: 1)
  -p, --parallel    Update all detected nodes at once
  -n, --dry-run     Check updates and reboot status without changing packages
      --nodes LIST  Only process comma-separated node names
      --exclude LIST
                    Exclude comma-separated node names
  -h, --help        Show this help message
EOF
}

# ---- ARRAYS / VARIABLES ----
REBOOT_NEEDED=()
FAILED_NODES=()
MAX_PARALLEL=1
DRY_RUN=0
ONLY_NODES=()
EXCLUDE_NODES=()
REMOTE_CHECK_TIMEOUT=120
APT_UPDATE_TIMEOUT=1800
APT_UPGRADE_TIMEOUT=7200
APT_MAINT_TIMEOUT=1800
APT_ENV="DEBIAN_FRONTEND=noninteractive"
APT_DPKG_OPTS="-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
SSH_OPTS=(
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o ConnectionAttempts=1
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=4
)

# ---- ARGUMENT PARSING ----
while [ "$#" -gt 0 ]; do
  case "$1" in
    -j|--jobs)
      if [ -z "${2:-}" ] || [[ ! "$2" =~ ^[0-9]+$ ]] || [ "$2" -lt 1 ]; then
        echo -e "${RED}Error: --jobs requires a positive integer.${RESET}"
        exit 1
      fi
      MAX_PARALLEL="$2"
      shift 2
      ;;
    -p|--parallel)
      MAX_PARALLEL=0
      shift
      ;;
    -n|--dry-run)
      DRY_RUN=1
      shift
      ;;
    --nodes)
      if [ -z "${2:-}" ]; then
        echo -e "${RED}Error: --nodes requires a comma-separated node list.${RESET}"
        exit 1
      fi
      IFS=',' read -r -a ONLY_NODES <<< "$2"
      shift 2
      ;;
    --exclude)
      if [ -z "${2:-}" ]; then
        echo -e "${RED}Error: --exclude requires a comma-separated node list.${RESET}"
        exit 1
      fi
      IFS=',' read -r -a EXCLUDE_NODES <<< "$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo -e "${RED}Error: unknown option '$1'.${RESET}"
      usage
      exit 1
      ;;
  esac
done

if [ -z "${BASH_VERSINFO:-}" ] || [ "${BASH_VERSINFO[0]}" -lt 4 ]; then
  echo -e "${RED}Error: this script requires Bash 4 or newer.${RESET}"
  exit 1
fi

declare -A UPDATED_PACKAGES
declare -A UPDATED_COUNT
declare -A RUNNING_KERNEL
declare -A LATEST_KERNEL
declare -A PVE_VERSION
declare -A REBOOT_REASON
declare -A NODE_RESULT
declare -A NODE_ERRORS
declare -A NODE_PIDS
declare -A NODE_STARTED
declare -A NODE_DONE

# ---- CLEAR SCREEN AT START ----
clear

# ---- REQUIRE ROOT ----
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}Error: this script must be run as root.${RESET}"
  exit 1
fi

# ---- FUNCTIONS ----
list_contains() {
  local NEEDLE="$1"
  shift

  for ITEM in "$@"; do
    if [ "$ITEM" = "$NEEDLE" ]; then
      return 0
    fi
  done

  return 1
}

remote_run() {
  local NODE="$1"
  local TIMEOUT_SECONDS="$2"
  local COMMAND="$3"

  ssh "${SSH_OPTS[@]}" root@"$NODE" "timeout --preserve-status ${TIMEOUT_SECONDS}s bash -lc $(printf '%q' "$COMMAND")"
}

print_rule() {
  printf "%b\n" "${CYAN}================================================================${RESET}"
}

print_title() {
  local TITLE="$1"

  print_rule
  printf "%b\n" "${CYAN}${BOLD}$TITLE${RESET}"
  print_rule
}

print_meta() {
  local LABEL="$1"
  local VALUE="$2"

  printf "%b%-14s%b %s\n" "$BOLD" "$LABEL:" "$RESET" "$VALUE"
}

update_node() {
  local NODE="$1"
  local STATUS_FILE="$2"
  local SUMMARY_FILE="$3"
  local MODE_LABEL=""

  if [ "$DRY_RUN" -eq 1 ]; then
    MODE_LABEL=" (dry run)"
  fi

  # Helper to write a single-line status
  node_status() {
    echo -e "$1" > "$STATUS_FILE"
  }

  write_summary() {
    local COUNT="${1:-0}"
    local PKGS="${2:-}"
    local RKERNEL="${3:-}"
    local LKERNEL="${4:-}"
    local NEED_REBOOT="${5:-0}"
    local RESULT="${6:-OK}"
    local ERRORS="${7:-}"
    local PVEVERSION="${8:-}"
    local REBOOT_REASON_TEXT="${9:-}"

    {
      echo "COUNT|$COUNT"
      echo "PKGS|$PKGS"
      echo "RKERNEL|$RKERNEL"
      echo "LKERNEL|$LKERNEL"
      echo "REBOOT|$NEED_REBOOT"
      echo "RESULT|$RESULT"
      echo "ERRORS|$ERRORS"
      echo "PVEVERSION|$PVEVERSION"
      echo "REBOOT_REASON|$REBOOT_REASON_TEXT"
    } > "$SUMMARY_FILE"
  }

  node_status "${CYAN}Initializing...${RESET}"

  # Quick SSH check
  remote_run "$NODE" "$REMOTE_CHECK_TIMEOUT" 'true' &>/dev/null
  if [ $? -ne 0 ]; then
    node_status "${RED}SSH connection failed${RESET}"
    write_summary 0 "" "" "" 0 "FAILED" "SSH connection failed"
    return
  fi

  local ERRORS=()

  # ---- STEP 1: apt-get update ----
  node_status "${CYAN}Running apt-get update...${RESET}"
  remote_run "$NODE" "$APT_UPDATE_TIMEOUT" "$APT_ENV apt-get update" &>/dev/null
  if [ $? -ne 0 ]; then
    node_status "${RED}apt-get update failed${RESET}"
    write_summary 0 "" "" "" 0 "FAILED" "apt-get update failed"
    return
  else
    node_status "${GREEN}apt-get update complete${RESET}"
  fi

  # ---- Determine packages that will be upgraded (simulation) ----
  node_status "${CYAN}Calculating upgrades...${RESET}"
  UPGRADE_SIM=$(remote_run "$NODE" "$APT_MAINT_TIMEOUT" "$APT_ENV apt-get -s dist-upgrade" 2>/dev/null)
  if [ $? -ne 0 ]; then
    node_status "${RED}Upgrade simulation failed${RESET}"
    write_summary 0 "" "" "" 0 "FAILED" "upgrade simulation failed"
    return
  fi

  INST_PKGS=$(printf '%s\n' "$UPGRADE_SIM" | awk '/^Inst / {printf "%s ", $2}' | sed 's/ *$//')
  REMV_PKGS=$(printf '%s\n' "$UPGRADE_SIM" | awk '/^Remv / {printf "%s ", $2}' | sed 's/ *$//')

  PKGS=""
  if [ -n "$INST_PKGS" ]; then
    PKGS="Install/upgrade: $INST_PKGS"
  fi
  if [ -n "$REMV_PKGS" ]; then
    if [ -n "$PKGS" ]; then
      PKGS="$PKGS; Remove: $REMV_PKGS"
    else
      PKGS="Remove: $REMV_PKGS"
    fi
  fi

  local COUNT=0
  if [ -n "$INST_PKGS" ] || [ -n "$REMV_PKGS" ]; then
    COUNT=$(( $(wc -w <<< "$INST_PKGS") + $(wc -w <<< "$REMV_PKGS") ))
    if [ "$DRY_RUN" -eq 1 ]; then
      node_status "${CYAN}Found ${BOLD}$COUNT${RESET}${CYAN} package change(s)${MODE_LABEL}${RESET}"
    else
      node_status "${CYAN}Applying ${BOLD}$COUNT${RESET}${CYAN} package change(s)...${RESET}"
    fi
  else
    node_status "${GREEN}No package changes needed${RESET}"
  fi

  # ---- STEP 2: dist-upgrade ----
  if [ "$COUNT" -gt 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    remote_run "$NODE" "$APT_UPGRADE_TIMEOUT" "$APT_ENV apt-get $APT_DPKG_OPTS -y dist-upgrade" &>/dev/null
    if [ $? -ne 0 ]; then
      node_status "${RED}Upgrade failed${RESET}"
      write_summary "$COUNT" "$PKGS" "" "" 0 "FAILED" "dist-upgrade failed"
      return
    fi

    node_status "${GREEN}Upgrade complete${RESET}"
  fi

  # ---- STEP 3: autoremove ----
  if [ "$DRY_RUN" -eq 0 ]; then
    node_status "${CYAN}Removing unused packages...${RESET}"
    remote_run "$NODE" "$APT_MAINT_TIMEOUT" "$APT_ENV apt-get $APT_DPKG_OPTS -y autoremove" &>/dev/null
    if [ $? -ne 0 ]; then
      node_status "${RED}autoremove failed${RESET}"
      ERRORS+=("autoremove failed")
    else
      node_status "${GREEN}autoremove complete${RESET}"
    fi
  fi

  # ---- STEP 4: clean ----
  if [ "$DRY_RUN" -eq 0 ]; then
    node_status "${CYAN}Cleaning package cache...${RESET}"
    remote_run "$NODE" "$APT_MAINT_TIMEOUT" "$APT_ENV apt-get clean" &>/dev/null
    if [ $? -ne 0 ]; then
      node_status "${RED}apt-get clean failed${RESET}"
      ERRORS+=("apt-get clean failed")
    else
      node_status "${GREEN}apt-get clean complete${RESET}"
    fi
  fi

  # ---- CHECK KERNEL VERSIONS / REBOOT ----
  node_status "${CYAN}Checking kernel & reboot requirement...${RESET}"

  PVEVERSION=$(remote_run "$NODE" "$REMOTE_CHECK_TIMEOUT" 'pveversion' 2>/dev/null)
  RKERNEL=$(remote_run "$NODE" "$REMOTE_CHECK_TIMEOUT" 'uname -r' 2>/dev/null)
  LKERNEL=$(remote_run "$NODE" "$REMOTE_CHECK_TIMEOUT" \
    "ls -1 /boot/vmlinuz-* 2>/dev/null | sed 's|.*/vmlinuz-||' | sort -V | tail -n1" 2>/dev/null)

  local NEED_REBOOT=0
  local REBOOT_REASONS=()
  local REBOOT_REASON_TEXT=""

  if [[ -n "$RKERNEL" && -n "$LKERNEL" && "$RKERNEL" != "$LKERNEL" ]]; then
    NEED_REBOOT=1
    REBOOT_REASONS+=("kernel mismatch")
  fi

  remote_run "$NODE" "$REMOTE_CHECK_TIMEOUT" '[ -f /var/run/reboot-required ]' &>/dev/null
  if [ $? -eq 0 ]; then
    NEED_REBOOT=1
    REBOOT_REASONS+=("/var/run/reboot-required")
  fi

  if [ ${#REBOOT_REASONS[@]} -gt 0 ]; then
    printf -v REBOOT_REASON_TEXT "%s; " "${REBOOT_REASONS[@]}"
    REBOOT_REASON_TEXT=${REBOOT_REASON_TEXT%; }
  fi

  local RESULT="OK"
  local ERROR_TEXT=""

  if [ ${#ERRORS[@]} -gt 0 ]; then
    RESULT="FAILED"
    printf -v ERROR_TEXT "%s; " "${ERRORS[@]}"
    ERROR_TEXT=${ERROR_TEXT%; }
  fi

  if [ "$NEED_REBOOT" -eq 1 ] && [ "$RESULT" = "FAILED" ]; then
    node_status "${RED}Completed with errors${RESET}${YELLOW} (reboot required)${RESET}${CYAN}${MODE_LABEL}${RESET}"
  elif [ "$NEED_REBOOT" -eq 1 ]; then
    node_status "${YELLOW}Completed (reboot required)${RESET}${CYAN}${MODE_LABEL}${RESET}"
  elif [ "$RESULT" = "FAILED" ]; then
    node_status "${RED}Completed with errors${RESET}${CYAN}${MODE_LABEL}${RESET}"
  else
    node_status "${GREEN}Completed (no reboot needed)${RESET}${CYAN}${MODE_LABEL}${RESET}"
  fi

  # ---- WRITE SUMMARY FILE ----
  write_summary "$COUNT" "$PKGS" "$RKERNEL" "$LKERNEL" "$NEED_REBOOT" "$RESULT" "$ERROR_TEXT" "$PVEVERSION" "$REBOOT_REASON_TEXT"
}

# ---- DETECT CLUSTER NODES ----
echo -e "${CYAN}${BOLD}Detecting Proxmox cluster nodes...${RESET}"

if ! command -v pvecm >/dev/null 2>&1; then
  echo -e "${RED}Error: 'pvecm' command not found. Are you running this on a Proxmox node?${RESET}"
  exit 1
fi

# Expected pvecm nodes format (modern Proxmox):
# Membership information
# ----------------------
# Nodeid      Votes Name
# 1           1     NODE-1 (local)
# 2           1     NODE-2
# ...
mapfile -t NODES < <(
  pvecm nodes | awk '
    $1 ~ /^[0-9]+$/ && NF >= 3 {print $3}
  '
)

if [ ${#NODES[@]} -eq 0 ]; then
  echo -e "${RED}Error: No cluster nodes detected via '\''pvecm nodes'\''.${RESET}"
  exit 1
fi

DETECTED_NODES=("${NODES[@]}")

if [ ${#ONLY_NODES[@]} -gt 0 ]; then
  for NODE in "${ONLY_NODES[@]}"; do
    if ! list_contains "$NODE" "${DETECTED_NODES[@]}"; then
      echo -e "${RED}Error: requested node '$NODE' was not detected by 'pvecm nodes'.${RESET}"
      exit 1
    fi
  done
fi

if [ ${#EXCLUDE_NODES[@]} -gt 0 ]; then
  for NODE in "${EXCLUDE_NODES[@]}"; do
    if ! list_contains "$NODE" "${DETECTED_NODES[@]}"; then
      echo -e "${RED}Error: excluded node '$NODE' was not detected by 'pvecm nodes'.${RESET}"
      exit 1
    fi
  done
fi

FILTERED_NODES=()
for NODE in "${DETECTED_NODES[@]}"; do
  if [ ${#ONLY_NODES[@]} -gt 0 ] && ! list_contains "$NODE" "${ONLY_NODES[@]}"; then
    continue
  fi

  if [ ${#EXCLUDE_NODES[@]} -gt 0 ] && list_contains "$NODE" "${EXCLUDE_NODES[@]}"; then
    continue
  fi

  FILTERED_NODES+=("$NODE")
done

NODES=("${FILTERED_NODES[@]}")

if [ ${#NODES[@]} -eq 0 ]; then
  echo -e "${RED}Error: node filters matched no nodes.${RESET}"
  exit 1
fi

if [ "$MAX_PARALLEL" -eq 0 ]; then
  MAX_PARALLEL=${#NODES[@]}
fi

if [ "$MAX_PARALLEL" -gt "${#NODES[@]}" ]; then
  MAX_PARALLEL=${#NODES[@]}
fi

if [ "$DRY_RUN" -eq 1 ]; then
  RUN_MODE="Dry run"
else
  RUN_MODE="Apply updates"
fi

clear
print_title "Proxmox VE Cluster Update"
print_meta "Mode" "$RUN_MODE"
print_meta "Detected" "${DETECTED_NODES[*]}"
print_meta "Selected" "${NODES[*]}"
print_meta "Concurrency" "$MAX_PARALLEL node(s) at a time"
if [ "$DRY_RUN" -eq 1 ]; then
  print_meta "Note" "Package changes will be reported but not applied"
fi
sleep 1

# ---- TEMP DIR FOR STATUS & SUMMARY FILES ----
TMP_DIR=$(mktemp -d /tmp/proxmox-update.XXXXXX) || {
  echo -e "${RED}Failed to create temporary directory.${RESET}"
  exit 1
}
trap 'rm -rf "$TMP_DIR"' EXIT

# ---- START UPDATES ----
echo
print_title "Starting Node Jobs"

for NODE in "${NODES[@]}"; do
  STATUS_FILE="$TMP_DIR/$NODE.status"
  echo -e "${YELLOW}Queued...${RESET}" > "$STATUS_FILE"
  NODE_STARTED["$NODE"]=0
  NODE_DONE["$NODE"]=0
done

# ---- LIVE DASHBOARD LOOP ----
while :; do
  running_count=0
  completed_count=0

  for NODE in "${NODES[@]}"; do
    if [ "${NODE_STARTED[$NODE]}" = "1" ] && [ "${NODE_DONE[$NODE]}" = "0" ]; then
      pid="${NODE_PIDS["$NODE"]}"
      if kill -0 "$pid" 2>/dev/null; then
        running_count=$((running_count + 1))
      else
        wait "$pid" 2>/dev/null
        NODE_DONE["$NODE"]=1
      fi
    fi

    if [ "${NODE_DONE[$NODE]}" = "1" ]; then
      completed_count=$((completed_count + 1))
    fi
  done

  for NODE in "${NODES[@]}"; do
    if [ "$running_count" -ge "$MAX_PARALLEL" ]; then
      break
    fi

    if [ "${NODE_STARTED[$NODE]}" = "0" ]; then
      STATUS_FILE="$TMP_DIR/$NODE.status"
      SUMMARY_FILE="$TMP_DIR/$NODE.summary"
      update_node "$NODE" "$STATUS_FILE" "$SUMMARY_FILE" &
      NODE_PIDS["$NODE"]=$!
      NODE_STARTED["$NODE"]=1
      running_count=$((running_count + 1))
    fi
  done

  clear
  print_title "Proxmox VE Cluster Update"
  print_meta "Mode" "$RUN_MODE"
  print_meta "Concurrency" "$MAX_PARALLEL node(s) at a time"
  print_meta "Progress" "$completed_count/${#NODES[@]} complete, $running_count running"
  echo
  printf "%-24s  %-12s  %s\n" "Node" "State" "Status"
  printf "%-24s  %-12s  %s\n" "------------------------" "------------" "----------------------------------------"

  for NODE in "${NODES[@]}"; do
    STATE="Queued"
    if [ "${NODE_DONE[$NODE]}" = "1" ]; then
      STATE="Done"
    elif [ "${NODE_STARTED[$NODE]}" = "1" ]; then
      STATE="Running"
    fi

    STATUS_FILE="$TMP_DIR/$NODE.status"
    if [ -f "$STATUS_FILE" ]; then
      STATUS_LINE=$(tail -n1 "$STATUS_FILE")
    else
      STATUS_LINE="(no status yet)"
    fi

    printf "%-24s  %-12s  %b\n" "$NODE" "$STATE" "$STATUS_LINE"
  done

  echo
  print_rule
  [ "$completed_count" -eq "${#NODES[@]}" ] && break
  sleep 0.5
done

echo
echo -e "${GREEN}${BOLD}All node jobs have finished.${RESET}"
echo

# ---- COLLECT SUMMARY DATA FROM FILES ----
for NODE in "${NODES[@]}"; do
  SUMMARY_FILE="$TMP_DIR/$NODE.summary"
  COUNT=0
  PKGS=""
  RK=""
  LK=""
  PV=""
  REBOOT=0
  REBOOT_REASON_TEXT=""
  RESULT="FAILED"
  ERRORS="No summary file was written"

  if [ -f "$SUMMARY_FILE" ]; then
    RESULT="OK"
    ERRORS=""
    while IFS="|" read -r key value; do
      case "$key" in
        COUNT)   COUNT="$value" ;;
        PKGS)    PKGS="$value" ;;
        RKERNEL) RK="$value" ;;
        LKERNEL) LK="$value" ;;
        REBOOT)  REBOOT="$value" ;;
        RESULT)  RESULT="$value" ;;
        ERRORS)  ERRORS="$value" ;;
        PVEVERSION) PV="$value" ;;
        REBOOT_REASON) REBOOT_REASON_TEXT="$value" ;;
      esac
    done < "$SUMMARY_FILE"
  fi

  UPDATED_COUNT["$NODE"]="$COUNT"
  UPDATED_PACKAGES["$NODE"]="$PKGS"
  RUNNING_KERNEL["$NODE"]="$RK"
  LATEST_KERNEL["$NODE"]="$LK"
  PVE_VERSION["$NODE"]="$PV"
  REBOOT_REASON["$NODE"]="$REBOOT_REASON_TEXT"
  NODE_RESULT["$NODE"]="$RESULT"
  NODE_ERRORS["$NODE"]="$ERRORS"

  if [ "$REBOOT" = "1" ]; then
    REBOOT_NEEDED+=("$NODE")
  fi

  if [ "$RESULT" != "OK" ]; then
    FAILED_NODES+=("$NODE")
  fi
done

# ---- SUMMARY ----
clear
print_title "Update Summary"
print_meta "Mode" "$RUN_MODE"
print_meta "Nodes" "${#NODES[@]} processed"
print_meta "Failures" "${#FAILED_NODES[@]}"
print_meta "Reboots" "${#REBOOT_NEEDED[@]}"
echo

if [ ${#REBOOT_NEEDED[@]} -eq 0 ]; then
  echo -e "${GREEN}${BOLD}Reboots:${RESET} none required"
else
  echo -e "${YELLOW}${BOLD}Reboots required:${RESET}"
  for NODE in "${REBOOT_NEEDED[@]}"; do
    REBOOT_REASON_TEXT=${REBOOT_REASON[$NODE]:-Unknown reason}
    echo -e "  - ${BOLD}${NODE}${RESET}: $REBOOT_REASON_TEXT"
  done
fi

echo
if [ ${#FAILED_NODES[@]} -eq 0 ]; then
  echo -e "${GREEN}${BOLD}Failures:${RESET} none reported"
else
  echo -e "${RED}${BOLD}Failures reported:${RESET}"
  for NODE in "${FAILED_NODES[@]}"; do
    ERROR_TEXT=${NODE_ERRORS[$NODE]:-Unknown failure}
    echo -e "  - ${BOLD}${NODE}${RESET}: $ERROR_TEXT"
  done
fi

echo
print_rule
echo -e "${BOLD}Per-node details${RESET}"
print_rule
for NODE in "${NODES[@]}"; do
  COUNT=${UPDATED_COUNT[$NODE]:-0}
  PKGLIST=${UPDATED_PACKAGES[$NODE]}
  RK=${RUNNING_KERNEL[$NODE]}
  LK=${LATEST_KERNEL[$NODE]}
  PV=${PVE_VERSION[$NODE]}
  REBOOT_REASON_TEXT=${REBOOT_REASON[$NODE]}
  RESULT=${NODE_RESULT[$NODE]:-FAILED}
  ERROR_TEXT=${NODE_ERRORS[$NODE]:-Unknown failure}

  echo
  echo -e "${CYAN}${BOLD}$NODE${RESET}"
  if [ "$RESULT" = "OK" ]; then
    print_meta "  Result" "${GREEN}OK${RESET}"
  else
    print_meta "  Result" "${RED}FAILED${RESET}"
    print_meta "  Errors" "$ERROR_TEXT"
  fi
  print_meta "  Changes" "${BOLD}$COUNT${RESET}"
  if [ "$COUNT" -gt 0 ] && [ -n "$PKGLIST" ]; then
    print_meta "  Packages" ""
    echo "    $PKGLIST" | fmt -w 70 | sed 's/^/    /'
  else
    print_meta "  Packages" "None"
  fi

  if [[ -n "$PV" ]]; then
    print_meta "  PVE" "${BOLD}$PV${RESET}"
  else
    print_meta "  PVE" "${RED}Could not be determined${RESET}"
  fi

  if [[ -n "$RK" && -n "$LK" ]]; then
    print_meta "  Kernel" "running ${BOLD}$RK${RESET}, latest ${BOLD}$LK${RESET}"
  elif [[ -n "$RK" ]]; then
    print_meta "  Kernel" "running ${BOLD}$RK${RESET}, latest ${RED}unknown${RESET}"
  else
    print_meta "  Kernel" "${RED}Could not be determined${RESET}"
  fi

  if [[ -n "$REBOOT_REASON_TEXT" ]]; then
    print_meta "  Reboot" "${YELLOW}$REBOOT_REASON_TEXT${RESET}"
  else
    print_meta "  Reboot" "None detected"
  fi
done

echo
print_rule
echo -e "${BOLD}All nodes processed.${RESET}"
print_rule
