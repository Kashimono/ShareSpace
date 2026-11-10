# Version 0.6.0 networking, monitoring, and security tests

## Run automated tests

Install the project with development dependencies and run:

    python -m pip install -e ".[dev]"
    python -m pytest -q

The project dependency set installs `watchdog`; monitoring and runtime sessions need
no internet service. Current verification in this Windows development workspace:
**83 passed, 2 skipped**. The two skips are existing filesystem symlink cases because
symlink creation is unavailable in this environment. The suite reports one existing
Starlette/HTTPX `TestClient` deprecation warning. In-process `TestClient` cases exercise
API and authorization logic; they do not prove transport encryption. Embedded Uvicorn
integration tests exercise HTTPS/WSS on loopback with production-style ephemeral
certificates.

## Automated coverage

- Session tests: credential generation and hashing, join-code validation and rate
  limits, approval, participant status, host-only control, heartbeat expiry, token
  revocation, and default `READ_ONLY` permissions.
- Permission/API tests: read-only browsing and download, read-only upload rejection,
  host-only permission updates, `READ_WRITE` upload success, global upload disable,
  revocation, transfer ownership, and structured errors.
- Transfer/file tests: directory confinement, hidden files, symlink and reparse-point
  handling, filename validation, 20,000,000-byte limit, no-overwrite behavior,
  chunked transfers, cancellation, revocation during an active upload, and `.part`
  cleanup.
- TLS tests: ephemeral certificate and SHA-256 fingerprint generation, local IP SANs,
  restrictive POSIX temporary permissions, and certificate cleanup.
- Loopback server tests: TLS-enabled Uvicorn startup, verified HTTPS, authenticated
  WSS and heartbeat, targeted permission events, mismatched fingerprint rejection,
  changed-certificate inspection, plaintext HTTP rejection, participant credential
  revocation, and certificate cleanup after shutdown.
- Filesystem/event tests: watchdog create, modify, delete, move, and nested-directory
  events; root restart and recovery; inaccessible subtree handling; hidden, symlink,
  and temporary-file filtering; validated event envelopes; bounded broker queues and
  resync hints; atomic-upload event delivery over loopback.
- GUI/notification tests: affected-directory refresh hints and directory-deletion
  navigation recovery, HTML-safe in-app fallback, and duplicate notification
  suppression. Native OS notification delivery is mocked and is not verified here.
- Discovery tests use fake Zeroconf records. Cross-platform metadata and hotspot
  adapters use unit tests and mocks; no real Wi-Fi adapter is exercised.

## Test with two physical computers

This manual test uses an existing Wi-Fi or Ethernet LAN first. Confirm that local
devices can reach each other; internet access is not required. Firewalls, VLANs, and
wireless client isolation may block the connection.

1. Install Python 3.11+ and ShareSpace on both computers with the README setup. Use a
Windows 10/11 and Linux pair for mixed-platform checks.
2. Start **Host Session** on one computer and choose **Host on this network**. Record
the host address and port, joining code, and the complete SHA-256 certificate
fingerprint shown by ShareSpace.
3. Give the participant the address, joining code, and fingerprint through a trusted
comparison channel. For example, read the fingerprint from the host display in
person or confirm it by voice with a known contact. Do not rely only on the mDNS
advertisement or another value sent over the same local network.
4. On the participant, select the discovered session or enter its local IPv4 address
and port manually. Compare/enter the fingerprint, confirm that it was checked through
the trusted channel, and verify the host before entering the join code. Confirm that
the UI reports a verified TLS connection. If the fingerprint differs, stop and do not
send the joining code.
5. Enter the joining code and request access. Approve the participant on the host.
The participant should connect with `READ_ONLY` permission. Confirm it can browse,
view file metadata, and download a file but cannot upload.
6. Select a test folder on the host and enable global uploads. Confirm that the
participant still cannot upload while `READ_ONLY`. Grant that participant
`READ_WRITE`; confirm the UI updates immediately and a new file uploads. Confirm a
duplicate filename is rejected without replacing the existing host file.
7. Start a larger test upload and revoke `READ_WRITE` while it is active. Confirm the
transfer stops, no destination file appears, and no temporary `.part` file remains.
Then grant `READ_WRITE` again, disable global uploads, and confirm uploads are still
rejected. This verifies that both permission conditions are required.
8. Disconnect the participant from the host. Confirm its file requests fail and its
active transfers stop. Join again if desired, then end the host session. Confirm the
server stops and its fingerprint changes in a new session.
9. Keep the participant browser open while another device creates, modifies, moves,
and deletes files in the shared tree. Confirm only affected listings refresh. For a
completed participant upload, confirm one `FILE_CREATED` update appears after the
host file is visible. Briefly disconnect and reconnect, then confirm the current
server listing is restored. Remove or disconnect the shared root and confirm the
host and participant report it unavailable.
10. Repeat with Windows hosting and Linux joining, then Linux hosting and Windows
joining. Record firewall and adapter behavior separately from the automated results.

If discovery fails but local TCP is permitted, use manual address and port entry. If
TLS verification fails, compare the newly presented fingerprint with the host through
the trusted channel. Do not accept it solely because the device appears in discovery.

## Hotspot checks

### Linux

The existing Version 0.2 NetworkManager adapter can be checked on a Linux computer
with a compatible radio/driver. Create the hotspot, host a session through **Host
using this hotspot**, and repeat the permission and transfer procedure above. Repeat
with internet uplink disconnected. Current Linux hotspot tests are mocked; radio
support, client association, and client-to-client communication have not been
verified on hardware.

### Windows

The current unpackaged development installation lacks the required `wiFiControl`
package identity/capability, and the Windows tethering API also needs a connection
profile. The existing adapter reports hotspot control unavailable in this build.
Hosting over an existing Wi-Fi or wired LAN remains available and is independent of
this limitation.

## Verification record and limits

| Verification | Result |
| --- | --- |
| Automated suite | 83 passed, 2 skipped; one Starlette/HTTPX deprecation warning |
| HTTP API and permission rules | In-process TestClient tests; application authorization verified, transport not covered by these cases |
| TLS certificate/IP SAN and cleanup | Automated certificate tests |
| HTTPS and WSS | Embedded Uvicorn loopback tests with ephemeral session certificates |
| Fingerprint mismatch/change handling | Loopback tests reject a wrong pin and report the presented fingerprint |
| Upload revocation and temporary-file cleanup | FileService active-upload cancellation test and permission integration tests |
| Filesystem watcher | Automated watchdog integration tests on the current Windows development host |
| WSS event validation and upload commit | TLS loopback checks; uploaded file is visible before its one `FILE_CREATED` event |
| Automatic browser updates | Qt-level event handling plus authenticated API refresh; no physical two-computer GUI run |
| Native desktop notifications | OS support and failure paths are mocked; Windows/Linux notification delivery was not tested |
| Windows/Linux physical communication | Not verified in this workspace |
| mDNS multicast and firewall traversal | Not verified on physical devices |
| Linux hotspot hardware | Not verified; adapter tests are mocked |
| Windows hotspot hardware | Unavailable in the current unpackaged development build |

Loopback TLS verifies local protocol integration. It does not establish that a
physical Windows/Linux pair can communicate through a specific router or firewall.
The self-signed fingerprint must be compared independently to authenticate the host.
TLS is transport encryption only; Version 0.6 does not implement end-to-end
encryption or persistent peer trust.
