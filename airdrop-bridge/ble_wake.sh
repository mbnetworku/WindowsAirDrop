#!/bin/bash
# Broadcast an Apple "AirDrop" BLE advertisement for a few seconds so nearby iPhones/Macs
# switch on AWDL and their AirDrop server. Experimental: format from published research
# (Apple company ID 0x004C, type 0x05). Zero contact hashes = "unknown sender", which only
# works when the Apple device is set to "Everyone for 10 minutes".
HCI=${HCI:-hci0}
hciconfig "$HCI" up || exit 1
# advertising data: flags + manufacturer data (4C 00 | 05 12 | 8x00 | 01 | 8x00 hashes | 00)
hcitool -i "$HCI" cmd 0x08 0x0008 1b 02 01 1a 17 ff 4c 00 05 12 00 00 00 00 00 00 00 00 01 00 00 00 00 00 00 00 00 00 00 00 00 00 >/dev/null
hcitool -i "$HCI" cmd 0x08 0x0006 a0 00 a0 00 03 00 00 00 00 00 00 00 00 07 00 >/dev/null
hcitool -i "$HCI" cmd 0x08 0x000a 01 >/dev/null
sleep "${SECONDS_ON:-8}"
hcitool -i "$HCI" cmd 0x08 0x000a 00 >/dev/null
