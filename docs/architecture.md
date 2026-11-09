# Initial architecture

## Requirements addressed in this milestone

- Provide one shared Python application and UI for Windows and Linux.
- Keep operating-system-specific network operations out of shared application logic.
- Support an offline-first product without cloud services or a mandatory runtime
  internet connection.
- Show the Host Session and Join Session entry points without implying that either
  action already creates or joins a session.

The project directory had no existing source files or project plan. This document
records the architecture and scope used for the first milestone so later work can
extend it incrementally.

## Layers

- sharespace.app owns Qt application startup and process lifetime.
- sharespace.ui contains presentation code. It communicates user intent through
  callbacks and does not contain platform networking code.
- Future shared session and transfer logic will live in separate application/domain
  modules. Windows and Linux network adapters will be separate platform modules and
  will be selected behind shared interfaces.

Only the startup and presentation layers exist in this milestone. The future modules
are intentionally not stubbed until their corresponding milestones define their
responsibilities.

## Runtime and dependencies

- Python 3.11 or newer.
- PySide6 (Qt 6) for a cross-platform desktop UI.
- No cloud SDKs, hosted services, or runtime network dependency.

PySide6 is the only application dependency. Python packaging metadata is in
pyproject.toml, using a src/ layout to keep importable application code separate
from repository files.

## Current interaction boundary

Host Session and Join Session buttons report that the session workflow is not
available yet. This gives the UI a reviewable entry point without adding hotspot
creation, discovery, sockets, session networking, or file transfer behavior early.
