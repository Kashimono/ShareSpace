# ShareSpace

ShareSpace is an offline-first wireless file-sharing application for Windows and Linux.

## Development setup

Use Python 3.11 or newer. Create a virtual environment and install the project and its
runtime dependencies. The Windows-only PyWinRT projections are installed only on
Windows; pytest is an optional development dependency.

    python -m venv .venv
    .venv\Scripts\Activate.ps1
    python -m pip install -e ".[dev]"
    python -m sharespace

On Linux, activate the environment with source .venv/bin/activate and then run the
same install and launch commands.

## Version 0.2: local hotspot

The Host Session screen can check adapters, configure an SSID and WPA password, start
and stop a local hotspot, and show the host interface address. The Join Session entry
point remains a placeholder. File transfers, peer discovery, session codes, and
participant management are not implemented.

The Linux adapter uses NetworkManager and nmcli. NetworkManager must be running and
the selected radio/driver must report AP mode support. If that radio is connected to
another network, ShareSpace asks before interrupting it and attempts to restore the
prior profile on stop.

Windows uses the supported Windows Runtime tethering API, not legacy hosted-network
commands. That API requires a packaged application declaring the wiFiControl device
capability and a Windows connection profile. The current Python development install
has no package identity, so it reports Windows hotspot control as unavailable. The
Windows API is designed for tethering a connection; offline-only hotspot operation
has not been established.

No cloud service is used. Linux hotspot creation does not need Internet; Windows
offline operation remains a platform limitation. Dependencies must be installed
before first use.

## Testing

Run the automated mocked tests with:

    python -m pytest -q

These tests mock Windows APIs, PowerShell, and nmcli. They do not create a real
hotspot or verify client connections.

Hardware test steps and platform limitations are documented in
[docs/networking-tests.md](docs/networking-tests.md).

See docs/architecture.md for the architecture and milestone boundaries.
