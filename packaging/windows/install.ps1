[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\ShareSpace'),
    [string]$StartMenuDirectory = (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'),
    [string]$DesktopDirectory = [Environment]::GetFolderPath('Desktop'),
    [switch]$CreateDesktopShortcut
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'app'
$executableName = 'ShareSpace.exe'
$markerName = '.sharespace-install'

function Get-SafeInstallRoot([string]$Path) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($resolved)
    if ($resolved.TrimEnd('\') -eq $root.TrimEnd('\') -or
        [System.IO.Path]::GetFileName($resolved) -ne 'ShareSpace') {
        throw 'InstallRoot must be a ShareSpace directory below a drive root.'
    }
    return $resolved
}

if (-not (Test-Path -LiteralPath (Join-Path $source $executableName) -PathType Leaf)) {
    throw 'The package is incomplete. Extract the full ShareSpace distribution before installing.'
}

$target = Get-SafeInstallRoot $InstallRoot
$parent = Split-Path -Parent $target
$stage = Join-Path $parent ".ShareSpace-stage-$PID"
$backup = Join-Path $parent ".ShareSpace-backup-$PID"
if ((Test-Path -LiteralPath $target) -and
    -not (Test-Path -LiteralPath (Join-Path $target $markerName) -PathType Leaf)) {
    throw 'The target directory already exists and is not managed by the ShareSpace installer.'
}
if ((Test-Path -LiteralPath $stage) -or (Test-Path -LiteralPath $backup)) {
    throw 'A previous ShareSpace install operation left a staging directory. Review it before retrying.'
}

New-Item -ItemType Directory -Path $stage -Force | Out-Null
try {
    Copy-Item -Path (Join-Path $source '*') -Destination $stage -Recurse -Force
    Set-Content -LiteralPath (Join-Path $stage $markerName) -Value 'ShareSpace 1.0.0' -Encoding ascii
    if (Test-Path -LiteralPath $target) {
        Move-Item -LiteralPath $target -Destination $backup
    }
    try {
        Move-Item -LiteralPath $stage -Destination $target
    }
    catch {
        if ((Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $target)) {
            Move-Item -LiteralPath $backup -Destination $target
        }
        throw
    }
    if (Test-Path -LiteralPath $backup) {
        Remove-Item -LiteralPath $backup -Recurse -Force
    }

    New-Item -ItemType Directory -Path $StartMenuDirectory -Force | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $shortcutPath = Join-Path $StartMenuDirectory 'ShareSpace.lnk'
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $target $executableName
    $shortcut.WorkingDirectory = $target
    $shortcut.IconLocation = "$(Join-Path $target $executableName),0"
    $shortcut.Description = 'ShareSpace local file sharing'
    $shortcut.Save()

    if ($CreateDesktopShortcut) {
        New-Item -ItemType Directory -Path $DesktopDirectory -Force | Out-Null
        $desktopLink = $shell.CreateShortcut((Join-Path $DesktopDirectory 'ShareSpace.lnk'))
        $desktopLink.TargetPath = Join-Path $target $executableName
        $desktopLink.WorkingDirectory = $target
        $desktopLink.IconLocation = "$(Join-Path $target $executableName),0"
        $desktopLink.Description = 'ShareSpace local file sharing'
        $desktopLink.Save()
    }
    Write-Output "ShareSpace 1.0.0 installed at $target"
}
finally {
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
