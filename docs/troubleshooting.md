# Troubleshooting ShareSpace

## The other computer is not listed

- Confirm both computers are connected to the same Wi-Fi or Ethernet network.
- Check that the network does not isolate wireless clients and that the firewall
  allows ShareSpace's local session traffic.
- Try the host's displayed IPv4 address and port in **Join Session**. Local discovery
  uses multicast and may be blocked even when direct connections work.
- A router, VLAN, VPN, or guest network can prevent local device-to-device access.
  Internet access does not fix those local network restrictions.

## The fingerprint does not match or TLS verification fails

Stop before entering the joining code. Ask the host for the current fingerprint using
a trusted channel and compare every character. A host restart creates a new session
certificate and fingerprint. Never accept a mismatch based only on the value shown by
local discovery.

## The host did not receive my join request

Check the address, port, joining code, and host availability. Ask the host to approve
the pending request. The code is a credential and should not be posted in logs,
screenshots, or public messages.

## An upload is rejected

The participant needs **READ_WRITE** permission and the host's global upload switch
must be enabled. Uploads create new files only, cannot replace an existing name, and
are limited to 20 MB. If the host revokes permission during an upload, the transfer is
cancelled and the incomplete file is discarded.

## Local settings or history are unavailable

Check that the current user can write to the operating-system application-data
directory and has available disk space. ShareSpace keeps a rotating diagnostic log
under that directory's `logs` subfolder. The log redacts common credential formats;
review it before sharing it with anyone.

## Linux package will not start

The supplied Linux package targets x86_64 and requires glibc 2.34 or newer. It was
built for Ubuntu 22.04; other distributions may require additional system libraries.
Start it from a terminal to see loader errors. If the desktop launch does not work,
try `~/.local/bin/sharespace` after installation.

## Linux hotspot setup fails

Hotspot creation requires NetworkManager, its `nmcli` command, a supported Wi-Fi
adapter and driver, and permission to create a wireless access point. ShareSpace can
still host on an existing Wi-Fi or Ethernet network if hotspot setup is unavailable.

## Windows hotspot setup is unavailable

The Windows classic desktop package does not have the Windows package identity and
Wi-Fi control capability used by the tethering API. Use an existing Wi-Fi or Ethernet
network to host a session.

## Contacting a maintainer

When reporting an issue, include the ShareSpace version, Windows/Linux distribution,
steps to reproduce, and relevant redacted log excerpt. Do not attach session joining
codes, bearer tokens, private keys, or personal files.
