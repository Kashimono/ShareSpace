# ShareSpace architecture

The workspace had no prior project plan or source code. Version 0.1 recorded the
starting architecture here; Version 0.2 adds the local-hotspot boundary described
below.

## Shared application

- sharespace.app creates the Qt application, selects the platform adapter, and cleans
  up adapter-owned hotspot resources when the app exits.
- sharespace.ui contains presentation code. The Host Session dialog runs blocking
  OS operations in a Qt worker thread and displays observed status and network data.
- sharespace.platform.base_hotspot defines OS-neutral data types, validation, and the
  hotspot interface.
- sharespace.platform.platform_manager selects Windows or Linux from
  platform.system() and exposes a shared facade.
- sharespace.platform.windows and sharespace.platform.linux contain separate
  operating-system implementations. Shared UI and manager code contain no
  OS-specific commands.

No file-transfer, discovery, participant, or session-code modules are introduced in
this milestone.

## Linux adapter

LinuxHotspotAdapter calls NetworkManager through nmcli. It checks that NetworkManager
is running, lists wireless devices, reads the WIFI-PROPERTIES.AP hardware/driver
capability, and creates a profile with a generated UUID. The profile is memory-only
(connection add save no), has autoconnect disabled, uses AP mode and WPA-PSK, and
uses ipv4.method shared for local IPv4 configuration. It activates and removes only
its own profile by UUID.

If the chosen radio already has an active connection, the UI asks before replacing
its active role. The adapter records that profile UUID and tries to restore it after
stopping, but only if the interface has no other active profile at that time. A
different connection selected by the user while the hotspot is running is left
alone. ipv4.method shared supplies NetworkManager-managed local addressing; required
components such as the dnsmasq plugin must be present.

## Windows adapter

WindowsHotspotAdapter enumerates physical 802.11 adapters with PowerShell and
delegates support and control to WindowsTetheringBackend, which calls
NetworkOperatorTetheringManager. No deprecated netsh wlan hostednetwork commands
are used.

There are material API constraints:

- The API requires wiFiControl in the app package manifest. ShareSpace's current
  editable Python installation is unpackaged, so the adapter reports this limitation
  and will not attempt to create a hotspot.
- The manager is created from an active Windows connection profile. If Windows
  exposes no suitable profile, the adapter reports unsupported. Microsoft describes
  this API as tethering a connection; an offline-only access point with no
  connection profile is not guaranteed.
- Windows 11 version 24H2 (build 26100) adds a per-session SSID/password config.
  Windows 10 and earlier Windows 11 releases use the persistent configuration API;
  ShareSpace saves and restores the prior configuration on normal stop and failed
  start. A process crash can still prevent that restoration.
- The local IP query looks for an active Microsoft Wi-Fi Direct virtual adapter. If
  Windows or a driver exposes a different name, the UI reports that it could not
  identify the address instead of guessing.

The WinRT projections are conditional Windows dependencies. Adding a package
capability and building an MSIX/sparse package is not part of this milestone; actual
Windows hotspot creation remains blocked until that packaging step is implemented.

## Shared configuration and state

SSID and password inputs are validated as printable ASCII to avoid cross-locale
Windows SSID differences. The WPA passphrase is 8–63 characters. Platform adapters
return ACTIVE, STOPPED, or UNKNOWN; a requested operation is not treated as
successful until the OS reports an active hotspot.

Linux errors are returned from nmcli and Windows tethering results/exceptions are
translated to user-facing errors. Credentials are not written to application logs.
The Linux SSID/password are passed as short-lived nmcli process arguments because
nmcli's noninteractive connection profile interface accepts them as properties; the
profile itself is memory-only and removed on stop.

## Primary platform references

- [NetworkManager nmcli reference](https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nmcli.html)
- [NetworkManager IPv4 settings](https://www.networkmanager.dev/docs/api/latest/settings-ipv4.html)
- [Windows tethering manager](https://learn.microsoft.com/en-us/uwp/api/windows.networking.networkoperators.networkoperatortetheringmanager)
- [Windows connection-profile capability requirement](https://learn.microsoft.com/en-us/uwp/api/windows.networking.networkoperators.networkoperatortetheringmanager.createfromconnectionprofile)
- [Windows per-session tethering configuration](https://learn.microsoft.com/en-us/uwp/api/windows.networking.networkoperators.networkoperatortetheringsessionaccesspointconfiguration)
- [PyWinRT installation and namespace packages](https://pywinrt.readthedocs.io/en/latest/installing/)
