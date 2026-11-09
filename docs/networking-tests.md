# Version 0.2 networking tests

## Automated tests

Activate the virtual environment and run:

    python -m pip install -e ".[dev]"
    python -m pytest -q

The automated suite mocks nmcli, PowerShell, and the Windows tethering backend. It
covers platform selection, adapter discovery, AP support results, status, local IP
parsing, failed starts, permission/driver errors, cleanup, and preservation of an
unrelated active Linux connection. These tests do not control a real radio or prove
that another device can connect.

## Linux hardware test (Ubuntu/Fedora with NetworkManager)

1. Confirm NetworkManager is running and nmcli -f WIFI-PROPERTIES.AP device show
   the-interface-name reports yes for the wireless radio.
2. If possible, leave the radio disconnected or select a second wireless adapter.
   ShareSpace will ask before using a radio that is connected to another network.
3. Run ShareSpace, choose Host Session, enter an SSID and password, select the
   radio, and run Check Compatibility.
4. Create the hotspot. Confirm the status changes to Hotspot active and the host
   local IP is shown. On a second phone or computer, connect to the SSID and enter
   the configured password.
5. With no Internet uplink available, verify the client remains connected and can
   reach the host's displayed local IP. A temporary TCP listener can test local
   traffic independently of file transfer support:

       python -c "import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('0.0.0.0', 54832)); s.listen(1); c,a=s.accept(); print(a, c.recv(128)); c.sendall(b'local-ok'); c.close(); s.close()"

   From the client, connect to the host IP on TCP port 54832 and send a short
   message; verify the host prints it and the client receives local-ok. Replace
   HOST_IP with the address shown by ShareSpace:

       python -c "import socket; s=socket.create_connection(('HOST_IP', 54832)); s.sendall(b'client-message'); print(s.recv(128)); s.close()"

   Allow that local test port through the host firewall if needed.
6. Stop the hotspot in ShareSpace. Confirm the temporary profile is removed, the
   hotspot disappears from the client, and the previous Wi-Fi profile returns if it
   was active before the test and no other profile took its place.
7. Repeat with the host's Internet/Wi-Fi uplink disabled to verify the hotspot and
   local client-to-host traffic work offline.

NetworkManager reports AP capability from the current radio/driver, but hardware
firmware and distribution configuration can still reject activation. If
ipv4.method shared cannot start because required NetworkManager components are
missing, ShareSpace reports the failure and removes its temporary profile.

## Windows hardware test

The current Python development install is unpackaged, so the Windows adapter is
expected to report that the required wiFiControl package capability is unavailable.
No Windows hotspot has been started or verified on hardware in this milestone.

After a future Windows packaging step provides package identity and declares
wiFiControl, test on Windows 10 and Windows 11 separately:

1. Check that ShareSpace detects the physical Wi-Fi adapter and reports the exact
   Windows tethering capability result.
2. Configure the SSID/password, create the hotspot, and confirm that Windows reports
   tethering active and ShareSpace displays the virtual adapter IP.
3. Connect a second device and test local host/client communication using the TCP
   listener steps above.
4. Disable the host's Internet uplink and repeat. A missing Windows connection
   profile may prevent this API from starting; that is a real platform limitation
   and must remain an explicit unsupported result.
5. Stop the hotspot and confirm it is off. On pre-24H2 Windows, also confirm the
   original persistent mobile-hotspot SSID/password have been restored.

Windows 11 24H2 supports per-session configuration. Windows 10 and earlier Windows
11 versions require temporary changes to the persisted mobile-hotspot configuration
and restoration on orderly stop. Test those OS paths independently.

## Verification record for this milestone

- Automated mocked tests: see the milestone report for the current count.
- PyWinRT projections: import/API-shape smoke checked on Windows, but calls requiring
  package identity and a real radio were not executed.
- Linux nmcli calls: mocked only; no Linux host was available.
- Client association and local client/host communication: not hardware-verified.
