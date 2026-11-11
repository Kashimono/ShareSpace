# Release and upgrade process

## Current 1.0.0 candidate

The project version is defined by `sharespace.__version__` and is consumed by Python
package metadata, the application window, API metadata, Windows file properties, and
the package names. Update the changelog and user-facing documentation with each
release.

The native build workflow is configured for Windows Server 2022 and Ubuntu 22.04. It
runs the automated suite, builds on the matching OS, launches the frozen application, exercises
the per-user install/upgrade/uninstall scripts, and retains the archive and checksum
as temporary workflow artifacts. It does not publish a GitHub Release or upload to an
external package service. The desktop application itself uses no cloud service and
does not require internet access at runtime.

`packaging/release-constraints.txt` pins the Python 3.11 build and runtime dependency
versions, including transitive dependencies. The workflow uses those constraints on
both operating systems so builds do not silently pick up newer Python package
releases. This workspace has verified the Windows artifact; the Linux job has not yet
been run.

## Build locally

Build each platform on that operating system with Python 3.11:

    python -m pip install -e ".[dev,build]" -c packaging/release-constraints.txt
    python -m pytest -q
    python packaging/build_release.py --target windows

On Linux, replace `windows` with `linux`. The build creates a one-folder PyInstaller
bundle, runs it with isolated temporary data, tests install/upgrade/uninstall while
checking that user data is retained, and writes an archive and SHA-256 file beneath
`release-artifacts/`. Use `--replace` only when intentionally rebuilding that
version's artifact.

Before handing an archive to anyone, verify its checksum, inspect the generated
`INSTALL.txt` and `THIRD_PARTY_NOTICES.md`, and confirm that the candidate has been
tested on its target operating system. A checksum is not a digital signature.

## Version and upgrade rules

- Use semantic versions: increment the patch number for compatible fixes, the minor
  number for compatible features, and the major number for incompatible changes.
- Keep application and database upgrades forward-only. Add each SQLite migration as
  a numbered transaction in `LocalStore`; retain pre-migration backups and test both
  successful and failed migration paths.
- The installer replaces only a directory carrying the ShareSpace install marker.
  Uninstall removes program files and shortcuts/desktop integration while keeping
  user settings, history, and logs.
- Build and test each OS archive natively. Do not infer Linux behavior from Windows
  tests or infer physical network behavior from loopback tests.
- Keep public release steps separate from packaging. A maintainer may review and
  publish a candidate only after approval, platform checks, and licensing review.

## Public distribution prerequisites

No project license has been selected or included in this repository. Do not present
the candidate as an approved public release until the project owner chooses a license
and confirms redistribution terms. PySide6/Qt's community distributions permit
LGPL/GPL or commercial licensing; bundling Qt requires following the selected terms,
including applicable source and relinking requirements. Review the official
[Qt for Python licensing information](https://doc.qt.io/qtforpython-6/) and
[Qt LGPL obligations](https://www.qt.io/development/open-source-lgpl-obligations).

The supplied local candidate build is not a substitute for a clean Linux package run,
physical Windows/Linux network testing, or review of the release license. Record
those results explicitly before calling a distribution production-ready.
