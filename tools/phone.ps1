param(
    [ValidateSet('devices', 'install', 'open', 'forward', 'remove-forward')]
    [string]$Action = 'devices',
    [string]$Serial = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$adbTool = Join-Path $projectRoot '.tools\android-windows\platform-tools\adb.exe'
if (-not (Test-Path -LiteralPath $adbTool)) {
    throw 'Run python3 tools/bootstrap_android.py --windows-adb in WSL first.'
}
if ($Action -eq 'devices') { & $adbTool devices -l; exit $LASTEXITCODE }
$inventory = & $adbTool devices
if ($LASTEXITCODE -ne 0) { throw 'Could not list USB devices.' }
$ready = @($inventory | ForEach-Object { if ($_ -match '^(\S+)\s+device(?:\s|$)') { $Matches[1] } })
if ($Serial -eq '') {
    if ($ready.Count -ne 1) { throw 'Connect exactly one authorized phone, or pass -Serial explicitly.' }
    $Serial = $ready[0]
}
if ($ready -notcontains $Serial) { throw 'The selected phone is not authorized. Confirm USB debugging on the phone.' }
switch ($Action) {
    'install' {
        $apk = Join-Path $projectRoot 'dist\v360-usb-bridge-0.1.0.apk'
        if (-not (Test-Path -LiteralPath $apk)) { throw 'Build the helper APK first.' }
        & $adbTool -s $Serial install -r $apk
    }
    'open' { & $adbTool -s $Serial shell am start -n 'org.v360web.bridge/.MainActivity' }
    'forward' {
        $existing = @(& $adbTool forward --list)
        if ($existing -contains "$Serial tcp:8766 tcp:8766") { Write-Output 'Forwarding already configured.'; exit 0 }
        & $adbTool -s $Serial forward --no-rebind 'tcp:8766' 'tcp:8766'
    }
    'remove-forward' {
        $existing = @(& $adbTool forward --list)
        if ($existing -notcontains "$Serial tcp:8766 tcp:8766") { throw 'No matching owned forward to remove.' }
        & $adbTool -s $Serial forward --remove 'tcp:8766'
    }
}
if ($LASTEXITCODE -ne 0) { throw 'ADB operation failed.' }
