# ShareSpace

ShareSpace is an offline-first local file-sharing application for Windows and Linux.
Version 0.4.0 adds authenticated browsing, downloads, and optional uploads for a
host-selected directory.

## Development setup

Use Python 3.11 or newer. Install the application and its local runtime dependencies:

    python -m venv .venv
    .venv\Scripts\Activate.ps1
    python -m pip install -e ".[dev]"
    python -m sharespace

On Linux, activate the environment with `source .venv/bin/activate` and use the same
install and launch commands. ShareSpace has no cloud service or Internet requirement
at runtime.

## Create or join a session

From **Host Session**, choose **Host on this network** to use an existing local Wi-Fi
or Ethernet network. To use Version 0.2 hotspot support, create the hotspot first and
choose **Host using this hotspot**. The session server binds to all local IPv4
interfaces on an operating-system-assigned port. ShareSpace displays the selected
host IP, port, session ID, and joining code.

The joining code is an authentication credential. It is not an address. Hosts advertise
public session metadata with Zeroconf/mDNS; the advertisement never contains the
joining code, host token, participant token, or file information. Participants can
select a discovered session, enter its code, and wait for host approval. If multicast
discovery is blocked, enter the host's local IPv4 address and displayed port manually.

The host can approve or reject requests, view participant roles and connection states,
disconnect a connected participant, and end the session. WebSocket heartbeats detect
stale connections. Session state and credentials exist only in process memory.

## Share files in a session

After starting a session, the host chooses one directory from the session dashboard.
Participants can browse and download its files after the host approves them and their
session connection is active. Uploads are disabled by default; the host can enable or
disable them at any time. Each file is limited to 20,000,000 bytes (20 MB decimal).
Uploads create new files only and fail if the destination name already exists.

Paths shown to participants are relative to the shared directory. Hidden files and
symbolic links are excluded, traversal outside the selected directory is rejected, and
the selected root stays on the host. The file service does not create folders, delete
files, synchronize changes, or preserve transfer history after a session ends.

## Dependencies and local transport

Runtime dependencies are PySide6, FastAPI, Uvicorn, HTTPX, WebSockets, Zeroconf,
psutil, and Pydantic. Network traffic stays on the local network. Session and file
requests use HTTP, real-time events use WebSockets, and file contents stream in bounded
chunks. Traffic is not encrypted; use trusted local networks. TLS and stronger identity
work are later security milestones.

Windows hotspot control retains the Version 0.2 limitation: the current unpackaged
development install lacks the required `wiFiControl` package capability, and the
Windows tethering API also needs a connection profile. Linux hotspot tests remain
mocked and do not verify a real adapter. Hosting over an existing local network is
available independently of hotspot creation.

## Automated tests

Run the suite with:

    python -m pytest -q

The suite exercises session state and HTTP/WebSocket routes, mDNS parsing, file path
confinement, authentication, transfer limits and cancellation, and loopback file
transfers through the embedded server. It does not verify two physical devices,
multicast reachability, firewall behavior, or hotspot connectivity.

See [docs/architecture.md](docs/architecture.md) for module boundaries and API
messages, and [docs/networking-tests.md](docs/networking-tests.md) for test results,
limitations, and two-computer test steps.
