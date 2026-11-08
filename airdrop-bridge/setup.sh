#!/bin/bash
# Installs the AirDrop bridge inside a Debian/Ubuntu Linux VM.
# Usage:  sudo ./setup.sh <usb-wifi-interface> [channel 6|44|149]
# Find the interface name with:  ip link   (USB adapters look like wlx001122334455 or wlan0)
set -euo pipefail
IFACE=${1:?usage: sudo ./setup.sh <usb-wifi-interface> [channel]}
CHAN=${2:-6}
DIR=/opt/airdrop-bridge
SRC=$(cd "$(dirname "$0")" && pwd)

apt-get update
apt-get install -y git build-essential cmake pkg-config python3 python3-venv \
  libpcap-dev libev-dev libnl-3-dev libnl-genl-3-dev libnl-route-3-dev libarchive13 \
  bluez iw

# OWL - open-source AWDL (Apple Wireless Direct Link)
if ! command -v owl >/dev/null; then
  rm -rf /tmp/owl && git clone --recursive https://github.com/seemoo-lab/owl.git /tmp/owl
  (cd /tmp/owl && mkdir -p build && cd build && cmake .. && make -j"$(nproc)" && make install)
fi

# OpenDrop - open-source AirDrop
mkdir -p $DIR /var/lib/airdrop-bridge/received
python3 -m venv $DIR/venv
$DIR/venv/bin/pip install --upgrade pip opendrop
install -m 755 "$SRC/bridge.py" "$SRC/ble_wake.sh" $DIR/

# The Wi-Fi adapter is used exclusively for AWDL - stop NetworkManager from touching it
command -v nmcli >/dev/null && nmcli device set "$IFACE" managed no || true

TOKEN_FILE=$DIR/token
[ -f $TOKEN_FILE ] || head -c 18 /dev/urandom | base64 | tr -d '/+=' > $TOKEN_FILE
chmod 600 $TOKEN_FILE

cat > /etc/systemd/system/owl.service <<UNIT
[Unit]
Description=OWL - Apple Wireless Direct Link on $IFACE
After=network.target
[Service]
ExecStart=/usr/local/bin/owl -i $IFACE -c $CHAN
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/airdrop-bridge.service <<UNIT
[Unit]
Description=AirDrop bridge for WindowsAirDrop
After=owl.service bluetooth.service
Wants=owl.service
[Service]
Environment=BRIDGE_TOKEN=$(cat $TOKEN_FILE)
Environment=BLE_CMD=$DIR/ble_wake.sh
Environment=PATH=$DIR/venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
ExecStart=$DIR/venv/bin/python $DIR/bridge.py --opendrop $DIR/venv/bin/opendrop
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now owl.service airdrop-bridge.service
sleep 5
echo
echo "=================================================================="
echo " AirDrop bridge installed."
echo " Bridge address : http://$(hostname -I | awk '{print $1}'):8765"
echo " Bridge token   : $(cat $TOKEN_FILE)"
echo " Enter both in the WindowsAirDrop app -> Apple AirDrop settings."
echo
ip link show awdl0 >/dev/null 2>&1 && echo " awdl0 is up - OWL is running." \
  || echo " WARNING: awdl0 not found. Check: journalctl -u owl -e  (adapter may lack monitor mode/injection)"
echo "=================================================================="
