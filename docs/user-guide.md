# ShareSpace 1.0.0 user guide

ShareSpace shares files directly between nearby Windows and Linux computers over a
local Wi-Fi or Ethernet network. The application does not need a cloud account or an
internet connection. The computers do need a network path that allows them to reach
each other.

## Install and remove

### Windows

1. Extract the Windows x64 archive to a folder you can write to.
2. Open PowerShell in that folder and run
   `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`.
   Add `-CreateDesktopShortcut` to that command if you also want a desktop shortcut.
3. Start ShareSpace from the Start menu.
4. To remove it, run `./uninstall.ps1` from the extracted folder.

The installer is per-user and needs no administrator rights. Uninstall removes the
program files and shortcuts; it keeps settings and local history. The package target
is Windows 10 version 1809 or newer and Windows 11 x64.

### Linux

1. Extract the Linux x86_64 archive.
2. Run `./install.sh` from the extracted folder.
3. Start ShareSpace from the desktop menu or run `~/.local/bin/sharespace`.
4. To remove it, run `./uninstall.sh` from the extracted folder.

The installer uses XDG user directories and needs no root access. It keeps settings
and local history after uninstall. The Linux package target is Ubuntu 22.04 x86_64 and
requires glibc 2.34 or newer; the Linux package has not been built or verified in this
workspace. Other distributions are not verified.

## Host or join

### Host a session

1. Choose **Host Session**.
2. Choose **Host on this network** to use an existing Wi-Fi or Ethernet connection.
3. Select the folder to share and note the displayed local address, port, joining code,
   and SHA-256 certificate fingerprint.
4. Give the joining code and fingerprint to the other person through a trusted
   comparison channel, such as reading them directly from your screen.
5. Approve each join request. New participants receive read-only access.

Windows hotspot creation is unavailable in this classic desktop package. On
Linux, hotspot creation depends on NetworkManager, `nmcli`, the wireless adapter and
driver, and desktop permissions. Hosting on an existing network does not use hotspot
creation.

### Join a session

1. Choose **Join Session**, then select a discovered session or enter its local IPv4
   address and port.
2. Compare the full fingerprint with the host over a trusted channel. Do not rely on
   the fingerprint shown by discovery alone.
3. Confirm the comparison, enter the joining code, and wait for the host to approve
   the request.
4. The host may grant read-write access. Uploads also require the host's global upload
   switch to be enabled.

ShareSpace pins the host certificate only after the fingerprint check. It does not
fall back to plaintext networking. A new session has a new fingerprint.

## File access and history

Participants can browse, search, and download files that are visible beneath the
host-selected folder. Read-write access allows creating new files by upload; it does
not allow overwriting, deleting, renaming, or creating folders. Hidden files and
symbolic links are not shared. Transfers are limited to 20 MB per file.

Settings, transfer history, and session history stay on the local computer in the OS
application-data directory. They do not contain file contents or session credentials.
The Settings screen controls theme, notification categories, download folder, and
history retention. Clearing history does not remove shared files. Uninstalling the
application also leaves settings and history in place.

## Checksums

Each archive has a sibling `.sha256` file containing its SHA-256 checksum. After
downloading, calculate the archive checksum locally and compare all 64 hexadecimal
characters before extracting it. A checksum detects accidental corruption; it does
not prove who produced a package unless the checksum itself came through a trusted
channel.

## Report a problem

Report bugs through the [ShareSpace issue tracker](https://github.com/Kashimono/ShareSpace/issues).
Include the application version, operating system, steps to reproduce, and a
credential-free log excerpt. Do not attach joining codes, bearer tokens, private keys,
or personal files.
