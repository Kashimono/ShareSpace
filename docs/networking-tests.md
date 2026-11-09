# Version 0.3.0 session and networking tests

## Automated tests

Install dependencies and run the project suite:

    python -m pip install -e ".[dev]"
    python -m pytest -q

Session tests cover:

- Session and credential generation, unique IDs, code hashing, and host identity.
- Join-code validation, duplicate-request idempotence, approval and rejection.
- Participant registration, role/state reporting, heartbeat updates, reconnects, and
  stale participant expiry.
- Host authorization, participant disconnect, and session teardown.
- FastAPI metadata, validation, request polling, and host endpoints.
- WebSocket authentication, heartbeat acknowledgement, state events, and host
  termination notification.
- Fake Zeroconf records, duplicate session advertisements, stale entry removal, and
  metadata secrecy.
- Windows-host/Linux-participant and Linux-host/Windows-participant protocol metadata.

The API suite uses FastAPI's in-process TestClient; the discovery suite uses fake mDNS
records. One loopback integration test binds the embedded Uvicorn server and exercises
HTTP approval, a real WebSocket heartbeat, host disconnect, and teardown. The tests do
not send multicast on a real interface, traverse a firewall, or verify physical client
connections.

## Two-computer test over an existing local network

This is the recommended first hardware test because it does not rely on independent
hotspot creation. Both computers must share a local network that permits client-to-host
traffic. Internet access is not needed, but a router or access point may still isolate
wireless clients from one another.

1. On both computers, install and launch ShareSpace using the development setup in the
   README. Use a Windows 10/11 and Linux pair for mixed-platform testing.
2. Connect both computers to the same local Wi-Fi or wired LAN. For an offline test,
   use a local network with its Internet uplink disabled while leaving local device
   communication enabled.
3. On the host, choose **Host Session**, enter a session name, and select **Host on
   this network**. The app chooses a local IPv4 address and random port and displays
   `IP:port`, session ID, and joining code.
4. If Windows Firewall asks, allow the ShareSpace Python application on the trusted
   private network. Firewall policy may require an administrator to allow inbound TCP
   to the displayed port. The port is selected dynamically for each session.
5. On the participant, choose **Join Session**. Select the discovered session. If no
   session appears after refresh, enter the host IP and port manually. Enter the
   joining code supplied by the host and request access.
6. On the host, confirm the device and OS shown in Pending join requests, then approve
   the request. Confirm the participant reports Connected and the host count changes.
7. On the host, select the participant and choose **Disconnect participant**. Confirm
   the participant sees the disconnection. Start a fresh join request to test approval
   again.
8. To test approval again after a host disconnect, close and reopen the participant's
   Join Session window to start a fresh join request. Then end the session from the host.
   Confirm the participant sees the host-ended event, the server closes, and the session
   disappears from discovery after the service withdrawal is received.
9. Repeat with Windows hosting and Linux joining, then Linux hosting and Windows
   joining. Windows hotspot capability is not part of this LAN test.

If mDNS is unavailable but the local network permits TCP, manual IP and port connection
should still work. If neither device can reach the host, check firewall rules, client
isolation, VLAN/subnet routing, selected host interface, and whether the host IP is
reachable from the participant.

## ShareSpace-created hotspot test

### Linux

Linux hotspot creation still uses the Version 0.2 NetworkManager adapter. On a Linux
device with a radio/driver that reports AP mode support:

1. Open Host Session, select a wireless adapter, check compatibility, configure the
   SSID and password, and create the hotspot.
2. After the OS reports the hotspot active, choose **Host using this hotspot**.
3. Join from a second device and complete the same approval and teardown steps above.
4. Repeat with the Internet uplink disconnected. Verify host/participant local
   connectivity, and confirm the hotspot and session stop cleanly.

This is a manual test procedure only. Linux hotspot creation, client association,
client isolation behavior, and session traffic have not been hardware-verified in this
milestone.

### Windows

The current unpackaged Windows development install lacks the required `wiFiControl`
package identity/capability, so the existing Version 0.2 adapter reports hotspot
control unavailable. The session fallback can still be tested on an existing local
network. Do not treat hosting on the current LAN as proof that Windows hotspot creation
works. A future packaged build must declare the capability, then test Windows 10 and
Windows 11 separately.

## Verification record

| Verification type | Status |
| --- | --- |
| Session state, API, and WebSocket automated tests | 36 tests pass, including one loopback Uvicorn/WebSocket integration test |
| Discovery logic and advertisement metadata | Fake Zeroconf records only; no multicast hardware check |
| Real local server and client protocol | Loopback HTTP and WebSocket lifecycle verified |
| Existing Wi-Fi/LAN with two computers | Not hardware-verified in this milestone |
| ShareSpace-created Linux hotspot | Mocked tests only; no radio or client connection verified |
| ShareSpace-created Windows hotspot | Unavailable in this unpackaged build because of the existing `wiFiControl` capability limitation |
| Cross-platform Windows/Linux physical connections | Not verified |
| Encryption | Not implemented; local HTTP and WebSocket traffic is unencrypted |

## Troubleshooting

- **No sessions appear:** use Refresh, check that multicast/mDNS is allowed, and try
  manual local IPv4 address and port.
- **Manual connection fails:** confirm the host still shows an active session, the
  address is reachable from the participant, and inbound TCP is allowed for the
  displayed random port.
- **Join code rejected:** re-enter the host's current code. Codes change when sessions
  are recreated and are not IP addresses.
- **Host approves but participant does not connect:** inspect firewall prompts and
  client isolation. The participant must reach the displayed host address and port.
- **Hotspot clients cannot join:** some access points isolate associated clients. Use
  an existing LAN with client communication enabled to distinguish that from session
  logic.

This milestone tests session membership only. It does not browse files or transfer
content.
