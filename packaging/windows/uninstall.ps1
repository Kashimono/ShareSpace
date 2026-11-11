[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\ShareSpace'),
    [string]$StartMenuDirectory = (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'),
    [string]$DesktopDirectory = [Environment]::GetFolderPath('Desktop')
)

$ErrorActionPreference = 'Stop'
$target = [System.IO.Path]::GetFullPath($InstallRoot)
$root = [System.IO.Path]::GetPathRoot($target)
if ($target.TrimEnd('\') -eq $root.TrimEnd('\') -or
    [System.IO.Path]::GetFileName($target) -ne 'ShareSpace') {
    throw 'InstallRoot must be a ShareSpace directory below a drive root.'
}
$marker = Join-Path $target '.sharespace-install'
if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
    Write-Warning 'The ShareSpace installation marker was not found; no program files were removed.'
    return
}

$startLink = Join-Path $StartMenuDirectory 'ShareSpace.lnk'
if (Test-Path -LiteralPath $startLink) {
    Remove-Item -LiteralPath $startLink -Force
}
$desktopLink = Join-Path $DesktopDirectory 'ShareSpace.lnk'
if (Test-Path -LiteralPath $desktopLink) {
    Remove-Item -LiteralPath $desktopLink -Force
}
Remove-Item -LiteralPath $target -Recurse -Force
Write-Output 'ShareSpace program files and shortcuts were removed. Application settings and history were kept.'
