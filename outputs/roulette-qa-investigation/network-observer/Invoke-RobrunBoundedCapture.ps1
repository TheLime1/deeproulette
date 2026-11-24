param(
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [ValidateRange(1,60)][int]$Seconds=3,
    [ValidateRange(1,12)][int]$Iterations=1,
    [int]$GameProcessId=0,
    [string]$LocalAddress,
    [string]$GameLogPath=(Join-Path $env:USERPROFILE 'AppData\LocalLow\Kefir\ROB RUN\Player.log'),
    [string]$DotnetPath='C:\custom\bins\dotnet\10\dotnet.exe',
    [string]$ReceiverPath,
    [string]$AllowedWorkRoot,
    [switch]$ValidateOnly
)
# Normal supported netsh controller. Never self-elevates or changes any process protection.
# ValidateOnly performs discovery and argument validation, without trace operations.
$ErrorActionPreference='Stop'
if (-not $ReceiverPath) {$ReceiverPath=Join-Path $PSScriptRoot 'live-ndis-flex\bin\Release\net10.0\LiveNdisFlex.dll'}
function ClockMark { [ordered]@{utc=[DateTime]::UtcNow.ToString('o');qpc=[Diagnostics.Stopwatch]::GetTimestamp();qpcFrequency=[Diagnostics.Stopwatch]::Frequency} }
function BattleEndpoint {
    $match=Select-String -LiteralPath $GameLogPath -Pattern 'ConnectUpdater\.PeerConnect complete id=battle address=(\d+\.\d+\.\d+\.\d+):(\d+)' | Select-Object -Last 1
    if ($null -eq $match) {throw 'No completed first-party battle connection exists in the specified game log.'}
    $address=$match.Matches[0].Groups[1].Value;$port=[int]$match.Matches[0].Groups[2].Value
    $ip=[Net.IPAddress]::Parse($address)
    if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or $port -lt 1 -or $port -gt 65535) {throw 'Invalid battle endpoint.'}
    [ordered]@{address=$address;port=$port}
}
function GamePorts {
    if ($GameProcessId -eq 0) {
        $games=@(Get-Process -Name 'ROB RUN' -ErrorAction Stop)
        if ($games.Count -ne 1) {throw 'Exactly one ROB RUN process is required, or provide GameProcessId.'}
        $gameId=$games[0].Id
    } else {
        $game=Get-Process -Id $GameProcessId -ErrorAction Stop
        if ($game.ProcessName -ne 'ROB RUN') {throw 'The specified process is not ROB RUN.'}
        $gameId=$game.Id
    }
    $endpoints=@(Get-NetUDPEndpoint -ErrorAction Stop)
    $owned=@($endpoints | Where-Object OwningProcess -eq $gameId | Select-Object -ExpandProperty LocalPort -Unique | Sort-Object)
    $otherPorts=@{};foreach($ep in $endpoints) {if($ep.OwningProcess -ne $gameId){$otherPorts[[int]$ep.LocalPort]=$true}}
    $exclusive=@($owned | Where-Object {-not $otherPorts.ContainsKey([int]$_)})
    if ($exclusive.Count -eq 0) {throw 'No exclusively game-owned UDP ports are available; refusing to widen capture.'}
    [ordered]@{pid=$gameId;exclusivePorts=$exclusive;overlappingPortCount=$owned.Count-$exclusive.Count;snapshot=ClockMark}
}
if (-not $AllowedWorkRoot) {$AllowedWorkRoot=Join-Path $PSScriptRoot '..\..'}
$workRoot=[IO.Path]::GetFullPath($AllowedWorkRoot)
$outputRoot=[IO.Path]::GetFullPath($OutputDirectory)
if (-not $outputRoot.StartsWith($workRoot.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) {throw 'OutputDirectory must be within this workspace work directory.'}
$localIp=[Net.IPAddress]::Parse($LocalAddress)
if ($localIp.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {throw 'LocalAddress must be IPv4.'}
if (-not (Test-Path -LiteralPath $DotnetPath -PathType Leaf) -or -not (Test-Path -LiteralPath $ReceiverPath -PathType Leaf)) {throw 'Installed runtime or built receiver is missing.'}
if ($ValidateOnly) {
    [ordered]@{kind='validation';endpoint=BattleEndpoint;gamePorts=GamePorts;localAddress=$LocalAddress;output=$outputRoot;seconds=$Seconds;iterations=$Iterations;clock=ClockMark;traceActionsPerformed=$false} | ConvertTo-Json -Depth 7
    return
}
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {throw 'Normal Windows Administrator approval is required; this script does not self-elevate.'}
$netsh=Join-Path $env:SystemRoot 'System32\netsh.exe'
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
for ($iteration=1;$iteration -le $Iterations;$iteration++) {
    $status=(& $netsh trace show status 2>&1 | Out-String).Trim()
    # This Windows netsh build returns exit code 1 for the documented idle state.
    if ($status -ne 'There is no trace session currently in progress.') {throw 'Refusing to alter an existing or unknown netsh trace session.'}
    $endpoint=BattleEndpoint;$ports=GamePorts
    $tag=[DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff')+'-'+([Guid]::NewGuid().ToString('N').Substring(0,8))
    $etl=Join-Path $outputRoot ('bounded-'+$tag+'.etl')
    $pcap=Join-Path $outputRoot ('bounded-'+$tag+'.pcapng')
    $recordPath=Join-Path $outputRoot ('bounded-'+$tag+'.json')
    if ((Test-Path -LiteralPath $etl) -or (Test-Path -LiteralPath $pcap) -or (Test-Path -LiteralPath $recordPath)) {throw 'A selected output already exists.'}
    $record=[ordered]@{kind='capture';iteration=$iteration;seconds=$Seconds;endpoint=$endpoint;localAddress=$LocalAddress;gamePorts=$ports;maxTraceMb=128;report='disabled';correlation='disabled';perfMerge='no';etl=$etl;pcapng=$pcap;record=$recordPath;started=$false;stopped=$false;error=$null}
    $started=$false
    try {
        $record.startRequested=ClockMark
        $record.startOutput=(& $netsh trace start capture=yes persistent=no report=disabled correlation=disabled perfMerge=no ('tracefile='+$etl) maxsize=128 filemode=circular Ethernet.Type=IPv4 Protocol=UDP ('IPv4.Address='+$endpoint.address) PacketTruncateBytes=65535 CaptureMultiLayer=yes 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {throw $record.startOutput}
        $started=$true;$record.started=$true;$record.startSucceeded=ClockMark
        $record | ConvertTo-Json -Depth 9 | Set-Content -LiteralPath $recordPath -Encoding UTF8
        Start-Sleep -Seconds $Seconds
    } catch {$record.error=$_.Exception.Message}
    finally {
        if ($started) {
            $record.stopRequested=ClockMark
            $ownStatus=(& $netsh trace show status 2>&1 | Out-String).Trim()
            if ($LASTEXITCODE -eq 0 -and $ownStatus.IndexOf($etl,[StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $record.stopOutput=(& $netsh trace stop 2>&1 | Out-String).Trim()
                $record.stopped=($LASTEXITCODE -eq 0)
                $record.stopCompleted=ClockMark
                if (-not $record.stopped -and -not $record.error) {$record.error='The owned trace did not stop successfully.'}
            } else {
                $record.error='The current trace no longer matches this controller output; no unknown session was stopped.'
            }
        }
        $record | ConvertTo-Json -Depth 9 | Set-Content -LiteralPath $recordPath -Encoding UTF8
    }
    if ($record.error -or -not $record.stopped) {throw ('Capture failed; inspect '+$recordPath)}
    try {
        $record.replayStarted=ClockMark
        $localCsv=$ports.exclusivePorts -join ','
        $receiverOutput=& $DotnetPath $ReceiverPath --etl $etl --pcapng $pcap --server-ip $endpoint.address --server-port $endpoint.port --local-ip $LocalAddress --local-ports $localCsv --quiet 2>&1
        if ($LASTEXITCODE -ne 0) {throw ('Receiver failed: '+($receiverOutput | Out-String))}
        $record.receiver=($receiverOutput | Out-String | ConvertFrom-Json)
        $record.replayReady=ClockMark
        $record.endpointAfterCapture=BattleEndpoint
        $record.endpointChangedDuringCapture=($endpoint.address -ne $record.endpointAfterCapture.address -or $endpoint.port -ne $record.endpointAfterCapture.port)
        if ($record.receiver.accepted -eq 0) {$record.error='No exact endpoint/member-port packets were accepted; refusing a success claim.'}
        if ($record.endpointChangedDuringCapture) {$record.error='Battle endpoint changed during this batch; inspect coverage before using it.'}
    } catch {$record.error=$_.Exception.Message}
    finally {$record | ConvertTo-Json -Depth 9 | Set-Content -LiteralPath $recordPath -Encoding UTF8}
    [ordered]@{kind='batch_ready';iteration=$iteration;record=$recordPath;pcapng=$pcap;ready=$record.replayReady;receiver=$record.receiver;error=$record.error} | ConvertTo-Json -Depth 8 -Compress
    if ($record.error) {throw ('Batch could not be validated; inspect '+$recordPath)}
}
