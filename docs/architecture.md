# ShareSpace architecture

Version 0.5.0 adds participant upload permissions and encrypted local sessions to the
Version 0.4 file-sharing engine. It keeps the existing FastAPI/Uvicorn server,
WebSocket event stream, Qt UI, and separate Windows and Linux hotspot adapters.
Sessions and certificates work offline; there is no public certificate authority or
cloud dependency.

## Module boundaries

### Shared core

- `sharespace.core.models` defines validated session, participant, permission,
  directory, and transfer models shared by the API and UI.
- `sharespace.core.session_manager.SessionManager` owns in-memory session state,
  credentials, join approval, participant identity, heartbeats, and permissions. Each
  approved participant starts as `READ_ONLY`. It is the authority for role, active
  connection, and permission; the client never supplies these values.
- `sharespace.core.file_manager.SharedDirectoryManager` owns the host-selected root,
  relative-path validation, hidden-item filtering, symlink/reparse rejection, and
  confinement checks.
- `sharespace.core.transfer_manager.TransferManager` tracks transfer state, byte
  counts, cancellation, and bounded in-memory history. It stores no file content.
- `sharespace.core.device_manager.DeviceManager` identifies the device and available
  local IPv4 addresses and selects the host address without hardcoding it.

### Local networking

- `sharespace.networking.tls_security.SessionCertificateManager` creates one
  ephemeral self-signed RSA certificate and private key per host session. IP SANs
  cover all supplied local addresses and loopback addresses. The SHA-256 fingerprint
  is attached to public session metadata. Temporary key files are removed when the
  server stops. POSIX permissions are set to `0600` for files and `0700` for the
  directory; Windows uses the inherited access-control list of its temporary folder.
- `TLSConnectionSecurity` first inspects the peer certificate without sending HTTP,
  WebSocket data, or credentials. It compares the DER certificate's SHA-256 hash to
  the expected fingerprint, then creates a strict TLS context that requires the
  pinned certificate, validates its lifetime, and checks the host IP against the
  certificate SAN. TLS 1.2 is the minimum. There is no HTTP/WSS plaintext fallback.
- `sharespace.networking.session_server.SessionServer` binds a local socket and starts
  Uvicorn with the session certificate and key. HTTPS hosts the REST endpoints and
  WSS hosts the event endpoint. Startup and shutdown clean up the key material.
- `sharespace.networking.session_client.SessionClient` reuses a thread-safe HTTPX
  connection pool for HTTPS API requests and uses the same pin for WSS, approval,
  host controls, heartbeat events, and file-service control requests.
- `sharespace.networking.discovery.SessionAdvertiser` publishes public metadata on
  `_sharespace._tcp.local.` using protocol version 2. Discovery includes the
  fingerprint so the participant can compare it, but mDNS is not an independent
  trusted channel.
- `sharespace.networking.file_routes` authenticates every file operation. Browsing,
  metadata, and download require a currently connected participant. Upload additionally
  requires `READ_WRITE` and the host's global upload switch.
- `sharespace.networking.file_service.FileService` streams 64 KiB chunks. Its async
  authorization gate serializes permission changes, host upload toggles, participant
  disconnects, root changes, chunk writes, and final file publication. It cancels
  active uploads when access is revoked and removes incomplete temporary files.
- `sharespace.networking.file_client.FileClient` verifies the pinned certificate for
  file operations, streams without loading whole files into memory, and creates local
  download files without overwriting an existing destination.

### Qt presentation

- `HostSessionDialog` preserves the existing hotspot and network choices.
- `SessionHostDialog` shows the security status, negotiated TLS version, session
  fingerprint, requests, participant roles/status/permission, global upload setting,
  and transfer state. The host can copy the fingerprint, grant/revoke `READ_WRITE`,
  disconnect participants, and stop sharing.
- `SessionJoinDialog` shows the expected fingerprint. It requires the participant to
  affirm an independent trusted-channel comparison before verification and before the
  joining code is sent. TLS errors and replacement fingerprints are surfaced to the
  participant.
- `FileBrowserDialog` keeps browsing and downloading available to `READ_ONLY`
  participants. Upload controls require both `READ_WRITE` and the globally enabled
  upload state. It updates when a targeted permission event arrives.
- `sharespace.ui.session_workers` runs blocking setup and network work away from the
  Qt presentation thread.

The Windows and Linux hotspot implementations remain in their platform-specific
modules. TLS, session logic, file services, and permissions are shared.

## Session, permission, and trust flow

```mermaid
sequenceDiagram
    participant Host as Host UI
    participant Server as TLS SessionServer
    participant Peer as Participant UI
    Host->>Server: Generate ephemeral certificate and start HTTPS/WSS
    Server-->>Host: Session address, joining code, SHA-256 fingerprint
    Host->>Peer: Share address, joining code, fingerprint through trusted channel
    Peer->>Server: Credential-free certificate inspection handshake
    Peer->>Peer: Compare fingerprint and require confirmation
    Peer->>Server: HTTPS GET session and submit join request
    Host->>Server: Approve request using host bearer token
    Peer->>Server: Poll approval, authenticate WSS
    Server-->>Peer: Authenticated event with READ_ONLY permission
    Host->>Server: Grant READ_WRITE over HTTPS
    Server-->>Peer: Targeted permission change event over WSS
    Peer->>Server: Upload over HTTPS only if READ_WRITE and global switch enabled
```

The joining code is a 12-character secret; host, request, and participant credentials
are generated with `secrets`. The joining code is stored as a salted PBKDF2-HMAC-SHA256
digest in memory and failed code attempts are rate-limited. Approved participant
tokens are temporarily available only through the matching request token. Disconnect
and session-end operations revoke credentials. Active state is not persisted.

## Participant permissions and upload evaluation

The host is reported separately with role `HOST` and no participant permission.
Ordinary approved participants have one of:

| Permission | Browse, view metadata, download | Upload new files |
| --- | --- | --- |
| `READ_ONLY` | Allowed while connected | Rejected |
| `READ_WRITE` | Allowed while connected | Allowed only while host uploads are enabled |

`READ_WRITE` permits only a new upload. It does not add file modification, deletion,
rename, or directory creation operations. Host-only settings and participant
management remain restricted to the host token. Transfer queries and cancellation
remain scoped to the authenticated participant; the host may view/cancel any session
transfer.

For each upload, the server evaluates the conditions in this order:

1. Validate the bearer token, active session, participant identity, and connected state.
2. Require `READ_WRITE` participant permission.
3. Require the host's global uploads setting and a selected root.
4. Validate filename, declared size, destination path, and no-overwrite rules.
5. Recheck participant permission and the global setting under the authorization gate
   before creating the temporary file, before each chunk write, and before atomically
   publishing the final file.

The host's permission route updates the server-owned permission while holding the
same gate used by upload writes and commit. On `READ_WRITE` to `READ_ONLY`, active
uploads are cancelled, their `.part` files are deleted, and a targeted
`participant_permission_changed` event updates the participant UI. If an event is
missed, the next API request still uses the new server permission. A host disconnect
or session shutdown cancels that participant's active transfers.

## HTTP API and authorization

Routes use the `/api/v1` prefix and structured JSON errors. Session request validation
does not echo submitted values such as join codes. Access and transport are separate:
all application API and event communication runs over HTTPS/WSS using the pinned
session certificate.

| Method and route | Purpose | Authorization |
| --- | --- | --- |
| `GET /api/v1/health` | Local server health | None; HTTPS only in production session server |
| `GET /api/v1/session` | Public session metadata and fingerprint | None; HTTPS only in production session server |
| `POST /api/v1/sessions/{session_id}/join-requests` | Validate code and request approval | Joining code in body, HTTPS |
| `GET /api/v1/join-requests/{request_id}` | Poll and retrieve approved token | Request bearer token, HTTPS |
| `GET /api/v1/host/snapshot` | Read requests and participants | Host bearer token, HTTPS |
| `POST /api/v1/host/join-requests/{request_id}/approve` | Approve request | Host bearer token, HTTPS |
| `POST /api/v1/host/join-requests/{request_id}/reject` | Reject request | Host bearer token, HTTPS |
| `POST /api/v1/host/participants/{participant_id}/permissions` | Change participant permission | Host bearer token, HTTPS |
| `POST /api/v1/host/participants/{participant_id}/disconnect` | Revoke participant access | Host bearer token, HTTPS |
| `POST /api/v1/host/end` | End session | Host bearer token, HTTPS |
| `GET /api/v1/host/files/state` | Read host file-sharing state | Host bearer token, HTTPS |
| `POST /api/v1/host/files/root` | Select the shared root | Host bearer token, HTTPS |
| `POST /api/v1/host/files/stop` | Stop sharing the selected root | Host bearer token, HTTPS |
| `POST /api/v1/host/files/uploads` | Enable or disable global uploads | Host bearer token, HTTPS |
| `GET /api/v1/host/files/transfers` | Read all session transfer states | Host bearer token, HTTPS |
| `POST /api/v1/host/files/transfers/{transfer_id}/cancel` | Cancel any transfer | Host bearer token, HTTPS |
| `GET /api/v1/files/settings` | Read shared folder name and permission | Connected participant token, HTTPS |
| `GET /api/v1/files?path=...` | Browse relative directory | Connected participant token, HTTPS |
| `GET /api/v1/files/info?path=...` | Read file metadata | Connected participant token, HTTPS |
| `GET /api/v1/files/download?path=...` | Download a file up to 20 MB | Connected participant token, HTTPS |
| `POST /api/v1/files/upload?path=...` | Upload a new file | Connected token, `READ_WRITE`, global uploads enabled, HTTPS |
| `GET /api/v1/files/transfers` | Read participant transfer state | Connected participant token, HTTPS |
| `POST /api/v1/files/transfers/{transfer_id}/cancel` | Cancel own transfer | Connected participant token, HTTPS |

The WSS endpoint is `/api/v1/sessions/{session_id}/events`. The first message
authenticates with a bearer token after TLS is established. Participants send a
heartbeat every five seconds. Permission changes are sent only to the affected
participant. File-sharing state events include only availability, global upload
status, and the shared directory display name. Events never include joining codes,
bearer tokens, private keys, or filesystem paths.

## Security boundaries and limitations

- The certificate is self-signed, ephemeral, and pinned for the active connection.
  Users must compare its fingerprint through an independent trusted channel. A value
  obtained only from the same local network or mDNS record does not protect against a
  network attacker who can replace both the record and certificate.
- TLS provides transport encryption and server identity after that fingerprint check.
  ShareSpace does not provide end-to-end encryption, public-key user identity, or
  protection after a trusted device is compromised.
- Private-key file mode is restrictive on POSIX. Windows uses the platform's
  temporary-directory ACL; this development build does not set a custom Windows ACL.
- Certificates are valid for 30 days but are removed with the session. There is no
  persistent trust store; clients must compare a new fingerprint for a new session.
- Session data, authentication digests, and transfer history are in memory. The app
  disables Uvicorn access logs, logs authorization failure codes without request
  bodies, and avoids logging credential values.
- Files are limited to 20,000,000 bytes, transferred in 64 KiB chunks. Uploads use a
  same-directory temporary file and atomic no-overwrite publication. Symbolic links
  and reparse points are not followed, hidden entries are not served, and the selected
  root is never changed by participant requests.
- mDNS and the randomly selected local port may be blocked by firewalls, VLANs,
  multicast settings, or client isolation. Hotspot behavior remains limited by the
  existing platform adapters. Physical Windows/Linux network behavior requires
  separate hardware testing.

The implementation preserves the Version 0.4 file size limit and file operations.
File deletion, rename, participant-created directories, synchronization, persistent
history, cloud services, and end-to-end encryption are outside Version 0.5.
