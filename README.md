# ShareSpace

ShareSpace is an offline-first wireless file-sharing application for Windows and Linux.

## Development setup

Use Python 3.11 or newer. Create a virtual environment and install the project and its
runtime dependency:

    python -m venv .venv
    .venv\Scripts\Activate.ps1
    python -m pip install -e .
    python -m sharespace

On Linux, activate the environment with source .venv/bin/activate and then run the
same install and launch commands.

The first milestone provides the application shell and Host Session / Join Session
interface. Those controls are presentation placeholders; session discovery, hotspot
creation, networking, and file transfers are not implemented yet.

See docs/architecture.md for the initial architecture and milestone boundaries.
