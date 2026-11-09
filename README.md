# ShareSpace

ShareSpace is an offline-first local file-sharing application for Windows and Linux.
Version 0.3.0 adds temporary sessions, local discovery, join approval, and participant
monitoring. File browsing and file transfer are future milestones and are not included.

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

## Dependencies and local transport

Runtime dependencies are PySide6, FastAPI, Uvicorn, HTTPX, WebSockets, Zeroconf,
psutil, and Pydantic. Network traffic stays on the local network. Session requests use
HTTP and real-time events use WebSockets. This milestone does not encrypt that traffic;
do not describe the protocol as end-to-end encrypted. Broader encryption and identity
work remains for a later security milestone.

Windows hotspot control retains the Version 0.2 limitation: the current unpackaged
development install lacks the required `wiFiControl` package capability, and the
Windows tethering API also needs a connection profile. Linux hotspot tests remain
mocked and do not verify a real adapter. Hosting over an existing local network is
available independently of hotspot creation.

## Automated tests

Run the suite with:

    python -m pytest -q

The session tests exercise the state machine, HTTP/WebSocket routes, validation,
credential checks, mDNS data parsing, deduplication, and stale-entry handling. The
discovery tests use fake Zeroconf records. A loopback integration test exercises the
embedded Uvicorn server and WebSocket stack. These tests do not verify two physical
devices, multicast reachability, firewall behavior, or hotspot connectivity.

See [docs/architecture.md](docs/architecture.md) for module boundaries and API
messages, and [docs/networking-tests.md](docs/networking-tests.md) for test results,
limitations, and two-computer test steps.
