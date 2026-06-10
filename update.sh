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
  -h, --help        Show this help message
EOF
}

# ---- ARRAYS / VARIABLES ----
REBOOT_NEEDED=()
FAILED_NODES=()
MAX_PARALLEL=1
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
update_node() {
  local NODE="$1"
  local STATUS_FILE="$2"
  local SUMMARY_FILE="$3"

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

    {
      echo "COUNT|$COUNT"
      echo "PKGS|$PKGS"
      echo "RKERNEL|$RKERNEL"
      echo "LKERNEL|$LKERNEL"
      echo "REBOOT|$NEED_REBOOT"
      echo "RESULT|$RESULT"
      echo "ERRORS|$ERRORS"
    } > "$SUMMARY_FILE"
  }

  node_status "${CYAN}Initializing...${RESET}"

  # Quick SSH check
  ssh "${SSH_OPTS[@]}" root@"$NODE" 'true' &>/dev/null
  if [ $? -ne 0 ]; then
    node_status "${RED}SSH connection failed${RESET}"
    write_summary 0 "" "" "" 0 "FAILED" "SSH connection failed"
    return
  fi

  local ERRORS=()

  # ---- STEP 1: apt update ----
  node_status "${CYAN}Running apt update...${RESET}"
  ssh "${SSH_OPTS[@]}" root@"$NODE" 'apt update' &>/dev/null
  if [ $? -ne 0 ]; then
    node_status "${RED}apt update failed${RESET}"
    write_summary 0 "" "" "" 0 "FAILED" "apt update failed"
    return
  else
    node_status "${GREEN}apt update complete${RESET}"
  fi

  # ---- Determine packages that will be upgraded (simulation) ----
  node_status "${CYAN}Calculating upgrades...${RESET}"
  UPGRADE_SIM=$(ssh "${SSH_OPTS[@]}" root@"$NODE" 'apt-get -s full-upgrade' 2>/dev/null)
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
    node_status "${CYAN}Applying ${BOLD}$COUNT${RESET}${CYAN} package change(s)...${RESET}"
  else
    node_status "${GREEN}No package changes needed${RESET}"
  fi

  # ---- STEP 2: full-upgrade ----
  if [ "$COUNT" -gt 0 ]; then
    ssh "${SSH_OPTS[@]}" root@"$NODE" 'apt -y full-upgrade' &>/dev/null
    if [ $? -ne 0 ]; then
      node_status "${RED}Upgrade failed${RESET}"
      write_summary "$COUNT" "$PKGS" "" "" 0 "FAILED" "full-upgrade failed"
      return
    fi

    node_status "${GREEN}Upgrade complete${RESET}"
  fi

  # ---- STEP 3: autoremove ----
  node_status "${CYAN}Removing unused packages...${RESET}"
  ssh "${SSH_OPTS[@]}" root@"$NODE" 'apt -y autoremove' &>/dev/null
  if [ $? -ne 0 ]; then
    node_status "${RED}autoremove failed${RESET}"
    ERRORS+=("autoremove failed")
  else
    node_status "${GREEN}autoremove complete${RESET}"
  fi

  # ---- STEP 4: clean ----
  node_status "${CYAN}Cleaning package cache...${RESET}"
  ssh "${SSH_OPTS[@]}" root@"$NODE" 'apt clean' &>/dev/null
  if [ $? -ne 0 ]; then
    node_status "${RED}apt clean failed${RESET}"
    ERRORS+=("apt clean failed")
  else
    node_status "${GREEN}apt clean complete${RESET}"
  fi

  # ---- CHECK KERNEL VERSIONS / REBOOT ----
  node_status "${CYAN}Checking kernel & reboot requirement...${RESET}"

  RKERNEL=$(ssh "${SSH_OPTS[@]}" root@"$NODE" 'uname -r' 2>/dev/null)
  LKERNEL=$(ssh "${SSH_OPTS[@]}" root@"$NODE" \
    "ls -1 /boot/vmlinuz-* 2>/dev/null | sed 's|.*/vmlinuz-||' | sort -V | tail -n1" 2>/dev/null)

  local NEED_REBOOT=0

  if [[ -n "$RKERNEL" && -n "$LKERNEL" && "$RKERNEL" != "$LKERNEL" ]]; then
    NEED_REBOOT=1
  fi

  ssh "${SSH_OPTS[@]}" root@"$NODE" '[ -f /var/run/reboot-required ]' &>/dev/null
  if [ $? -eq 0 ]; then
    NEED_REBOOT=1
  fi

  local RESULT="OK"
  local ERROR_TEXT=""

  if [ ${#ERRORS[@]} -gt 0 ]; then
    RESULT="FAILED"
    printf -v ERROR_TEXT "%s; " "${ERRORS[@]}"
    ERROR_TEXT=${ERROR_TEXT%; }
  fi

  if [ "$NEED_REBOOT" -eq 1 ] && [ "$RESULT" = "FAILED" ]; then
    node_status "${RED}Completed with errors${RESET}${YELLOW} (reboot required)${RESET}"
  elif [ "$NEED_REBOOT" -eq 1 ]; then
    node_status "${YELLOW}Completed (reboot required)${RESET}"
  elif [ "$RESULT" = "FAILED" ]; then
    node_status "${RED}Completed with errors${RESET}"
  else
    node_status "${GREEN}Completed (no reboot needed)${RESET}"
  fi

  # ---- WRITE SUMMARY FILE ----
  write_summary "$COUNT" "$PKGS" "$RKERNEL" "$LKERNEL" "$NEED_REBOOT" "$RESULT" "$ERROR_TEXT"
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

echo -e "${GREEN}Detected nodes:${RESET} ${NODES[*]}"
if [ "$MAX_PARALLEL" -eq 0 ]; then
  MAX_PARALLEL=${#NODES[@]}
fi

if [ "$MAX_PARALLEL" -gt "${#NODES[@]}" ]; then
  MAX_PARALLEL=${#NODES[@]}
fi

echo -e "${GREEN}Update concurrency:${RESET} $MAX_PARALLEL node(s) at a time"
sleep 1

# ---- TEMP DIR FOR STATUS & SUMMARY FILES ----
TMP_DIR=$(mktemp -d /tmp/proxmox-update.XXXXXX) || {
  echo -e "${RED}Failed to create temporary directory.${RESET}"
  exit 1
}
trap 'rm -rf "$TMP_DIR"' EXIT

# ---- START UPDATES ----
echo
echo -e "${CYAN}${BOLD}=============================================${RESET}"
echo -e "${CYAN}${BOLD}   Proxmox VE Cluster Update Script${RESET}"
echo -e "${CYAN}${BOLD}=============================================${RESET}"

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
  echo -e "${CYAN}${BOLD}Proxmox VE Cluster Update - Live Status${RESET}"
  echo -e "${CYAN}---------------------------------------------${RESET}"
  echo -e "${CYAN}Concurrency:${RESET} $MAX_PARALLEL node(s) at a time"
  printf "%-18s | %s\n" "Node" "Status"
  echo "--------------------+------------------------------------------"

  for NODE in "${NODES[@]}"; do
    STATUS_FILE="$TMP_DIR/$NODE.status"
    if [ -f "$STATUS_FILE" ]; then
      STATUS_LINE=$(tail -n1 "$STATUS_FILE")
    else
      STATUS_LINE="(no status yet)"
    fi

    printf "%-18s | %s\n" "$NODE" "$STATUS_LINE"
  done

  echo -e "${CYAN}---------------------------------------------${RESET}"
  [ "$completed_count" -eq "${#NODES[@]}" ] && break
  sleep 0.5
done

echo
echo -e "${GREEN}${BOLD}All node update jobs have finished.${RESET}"
echo

# ---- COLLECT SUMMARY DATA FROM FILES ----
for NODE in "${NODES[@]}"; do
  SUMMARY_FILE="$TMP_DIR/$NODE.summary"
  COUNT=0
  PKGS=""
  RK=""
  LK=""
  REBOOT=0
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
      esac
    done < "$SUMMARY_FILE"
  fi

  UPDATED_COUNT["$NODE"]="$COUNT"
  UPDATED_PACKAGES["$NODE"]="$PKGS"
  RUNNING_KERNEL["$NODE"]="$RK"
  LATEST_KERNEL["$NODE"]="$LK"
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
echo -e "${BOLD}${CYAN}=============================================${RESET}"
echo -e "${BOLD}${CYAN}              UPDATE SUMMARY${RESET}"
echo -e "${BOLD}${CYAN}=============================================${RESET}"

if [ ${#REBOOT_NEEDED[@]} -eq 0 ]; then
  echo -e "${GREEN}No nodes require a reboot.${RESET}"
else
  echo -e "${YELLOW}The following nodes require a reboot:${RESET}"
  for NODE in "${REBOOT_NEEDED[@]}"; do
    echo -e "  - ${BOLD}${NODE}${RESET}"
  done
fi

echo
if [ ${#FAILED_NODES[@]} -eq 0 ]; then
  echo -e "${GREEN}No node update failures were reported.${RESET}"
else
  echo -e "${RED}The following nodes reported update failures:${RESET}"
  for NODE in "${FAILED_NODES[@]}"; do
    ERROR_TEXT=${NODE_ERRORS[$NODE]:-Unknown failure}
    echo -e "  - ${BOLD}${NODE}${RESET}: $ERROR_TEXT"
  done
fi

echo
echo -e "${BOLD}${CYAN}Per-node package change summary:${RESET}"
for NODE in "${NODES[@]}"; do
  COUNT=${UPDATED_COUNT[$NODE]:-0}
  PKGLIST=${UPDATED_PACKAGES[$NODE]}
  RK=${RUNNING_KERNEL[$NODE]}
  LK=${LATEST_KERNEL[$NODE]}
  RESULT=${NODE_RESULT[$NODE]:-FAILED}
  ERROR_TEXT=${NODE_ERRORS[$NODE]:-Unknown failure}

  echo
  echo -e "${YELLOW}${BOLD}$NODE${RESET}"
  if [ "$RESULT" = "OK" ]; then
    echo -e "  Result: ${GREEN}OK${RESET}"
  else
    echo -e "  Result: ${RED}FAILED${RESET}"
    echo "  Errors: $ERROR_TEXT"
  fi
  echo -e "  Package changes: ${BOLD}$COUNT${RESET}"
  if [ "$COUNT" -gt 0 ] && [ -n "$PKGLIST" ]; then
    echo -e "  Package changes:"
    echo "    $PKGLIST" | fmt -w 70 | sed 's/^/    /'
  else
    echo "  Package changes: None"
  fi

  if [[ -n "$RK" && -n "$LK" ]]; then
    echo -e "  Running kernel: ${BOLD}$RK${RESET}"
    echo -e "  Latest installed kernel: ${BOLD}$LK${RESET}"
  elif [[ -n "$RK" ]]; then
    echo -e "  Running kernel: ${BOLD}$RK${RESET}"
    echo -e "  Latest installed kernel: ${RED}Unknown (no /boot/vmlinuz-* ?)${RESET}"
  else
    echo -e "  Kernel information: ${RED}Could not be determined${RESET}"
  fi
done

echo
echo -e "${BOLD}${CYAN}=============================================${RESET}"
echo -e "${BOLD}All nodes processed.${RESET}"
echo -e "${BOLD}${CYAN}=============================================${RESET}"
