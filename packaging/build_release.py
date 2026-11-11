"""Build and verify a native Windows or Linux ShareSpace release archive."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sharespace import __version__  # noqa: E402


def _runtime_distributions() -> list[importlib.metadata.Distribution]:
    with (ROOT / "pyproject.toml").open("rb") as project_file:
        metadata = tomllib.load(project_file)["project"]
    environment = default_environment()
    pending = [Requirement(requirement) for requirement in metadata["dependencies"]]
    distributions: dict[str, importlib.metadata.Distribution] = {}
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not requirement.marker.evaluate(environment):
            continue
        name = canonicalize_name(requirement.name)
        if name in distributions:
            continue
        try:
            distribution = importlib.metadata.distribution(requirement.name)
        except importlib.metadata.PackageNotFoundError as error:
            raise RuntimeError(
                f"Runtime dependency {requirement.name!r} is missing from the release environment."
            ) from error
        distributions[name] = distribution
        pending.extend(
            Requirement(child)
            for child in distribution.requires or ()
        )
    return sorted(distributions.values(), key=lambda distribution: (
        canonicalize_name(distribution.metadata.get("Name", ""))
    ))


def _write_third_party_notices(destination: Path) -> None:
    lines = [
        "# Third-party notices",
        "",
        f"ShareSpace {__version__} bundles runtime dependencies. Their license terms remain applicable.",
        "",
        "The ShareSpace project license has not been specified in this repository. This notice does not grant a license to ShareSpace source code.",
        "",
        "## Runtime dependency tree for this build platform",
        "",
        "| Package | Version | Declared license |",
        "| --- | --- | --- |",
    ]
    licenses_dir = destination / "THIRD_PARTY_LICENSES"
    licenses_dir.mkdir(parents=True, exist_ok=True)
    for distribution in _runtime_distributions():
        package_name = distribution.metadata.get("Name", "Unknown package")
        expression = (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or "See upstream license file"
        )
        lines.append(f"| {package_name} | {distribution.version} | {expression.replace('|', '/')} |")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", package_name)
        for entry in distribution.files or ():
            relative = Path(str(entry))
            leaf = relative.name.casefold()
            if "license" not in leaf and "copying" not in leaf and "notice" not in leaf:
                continue
            source = Path(distribution.locate_file(entry))
            if not source.is_file():
                continue
            target_dir = licenses_dir / safe_name
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target_dir / source.name)
    lines.extend([
        "",
        "The PySide6 community wheels declare LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only. Qt and PySide redistribution obligations apply to bundled Qt libraries. Review the Qt license and source-code requirements before public distribution:",
        "",
        "- https://doc.qt.io/qtforpython-6/",
        "- https://www.qt.io/development/open-source-lgpl-obligations",
        "",
        "PyInstaller is a build-time tool and is not part of the runtime package.",
        "",
    ])
    (destination / "THIRD_PARTY_NOTICES.md").write_text("\n".join(lines), encoding="utf-8")


def _version_info(version: str) -> str:
    numbers = tuple(int(part) for part in version.split("."))
    file_version = (*numbers, 0)
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={file_version}, prodvers={file_version},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'ShareSpace offline-first local file sharing'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'ShareSpace'),
      StringStruct('OriginalFilename', 'ShareSpace.exe'),
      StringStruct('ProductName', 'ShareSpace'),
      StringStruct('ProductVersion', '{version}')
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)"""


def _build_bundle(target: str, temp_root: Path) -> Path:
    dist_path = temp_root / "dist"
    work_path = temp_root / "work"
    spec_path = temp_root / "spec"
    for directory in (dist_path, work_path, spec_path):
        directory.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "PyInstaller",
        "--clean", "--noconfirm", "--onedir", "--windowed",
        "--name", "ShareSpace",
        "--distpath", str(dist_path),
        "--workpath", str(work_path),
        "--specpath", str(spec_path),
        "--add-data", f"{ROOT / 'assets' / 'sharespace.png'}{os.pathsep}assets",
        "--collect-submodules", "sharespace",
        "--collect-submodules", "zeroconf",
        "--collect-submodules", "watchdog",
    ]
    if target == "windows":
        version_file = temp_root / "windows-version-info.txt"
        version_file.write_text(_version_info(__version__), encoding="utf-8")
        command.extend([
            "--icon", str(ROOT / "assets" / "sharespace.ico"),
            "--version-file", str(version_file),
        ])
    command.append(str(ROOT / "src" / "sharespace" / "__main__.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    bundle = dist_path / "ShareSpace"
    executable = bundle / "ShareSpace.exe" if target == "windows" else bundle / "ShareSpace"
    if not executable.is_file():
        raise RuntimeError(f"PyInstaller did not create the expected executable: {executable}")
    if target == "linux" and not os.access(executable, os.X_OK):
        executable.chmod(executable.stat().st_mode | 0o111)
    return bundle


def _smoke_test(executable: Path, target: str, temp_root: Path) -> None:
    smoke_data = temp_root / "smoke-data"
    smoke_data.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["SHARESPACE_SMOKE_DATA_DIR"] = str(smoke_data)
    if target == "linux":
        environment["QT_QPA_PLATFORM"] = "offscreen"
        environment["XDG_DATA_HOME"] = str(smoke_data / "xdg-data")
        environment["XDG_STATE_HOME"] = str(smoke_data / "xdg-state")
    else:
        # Exercise Qt's native Windows window plugin in the packaged executable.
        environment["QT_QPA_PLATFORM"] = "windows"
        environment["LOCALAPPDATA"] = str(smoke_data / "local")
        environment["APPDATA"] = str(smoke_data / "roaming")
    try:
        result = subprocess.run(
            [str(executable), "--smoke-test"],
            cwd=temp_root,
            env=environment,
            capture_output=(target == "linux"),
            text=(target == "linux"),
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        log_path = smoke_data / "logs" / "sharespace.log"
        diagnostic_log = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if log_path.is_file() else "(no application log was created)"
        raise RuntimeError(
            f"Packaged application smoke test timed out. Last diagnostic log entries:\n{diagnostic_log}"
        ) from error
    if result.returncode != 0:
        log_path = smoke_data / "logs" / "sharespace.log"
        diagnostic_log = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if log_path.is_file() else "(no application log was created)"
        raise RuntimeError(
            f"Packaged application smoke test failed (0x{result.returncode & 0xFFFFFFFF:08X}).\n"
            f"stdout:\n{(result.stdout or '')[-4000:]}\nstderr:\n{(result.stderr or '')[-4000:]}\n"
            f"Application log:\n{diagnostic_log}"
        )
    if not (smoke_data / "sharespace.sqlite3").is_file():
        raise RuntimeError("Packaged application did not initialize its local database.")
    if not (smoke_data / "logs" / "sharespace.log").is_file():
        raise RuntimeError("Packaged application did not create its diagnostic log.")


def _install_test(package_root: Path, target: str, temp_root: Path) -> None:
    # Share the first packaged-launch database so reinstall/uninstall checks prove
    # that installation upgrades preserve the same local user data.
    smoke_data = temp_root / "smoke-data"
    environment = os.environ.copy()
    environment["SHARESPACE_SMOKE_DATA_DIR"] = str(smoke_data)
    if target == "windows":
        shell = shutil.which("powershell.exe") or shutil.which("powershell")
        if shell is None:
            raise RuntimeError("PowerShell is required to test the Windows user-local installer.")
        base = temp_root / "InstallTests"
        install_root = base / "Programs" / "ShareSpace"
        start_menu = base / "StartMenu"
        desktop = base / "Desktop"
        script_args = [
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(package_root / "install.ps1"),
            "-InstallRoot", str(install_root),
            "-StartMenuDirectory", str(start_menu),
            "-DesktopDirectory", str(desktop),
            "-CreateDesktopShortcut",
        ]
        subprocess.run([shell, *script_args], cwd=temp_root, env=environment, check=True)
        installed_executable = install_root / "ShareSpace.exe"
        if not installed_executable.is_file():
            raise RuntimeError("The Windows installer did not install ShareSpace.exe.")
        if not (start_menu / "ShareSpace.lnk").is_file() or not (desktop / "ShareSpace.lnk").is_file():
            raise RuntimeError("The Windows installer did not create the expected shortcuts.")
        _smoke_test(installed_executable, target, temp_root)
        subprocess.run([shell, *script_args], cwd=temp_root, env=environment, check=True)
        if not (smoke_data / "sharespace.sqlite3").is_file():
            raise RuntimeError("Reinstalling the application changed its local user data.")
        uninstall_args = [
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(package_root / "uninstall.ps1"),
            "-InstallRoot", str(install_root),
            "-StartMenuDirectory", str(start_menu),
            "-DesktopDirectory", str(desktop),
        ]
        subprocess.run([shell, *uninstall_args], cwd=temp_root, env=environment, check=True)
        if install_root.exists() or (start_menu / "ShareSpace.lnk").exists():
            raise RuntimeError("The Windows uninstaller left program files or shortcuts behind.")
        if not (smoke_data / "sharespace.sqlite3").is_file():
            raise RuntimeError("The Windows uninstaller removed user data.")
        return

    home = temp_root / "LinuxHome"
    xdg_data = temp_root / "LinuxData"
    xdg_bin = temp_root / "LinuxBin"
    environment.update({
        "HOME": str(home),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_BIN_HOME": str(xdg_bin),
    })
    package_root.mkdir(parents=True, exist_ok=True)
    install_script = package_root / "install.sh"
    subprocess.run(["bash", str(install_script)], cwd=temp_root, env=environment, check=True)
    installed_executable = xdg_data / "sharespace" / "app" / "ShareSpace"
    if not installed_executable.is_file():
        raise RuntimeError("The Linux installer did not install the ShareSpace executable.")
    launcher = xdg_bin / "sharespace"
    result = subprocess.run(
        [str(launcher), "--smoke-test"], cwd=temp_root, env=environment,
        capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Installed Linux application smoke test failed ({result.returncode}).\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    subprocess.run(["bash", str(install_script)], cwd=temp_root, env=environment, check=True)
    if not (smoke_data / "sharespace.sqlite3").is_file():
        raise RuntimeError("Reinstalling the Linux application changed its local user data.")
    subprocess.run(["bash", str(package_root / "uninstall.sh")], cwd=temp_root, env=environment, check=True)
    if installed_executable.exists() or launcher.exists():
        raise RuntimeError("The Linux uninstaller left program files or launchers behind.")
    if not (smoke_data / "sharespace.sqlite3").is_file():
        raise RuntimeError("The Linux uninstaller removed user data.")


def _check_payload(package_root: Path) -> None:
    forbidden_suffixes = {
        ".sqlite", ".sqlite3", ".db", ".key", ".p12", ".pfx", ".part"
    }
    for item in package_root.rglob("*"):
        if item.name == ".git" or item.suffix.casefold() in forbidden_suffixes:
            raise RuntimeError(f"The release payload contains a forbidden artifact: {item}")
        if item.is_file() and item.suffix.casefold() == ".pem":
            if b"PRIVATE KEY-----" in item.read_bytes():
                raise RuntimeError(f"The release payload contains private key material: {item}")
        if item.name.casefold() in {".env", ".env.local"}:
            raise RuntimeError(f"The release payload contains environment configuration: {item}")


def _write_install_guide(package_root: Path, target: str) -> None:
    if target == "windows":
        guide = """ShareSpace 1.0.0 for Windows x64

1. Extract this folder to a location you can write to.
2. From PowerShell run: powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\install.ps1
   The installer adds a Start Menu shortcut.
3. To also add a desktop shortcut, append: -CreateDesktopShortcut
4. Run uninstall.ps1 to remove application files and shortcuts.

Both scripts install per-user and require no administrator rights. Application settings and history are kept after uninstall. Windows hotspot creation is unavailable in this classic desktop package; host on an existing Wi-Fi or Ethernet network instead.

Minimum target: Windows 10 version 1809 or Windows 11 x64.
"""
    else:
        guide = """ShareSpace 1.0.0 for Linux x86_64

1. Extract the archive.
2. From the extracted folder run: ./install.sh
3. Launch ShareSpace from the desktop menu or run: ~/.local/bin/sharespace
4. To remove the application and desktop entry run: ./uninstall.sh

Installation is user-local and uses XDG directories. It does not need root access. Settings and history are kept after uninstall. Hotspot creation requires NetworkManager, nmcli, a compatible wireless adapter/driver, and desktop permissions.

Tested target: Ubuntu 22.04 x86_64 build environment. Other Linux distributions are not verified. The Qt runtime requires glibc 2.34 or newer.
"""
    (package_root / "INSTALL.txt").write_text(guide, encoding="utf-8")


def _create_archive(package_root: Path, target: str, output: Path) -> None:
    temporary_output = output.with_name(f"{output.name}.tmp-{os.getpid()}")
    root_name = output.name.removesuffix(".zip").removesuffix(".tar.gz")
    if target == "windows":
        with zipfile.ZipFile(temporary_output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as archive:
            for item in package_root.rglob("*"):
                if item.is_file():
                    relative = Path(root_name) / item.relative_to(package_root)
                    archive.write(item, relative.as_posix())
    else:
        with tarfile.open(temporary_output, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            archive.add(package_root, arcname=root_name, recursive=True)
    os.replace(temporary_output, output)


def _verify_archive(artifact: Path, target: str) -> None:
    """Check archive integrity and confirm essential install files are present."""
    root_name = artifact.name.removesuffix(".zip").removesuffix(".tar.gz")
    if target == "windows":
        with zipfile.ZipFile(artifact) as archive:
            broken_entry = archive.testzip()
            members = set(archive.namelist())
    else:
        with tarfile.open(artifact, "r:gz") as archive:
            members = {member.name for member in archive.getmembers()}
            broken_entry = None
    if broken_entry is not None:
        raise RuntimeError(f"Release archive contains a damaged entry: {broken_entry}")
    required = {
        f"{root_name}/app/ShareSpace.exe" if target == "windows"
        else f"{root_name}/app/ShareSpace",
        f"{root_name}/INSTALL.txt",
        f"{root_name}/app/THIRD_PARTY_NOTICES.md",
        f"{root_name}/install.ps1" if target == "windows" else f"{root_name}/install.sh",
        f"{root_name}/uninstall.ps1" if target == "windows" else f"{root_name}/uninstall.sh",
    }
    missing = sorted(required - members)
    if missing:
        raise RuntimeError(f"Release archive is missing required files: {', '.join(missing)}")


def _write_checksum(artifact: Path) -> Path:
    checksum = _sha256_file(artifact)
    checksum_path = artifact.with_suffix(artifact.suffix + ".sha256")
    checksum_path.write_text(f"{checksum}  {artifact.name}\n", encoding="ascii")
    return checksum_path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact_file:
        for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("windows", "linux"))
    parser.add_argument("--replace", action="store_true", help="replace an existing artifact of the same name")
    args = parser.parse_args()
    target = args.target or ("windows" if os.name == "nt" else "linux" if sys.platform.startswith("linux") else "")
    if target not in {"windows", "linux"}:
        parser.error("Release bundles must be built on native Windows or Linux.")
    if target == "windows" and os.name != "nt" or target == "linux" and not sys.platform.startswith("linux"):
        parser.error(f"The {target} bundle must be built on that native operating system.")

    artifact_dir = ROOT / "release-artifacts"
    artifact_dir.mkdir(exist_ok=True)
    suffix = "Windows-x64.zip" if target == "windows" else "Linux-x86_64.tar.gz"
    artifact = artifact_dir / f"ShareSpace-{__version__}-{suffix}"
    checksum_path = artifact.with_suffix(artifact.suffix + ".sha256")
    if (artifact.exists() or checksum_path.exists()) and not args.replace:
        parser.error(f"{artifact.name} already exists; pass --replace to intentionally rebuild it.")

    build_root = ROOT / "build" / "release-work"
    build_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{target}-", dir=build_root) as temporary:
        temporary_root = Path(temporary)
        bundle = _build_bundle(target, temporary_root)
        executable = bundle / "ShareSpace.exe" if target == "windows" else bundle / "ShareSpace"
        _smoke_test(executable, target, temporary_root)

        package_root = temporary_root / "package"
        package_root.mkdir()
        shutil.copytree(bundle, package_root / "app")
        _write_third_party_notices(package_root / "app")
        _write_install_guide(package_root, target)
        if target == "windows":
            shutil.copy2(ROOT / "packaging" / "windows" / "install.ps1", package_root / "install.ps1")
            shutil.copy2(ROOT / "packaging" / "windows" / "uninstall.ps1", package_root / "uninstall.ps1")
        else:
            shutil.copytree(ROOT / "assets", package_root / "assets")
            shutil.copy2(ROOT / "packaging" / "linux" / "install.sh", package_root / "install.sh")
            shutil.copy2(ROOT / "packaging" / "linux" / "uninstall.sh", package_root / "uninstall.sh")
            (package_root / "install.sh").chmod(0o755)
            (package_root / "uninstall.sh").chmod(0o755)
        _check_payload(package_root)
        _install_test(package_root, target, temporary_root)
        _create_archive(package_root, target, artifact)

    _verify_archive(artifact, target)
    checksum = _write_checksum(artifact)
    print(f"Created: {artifact}")
    print(f"SHA-256: {_sha256_file(artifact)}")
    print(f"Checksum file: {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
