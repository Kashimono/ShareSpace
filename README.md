# ShareSpace

ShareSpace is an offline-first local file-sharing application for Windows and Linux.
Version 0.7.0 adds tree-wide filename search, improved shared-folder browsing,
persistent local transfer and session history, centralized settings, clearer connection
states, and safer error reporting. Runtime sharing works over the local network and
does not need a cloud service, public certificate authority, or internet connection.

## Development setup

Use Python 3.11 or newer. Install the project and development dependencies:

    python -m venv .venv
    .venv\Scripts\Activate.ps1
    python -m pip install -e ".[dev]"
    python -m sharespace

On Linux, activate with `source .venv/bin/activate`, then run the same install and
launch commands. The `cryptography` dependency creates session certificates locally.

## Host and join a session

From **Host Session**, choose **Host on this network** to use an existing Wi-Fi or
Ethernet network. Version 0.2 hotspot support is available through **Host using this
hotspot** after creating the hotspot. The session server binds to local IPv4
interfaces on an operating-system-assigned port and displays the address, joining
code, and SHA-256 certificate fingerprint.

Participants can select a discovered session or enter the host's local IPv4 address
and port. Before a join code or bearer credential is sent, the participant must
compare the fingerprint with the host using a trusted channel, such as reading it
directly from the host screen or confirming it with a known contact. The fingerprint
in mDNS discovery is convenient for display, but is not an independent trust check.
ShareSpace first inspects the certificate without sending application data, checks the
fingerprint, and then uses strict certificate and host-address verification for all
HTTPS and WSS communication.

The joining code is an authentication credential, not an address. Discovery records
contain public session metadata only; they never contain joining codes, host tokens,
participant tokens, or file information. When multicast discovery is unavailable,
manual local address and port entry is supported.

## Participant permissions and file sharing

Approved participants start with **READ_ONLY** permission. They can browse the
host-selected directory, view file information, and download permitted files. File
creation, modification, deletion, and directory changes are monitored on the host
and sent over the authenticated WSS session. The participant browser refreshes the
affected directory through the authenticated file API, so missed events are recovered
from the host's current listing after reconnect. The host dashboard shows recent file
and participant activity. ShareSpace requests native tray notifications when the
desktop reports support and otherwise displays an in-app banner; native delivery is
controlled by the operating system's notification settings.

The host can grant **READ_WRITE** to allow a participant to upload new files. This does
not allow overwriting, modifying, renaming, deleting, or creating directories.

Uploads require both the participant's **READ_WRITE** permission and the host's
global upload switch to be enabled. Either condition being false rejects an upload.
Revoking READ_WRITE cancels active uploads and removes incomplete temporary files;
the server checks authorization while processing each upload chunk and before
publishing the completed file. The global switch remains available as an emergency
disable for every participant.

Each file is limited to 20,000,000 bytes (20 MB decimal). Uploads create new files
only and fail if the destination name already exists. Paths shown to participants are
relative to the shared directory. Hidden files and symbolic links are excluded, and
traversal outside the selected directory is rejected.

The participant browser now searches filenames and folder names throughout the shared
tree. Search is case-insensitive, debounced, and runs away from the GUI thread. The
authenticated `GET /api/v1/files/search?q=...` endpoint returns safe relative paths
and metadata only; it never reads file contents. Each request returns at most 200
results by default (500 maximum), and the UI indicates when results were limited.
Search does not build an index, so large trees may take longer to scan. Breadcrumbs,
parent navigation, sortable listings, readable sizes, search-result locations, and
live event refreshes are available in the same browser. Upload access still requires
both `READ_WRITE` and the host's global upload switch.

## Local history and settings

Transfer and session history and user preferences are stored in a local SQLite
database under Qt's operating-system application-data location, outside any shared
folder. The database uses schema versioning and the Python standard-library
`sqlite3`; no database server or extra dependency is required. Transfer records are
saved after each transfer starts, at bounded progress intervals, and when it finishes
so a later launch can mark unfinished work `INTERRUPTED`. Downloads are completed in
history only after the local destination is finalized. Uploads are completed only
after the host publishes the file. Interrupted transfers are not resumed.

Session history is written only after hosting succeeds or a participant has been
authenticated. Normal termination, disconnection, failure, or an unclean application
exit receives an appropriate outcome. Previous sessions are metadata only; they are
not active or joinable and are never reconnected automatically. Transfer and session
history screens support filtering and sorting. Clearing history requires confirmation
and removes only terminal local history records, never shared files or active state.

Settings include system, light, and dark appearance; notification categories; a
default download folder; and history retention of 30, 90, or 365 days, or indefinitely.
The download folder is selected with the operating system's folder picker and checked
for availability. Settings do not change host sharing, active network connections, or
security configuration. History and settings never include bearer credentials,
joining codes, private keys, file contents, or absolute host shared-directory paths.

The participant status distinguishes certificate verification, connection setup,
authenticated TLS connection, reconnection, and host unavailability. Reconnection
uses exponential delays capped at ten seconds and stops after twelve consecutive
failures. The host dashboard reports server/session state, connected participants,
shared-folder and monitor status, event streams, and active transfers.

## TLS and certificate trust

Each host session creates an ephemeral self-signed RSA certificate. Its subject
alternative names include the local IP addresses used by the server, loopback IPs,
and localhost. The host's SHA-256 fingerprint is shown in the host interface and can
be copied. The private key is stored in a temporary session directory and removed
when the server stops. On POSIX systems the key and directory use mode `0600` and
`0700`; Windows uses the temporary directory's inherited access-control list.

HTTP uses HTTPS and event connections use WSS, with TLS 1.2 as the minimum and newer
protocols negotiated when available. A client pins the self-signed certificate only
after the fingerprint comparison, then validates the certificate, its validity
period, and the requested IP address for every encrypted connection. ShareSpace does
not fall back to plaintext HTTP or WebSockets. Transport encryption protects traffic
on the local network; this milestone does not implement end-to-end encryption.

## Platform notes

The same Python TLS, HTTP, WebSocket, and permission logic is shared by Windows and
Linux. Hotspot control remains behind the existing platform-specific adapters. The
current unpackaged Windows development install lacks the required `wiFiControl`
package capability, and the Windows tethering API also needs a connection profile.
Linux hotspot tests remain mocked. Hosting on an existing local network does not
depend on hotspot creation.

## Tests and documentation

Run the automated suite with:

    python -m pytest -q

The Version 0.7.0 Windows workspace run reports **105 passed, 3 skipped**, and one
existing Starlette/HTTPX deprecation warning. The skips are symlink tests because
symlink creation was unavailable in this environment. Tests cover file search and
authorization, versioned database initialization and restart recovery, history and settings,
connection retry states, file-browser behavior, transfer streaming, permissions,
upload cancellation and cleanup, certificate pinning, HTTPS/WSS loopback connections,
filesystem events, event validation/backpressure, and notification fallback. The
Qt startup test uses the offscreen platform plugin. Windows filesystem monitoring was
exercised; Linux filesystem monitoring and native Windows/Linux notification delivery
have not been physically verified. Loopback tests do not establish physical
Windows/Linux connectivity, multicast reachability, firewall behavior, or hotspot
support.

See [docs/architecture.md](docs/architecture.md) for module boundaries, persistence,
search, and API authorization, and [docs/networking-tests.md](docs/networking-tests.md)
for test coverage, limitations, and manual two-computer instructions.
