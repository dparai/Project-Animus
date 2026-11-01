<#
.SYNOPSIS
  Copy the newest Animus backup from the VM to a local folder (run it yourself on your PC).

.EXAMPLE
  .\scripts\pull-backup.ps1 -Dest E:\animus-backups -KeyPath C:\path\to\key -HostName vm.example.org

  Needs the Windows OpenSSH client (ssh, scp). Prints file names only, never any content.
  The backup folder on the VM is private to the animus user, so the newest file is copied to a
  temporary owner-only file with sudo, fetched with scp, then removed again.
#>
param(
    [Parameter(Mandatory = $true)][string]$Dest,
    [Parameter(Mandatory = $true)][string]$KeyPath,
    [Parameter(Mandatory = $true)][string]$HostName,
    [string]$UserName = 'ubuntu',
    [string]$RemoteDir = '/opt/animus/backups'
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $KeyPath)) { throw 'Key file not found.' }
if (-not (Test-Path -LiteralPath $Dest -PathType Container)) { throw 'Destination folder not found.' }
if ($HostName -notmatch '^[A-Za-z0-9.\-]+$') { throw 'Host name has unexpected characters.' }
if ($UserName -notmatch '^[a-z_][a-z0-9_\-]*$') { throw 'User name has unexpected characters.' }
if ($RemoteDir -notmatch '^/[A-Za-z0-9_./\-]+$') { throw 'Remote folder has unexpected characters.' }

$target = "$UserName@$HostName"
$sshOpts = @('-i', $KeyPath, '-o', 'BatchMode=yes')

$listing = & ssh @sshOpts $target "sudo -u animus ls -1 $RemoteDir"
if ($LASTEXITCODE -ne 0) { throw 'Could not list backups on the VM.' }
$name = ($listing | Where-Object { $_ -match '^animus-\d{8}-\d{6}\.db$' } | Sort-Object | Select-Object -Last 1)
if (-not $name) { throw 'No backup found on the VM.' }

$tmp = (& ssh @sshOpts $target 'mktemp /tmp/animus-pull.XXXXXXXXXX' | Select-Object -First 1)
if ($LASTEXITCODE -ne 0 -or $tmp -notmatch '^/tmp/animus-pull\.[A-Za-z0-9]+$') { throw 'Could not create a temporary file on the VM.' }
& ssh @sshOpts $target "sudo cp $RemoteDir/$name $tmp && chmod 600 $tmp"
if ($LASTEXITCODE -ne 0) { & ssh @sshOpts $target "rm -f $tmp" | Out-Null; throw 'Could not stage the backup.' }
try {
    & scp @sshOpts "${target}:$tmp" (Join-Path $Dest $name)
    if ($LASTEXITCODE -ne 0) { throw 'Download failed.' }
} finally {
    & ssh @sshOpts $target "rm -f $tmp" | Out-Null
}

$local = Join-Path $Dest $name
if ((Get-Item -LiteralPath $local).Length -le 0) { throw 'Downloaded file is empty.' }
Write-Output "Saved $name"
