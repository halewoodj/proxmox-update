#!/bin/bash
# =============================================
# Proxmox VE Cluster Update Script
# =============================================
# - Auto-detects cluster nodes via pvecm
# - Runs updates on all nodes in parallel
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

# ---- ARRAYS / VARIABLES ----
REBOOT_NEEDED=()
SSH_OPTS=(
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o ConnectionAttempts=1
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=4
)

declare -A UPDATED_PACKAGES
declare -A UPDATED_COUNT
declare -A RUNNING_KERNEL
declare -A LATEST_KERNEL
declare -A NODE_PIDS

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

  node_status "${CYAN}Initializing...${RESET}"

  # Quick SSH check
  ssh "${SSH_OPTS[@]}" root@"$NODE" 'true' &>/dev/null
  if [ $? -ne 0 ]; then
    node_status "${RED}SSH connection failed${RESET}"
    {
      echo "COUNT|0"
      echo "PKGS|"
      echo "RKERNEL|"
      echo "LKERNEL|"
      echo "REBOOT|0"
    } > "$SUMMARY_FILE"
    return
  fi

  # ---- STEP 1: apt update ----
  node_status "${CYAN}Running apt update...${RESET}"
  ssh "${SSH_OPTS[@]}" root@"$NODE" 'apt update' &>/dev/null
  if [ $? -ne 0 ]; then
    node_status "${RED}apt update failed${RESET}"
    # Continue anyway, but note that upgrades may not be accurate
  else
    node_status "${GREEN}apt update complete${RESET}"
  fi

  # ---- Determine packages that will be upgraded (simulation) ----
  node_status "${CYAN}Calculating upgrades...${RESET}"
  UPGRADE_SIM=$(ssh "${SSH_OPTS[@]}" root@"$NODE" 'apt-get -s full-upgrade' 2>/dev/null)

  PKGS=$(printf '%s\n' "$UPGRADE_SIM" | awk '
    /The following packages will be upgraded:/ {flag=1; next}
    flag && NF==0 {flag=0}
    flag && NF>0 {
      gsub(/^ +| +$/, "")
      printf "%s ", $0
    }
  ' | sed 's/ *$//')

  local COUNT=0
  if [ -n "$PKGS" ]; then
    COUNT=$(wc -w <<< "$PKGS" | awk '{print $1}')
    node_status "${CYAN}Upgrading ${BOLD}$COUNT${RESET}${CYAN} package(s)...${RESET}"
  else
    node_status "${GREEN}No packages need upgrading${RESET}"
  fi

  # ---- STEP 2: full-upgrade ----
  if [ "$COUNT" -gt 0 ]; then
    ssh "${SSH_OPTS[@]}" root@"$NODE" 'apt -y full-upgrade' &>/dev/null
    if [ $? -ne 0 ]; then
      node_status "${RED}Upgrade failed${RESET}"
    else
      node_status "${GREEN}Upgrade complete${RESET}"
    fi
  fi

  # ---- STEP 3: autoremove ----
  node_status "${CYAN}Removing unused packages...${RESET}"
  ssh "${SSH_OPTS[@]}" root@"$NODE" 'apt -y autoremove' &>/dev/null
  if [ $? -ne 0 ]; then
    node_status "${RED}autoremove failed${RESET}"
  else
    node_status "${GREEN}autoremove complete${RESET}"
  fi

  # ---- STEP 4: clean ----
  node_status "${CYAN}Cleaning package cache...${RESET}"
  ssh "${SSH_OPTS[@]}" root@"$NODE" 'apt clean' &>/dev/null
  if [ $? -ne 0 ]; then
    node_status "${RED}apt clean failed${RESET}"
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

  if [ "$NEED_REBOOT" -eq 1 ]; then
    node_status "${YELLOW}Completed (reboot required)${RESET}"
  else
    node_status "${GREEN}Completed (no reboot needed)${RESET}"
  fi

  # ---- WRITE SUMMARY FILE ----
  {
    echo "COUNT|$COUNT"
    echo "PKGS|$PKGS"
    echo "RKERNEL|$RKERNEL"
    echo "LKERNEL|$LKERNEL"
    echo "REBOOT|$NEED_REBOOT"
  } > "$SUMMARY_FILE"
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
sleep 1

# ---- TEMP DIR FOR STATUS & SUMMARY FILES ----
TMP_DIR=$(mktemp -d /tmp/proxmox-update.XXXXXX) || {
  echo -e "${RED}Failed to create temporary directory.${RESET}"
  exit 1
}
trap 'rm -rf "$TMP_DIR"' EXIT

# ---- START PARALLEL UPDATES ----
echo
echo -e "${CYAN}${BOLD}=============================================${RESET}"
echo -e "${CYAN}${BOLD}   Proxmox VE Cluster Update Script${RESET}"
echo -e "${CYAN}${BOLD}=============================================${RESET}"

# Launch one background job per node
for NODE in "${NODES[@]}"; do
  STATUS_FILE="$TMP_DIR/$NODE.status"
  SUMMARY_FILE="$TMP_DIR/$NODE.summary"
  echo -e "${YELLOW}Queued...${RESET}" > "$STATUS_FILE"
  update_node "$NODE" "$STATUS_FILE" "$SUMMARY_FILE" &
  NODE_PIDS["$NODE"]=$!
done

# ---- LIVE DASHBOARD LOOP ----
while :; do
  clear
  echo -e "${CYAN}${BOLD}Proxmox VE Cluster Update - Live Status${RESET}"
  echo -e "${CYAN}---------------------------------------------${RESET}"
  printf "%-18s | %s\n" "Node" "Status"
  echo "--------------------+------------------------------------------"

  all_done=true

  for NODE in "${NODES[@]}"; do
    pid="${NODE_PIDS["$NODE"]}"
    if kill -0 "$pid" 2>/dev/null; then
      all_done=false
    fi

    STATUS_FILE="$TMP_DIR/$NODE.status"
    if [ -f "$STATUS_FILE" ]; then
      STATUS_LINE=$(tail -n1 "$STATUS_FILE")
    else
      STATUS_LINE="(no status yet)"
    fi

    printf "%-18s | %s\n" "$NODE" "$STATUS_LINE"
  done

  echo -e "${CYAN}---------------------------------------------${RESET}"
  $all_done && break
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

  if [ -f "$SUMMARY_FILE" ]; then
    while IFS="|" read -r key value; do
      case "$key" in
        COUNT)   COUNT="$value" ;;
        PKGS)    PKGS="$value" ;;
        RKERNEL) RK="$value" ;;
        LKERNEL) LK="$value" ;;
        REBOOT)  REBOOT="$value" ;;
      esac
    done < "$SUMMARY_FILE"
  fi

  UPDATED_COUNT["$NODE"]="$COUNT"
  UPDATED_PACKAGES["$NODE"]="$PKGS"
  RUNNING_KERNEL["$NODE"]="$RK"
  LATEST_KERNEL["$NODE"]="$LK"

  if [ "$REBOOT" = "1" ]; then
    REBOOT_NEEDED+=("$NODE")
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
echo -e "${BOLD}${CYAN}Per-node package update summary:${RESET}"
for NODE in "${NODES[@]}"; do
  COUNT=${UPDATED_COUNT[$NODE]:-0}
  PKGLIST=${UPDATED_PACKAGES[$NODE]}
  RK=${RUNNING_KERNEL[$NODE]}
  LK=${LATEST_KERNEL[$NODE]}

  echo
  echo -e "${YELLOW}${BOLD}$NODE${RESET}"
  echo -e "  Packages updated: ${BOLD}$COUNT${RESET}"
  if [ "$COUNT" -gt 0 ] && [ -n "$PKGLIST" ]; then
    echo -e "  Package list:"
    echo "    $PKGLIST" | fmt -w 70 | sed 's/^/    /'
  else
    echo "  Package list: None"
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
