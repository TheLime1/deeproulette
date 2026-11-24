param(
    [Parameter(Mandatory=$true)][string]$RunDirectory,
    [Parameter(Mandatory=$true)][string]$LeaseFile,
    [Parameter(Mandatory=$true)][string]$StopFile,
    [string]$WorkspaceRoot,
    [string]$GameLogPath=(Join-Path $env:USERPROFILE 'AppData\LocalLow\Kefir\ROB RUN\Player.log'),
    [string]$DotnetPath='C:\custom\bins\dotnet\10\dotnet.exe',
    [string]$ObserverPath,
    [string]$ReceiverPath,
    [string]$GuidQueryPath,
    [string]$FlushHelperPath,
    [ValidateRange(0,3600)][int]$OneShotDuration=0
)
# Owned filtered real-time capture; requires inherited Administrator rights.
# This script never prompts/elevates; the interactive app starts its capture host.
# Uses one filtered RealtimeLocal session, no disk-backed ETL and no capture cycles.
# OneShotDuration checks total elapsed lifetime between operations, including startup.
# Synchronous CIM calls may exceed it. Zero uses lease/StopFile.
$ErrorActionPreference='Stop'
if(-not $WorkspaceRoot){$WorkspaceRoot=Join-Path $PSScriptRoot '..\..\..'}
$WorkspaceRoot=[IO.Path]::GetFullPath($WorkspaceRoot)
$privateRoot=Join-Path $WorkspaceRoot 'work'
function PrivatePath([string]$Path) {
    $full=[IO.Path]::GetFullPath($Path)
    if(-not $full.StartsWith($privateRoot.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Driver paths must remain within this workspace work directory.'}
    $full
}
$RunDirectory=PrivatePath $RunDirectory;$LeaseFile=PrivatePath $LeaseFile;$StopFile=PrivatePath $StopFile
if(-not $ObserverPath){$ObserverPath=Join-Path $WorkspaceRoot 'outputs\roulette-qa-investigation\Run-Network-Observer.ps1'}
if(-not $ReceiverPath){$ReceiverPath=Join-Path $WorkspaceRoot 'outputs\roulette-qa-investigation\network-observer\receiver\LiveNdisFlex.dll'}
if(-not $GuidQueryPath){$GuidQueryPath=Join-Path $WorkspaceRoot 'outputs\roulette-qa-investigation\network-observer\guid-query\QueryGuidSession.dll'}
if(-not $FlushHelperPath){$FlushHelperPath=Join-Path $WorkspaceRoot 'outputs\roulette-qa-investigation\network-observer\flush-own-session\FlushOwnSession.dll'}
$gameLog=$GameLogPath
$netsh=Join-Path $env:SystemRoot 'System32\netsh.exe'
$metadataShell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
New-Item -ItemType Directory -Force -Path $RunDirectory | Out-Null
$statePath=Join-Path $RunDirectory 'driver-state.json';$logPath=Join-Path $RunDirectory 'driver-log.jsonl'
$utf8=[Text.UTF8Encoding]::new($false)
$script:runtime=[Diagnostics.Stopwatch]::StartNew()
$script:driverStartUtc=[Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime()
$script:lastLeaseUtc=$null;$script:lastLeasePid=0;$script:stopReason=$null
$script:segment=$null;$script:failure=$null
$script:state=[ordered]@{phase='starting';driverPid=$PID;consumerPid=0;flushPid=0;backend='netevent-realtime-local';pcapng=$null;endpoint=$null;localAddress=$null;localPorts=@();segmentId=$null;error=$null;traceBufferSizeKb=4;maxNumberOfBuffers=64;flushIntervalMs=10}

function Mark {[ordered]@{utc=[DateTime]::UtcNow.ToString('o');qpc=[Diagnostics.Stopwatch]::GetTimestamp();qpcFrequency=[Diagnostics.Stopwatch]::Frequency}}
function WriteState {
    $script:state.updated=Mark
    $temp=Join-Path $RunDirectory ('driver-state.'+[Guid]::NewGuid().ToString('N')+'.tmp')
    [IO.File]::WriteAllText($temp,($script:state | ConvertTo-Json -Depth 12 -Compress),$utf8)
    try {
        for($attempt=0;;$attempt++) {
            try{Move-Item -LiteralPath $temp -Destination $statePath -Force;break}
            catch{if($attempt -ge 99){throw};Start-Sleep -Milliseconds 5}
        }
    } finally{if(Test-Path -LiteralPath $temp){Remove-Item -LiteralPath $temp -Force}}
}
function Log([string]$Event,$Details=$null) {
    $row=[ordered]@{clock=Mark;event=$Event;driverPid=$PID;segmentId=$script:state.segmentId;details=$Details}
    [IO.File]::AppendAllText($logPath,($row | ConvertTo-Json -Depth 12 -Compress)+[Environment]::NewLine,$utf8)
}
function CheckLease {
    try {
        $lease=Get-Content -LiteralPath $LeaseFile -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        $hostId=[int]$lease.pid;$stamp=[DateTimeOffset]::Parse([string]$lease.utc,[Globalization.CultureInfo]::InvariantCulture)
        $age=([DateTimeOffset]::UtcNow-$stamp.ToUniversalTime()).TotalSeconds
        if($hostId -gt 0 -and $age -ge -2 -and $age -le 15) {
            $hostProcess=Get-Process -Id $hostId -ErrorAction Stop
            if($hostProcess.ProcessName -match '^python(?:w|\d+(?:\.\d+)*)?$') {
                if($null -eq $script:lastLeaseUtc -or $stamp -ge $script:lastLeaseUtc){$script:lastLeaseUtc=$stamp.ToUniversalTime();$script:lastLeasePid=$hostId}
            }
        }
    } catch{}
    if($null -eq $script:lastLeaseUtc){return $false}
    return (([DateTimeOffset]::UtcNow-$script:lastLeaseUtc).TotalSeconds -le 15)
}
function ShouldStop {
    if(Test-Path -LiteralPath $StopFile){$script:stopReason='stop_file';return $true}
    if($OneShotDuration -gt 0 -and $script:runtime.Elapsed.TotalSeconds -ge $OneShotDuration){$script:stopReason='one_shot_duration';return $true}
    if(-not (CheckLease)){$script:stopReason='lease_expired_or_invalid';return $true}
    return $false
}
function CheckLifetime {if(ShouldStop){throw [OperationCanceledException]::new('Driver lifetime ended.')}}
function QuoteArg([string]$Value) {
    $builder=[Text.StringBuilder]::new();[void]$builder.Append('"');$slashes=0
    foreach($character in $Value.ToCharArray()) {
        if($character -eq '\'){$slashes++;continue}
        if($character -eq '"'){[void]$builder.Append(('\'*(2*$slashes+1)));[void]$builder.Append('"')}
        else{if($slashes){[void]$builder.Append(('\'*$slashes))};[void]$builder.Append($character)}
        $slashes=0
    }
    if($slashes){[void]$builder.Append(('\'*(2*$slashes)))}
    [void]$builder.Append('"');$builder.ToString()
}
function LaunchOwned([string]$Executable,[string[]]$Arguments) {
    $info=[Diagnostics.ProcessStartInfo]::new();$info.FileName=$Executable
    $info.Arguments=(($Arguments | ForEach-Object{QuoteArg ([string]$_)}) -join ' ')
    $info.WorkingDirectory=$RunDirectory;$info.UseShellExecute=$false;$info.CreateNoWindow=$true
    $info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
    $process=[Diagnostics.Process]::new();$process.StartInfo=$info
    if(-not $process.Start()){throw 'An owned helper process could not be started.'}
    [pscustomobject]@{process=$process;stdout=$process.StandardOutput.ReadToEndAsync();stderr=$process.StandardError.ReadToEndAsync();spawnedPid=$process.Id}
}
function KillOwned($Child) {
    if($null -eq $Child){return}
    if(-not $Child.process.HasExited) {
        if($Child.process.Id -ne $Child.spawnedPid){throw 'Owned child identity changed; refusing process termination.'}
        $Child.process.Kill();[void]$Child.process.WaitForExit(2000)
    }
}
function InvokeOwned([string]$Executable,[string[]]$Arguments,[int]$TimeoutSeconds=10,[bool]$HonorLease=$true) {
    $child=LaunchOwned $Executable $Arguments;$timer=[Diagnostics.Stopwatch]::StartNew()
    try {
        while(-not $child.process.WaitForExit(100)) {
            if($HonorLease){CheckLifetime}
            if($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds){throw ('Owned helper timed out: '+[IO.Path]::GetFileName($Executable))}
        }
        [pscustomobject]@{exitCode=$child.process.ExitCode;stdout=$child.stdout.GetAwaiter().GetResult();stderr=$child.stderr.GetAwaiter().GetResult()}
    } finally{KillOwned $child;$child.process.Dispose()}
}
function LatestBattle {
    $entry=Select-String -LiteralPath $gameLog -Pattern 'ConnectUpdater\.PeerConnect complete id=battle address=(\d+\.\d+\.\d+\.\d+):(\d+)' | Select-Object -Last 1
    if($null -eq $entry){throw 'No completed battle connection is present in the game log.'}
    [pscustomobject]@{address=$entry.Matches[0].Groups[1].Value;port=[int]$entry.Matches[0].Groups[2].Value;line=[int]$entry.LineNumber}
}
function CimValue($Instance,[string]$Name) {
    if($null -eq $Instance){throw 'A required fresh CIM instance is missing.'}
    $property=$Instance.PSBase.CimInstanceProperties[$Name]
    if($null -eq $property){throw ('A required raw CIM property is missing: '+$Name)}
    # Do not use formatted/adapted properties (for example, TraceBufferSize can display as "4 KB").
    $property.Value
}
function ParsedGuid($Value) {
    $parsed=[Guid]::Empty
    if(-not [Guid]::TryParse([string]$Value,[ref]$parsed) -or $parsed -eq [Guid]::Empty){throw 'A nonempty fresh CIM GUID is required.'}
    $parsed
}
function FreshOwnSession([bool]$AllowMissing=$false) {
    $current=$script:segment
    if($null -eq $current -or $null -eq $current.guid){throw 'No fresh owned CIM GUID is available; refusing session changes.'}
    $items=@(Get-NetEventSession -Name $current.name -ErrorAction Stop)
    if($AllowMissing -and $items.Count -eq 0){return $null}
    if($items.Count -ne 1){throw 'The owned CIM session is missing or ambiguous; no other session was selected.'}
    $rawName=[string](CimValue $items[0] 'Name');$rawGuid=ParsedGuid (CimValue $items[0] 'Guid')
    if(-not [string]::Equals($rawName,$current.name,[StringComparison]::Ordinal) -or $rawGuid -ne $current.guid){throw 'Fresh CIM name/GUID changed; refusing to stop or remove that session.'}
    $items[0]
}
function ValidateSession($Session) {
    $current=$script:segment
    if([string](CimValue $Session 'Name') -cne $current.name -or (ParsedGuid (CimValue $Session 'Guid')) -ne $current.guid){throw 'Fresh session identity did not validate.'}
    if([int](CimValue $Session 'CaptureMode') -ne 2 -or [uint32](CimValue $Session 'TraceBufferSize') -ne 4 -or [int](CimValue $Session 'MaxNumberOfBuffers') -ne 64){throw 'Raw RealtimeLocal/4KB/64-buffer session configuration did not validate.'}
    if(-not [string]::IsNullOrWhiteSpace([string](CimValue $Session 'LocalFilePath'))){throw 'RealtimeLocal candidate unexpectedly has a disk-backed local file.'}
}
function ValidateProvider {
    $current=$script:segment;$providers=@(Get-NetEventPacketCaptureProvider -SessionName $current.name -ErrorAction Stop)
    if($providers.Count -ne 1){throw 'Exactly one owned packet-capture provider is required.'}
    $provider=$providers[0]
    if([string](CimValue $provider 'SessionName') -cne $current.name -or (ParsedGuid (CimValue $provider 'SessionGuid')) -ne $current.guid){throw 'Packet provider is not associated with the fresh owned session.'}
    if((ParsedGuid (CimValue $provider 'Guid')) -ne [Guid]'2ed6006e-4729-4609-b423-3ee7bcd678ef'){throw 'The owned provider is not the expected normal NDIS packet-capture provider.'}
    $ips=@(CimValue $provider 'IPAddresses');$protocols=@(CimValue $provider 'IPProtocols');$types=@(CimValue $provider 'EtherType')
    if($ips.Count -ne 1 -or [string]$ips[0] -cne [string]$script:state.endpoint.address -or $protocols.Count -ne 1 -or [int]$protocols[0] -ne 17 -or $types.Count -ne 1 -or [int]$types[0] -ne 2048){throw 'Exact source-side first-party IPv4/UDP filters did not validate; capture was not widened.'}
    if([int](CimValue $provider 'CaptureType') -ne 0 -or -not [bool](CimValue $provider 'MultiLayer') -or [uint16](CimValue $provider 'TruncationLength') -ne 0){throw 'Physical/multilayer/full-packet capture configuration did not validate.'}
}
function RefuseExistingCapture {
    $status=InvokeOwned $netsh @('trace','show','status')
    if($status.stdout.Trim() -ne 'There is no trace session currently in progress.'){throw 'Refusing an existing or unknown netsh trace; it was not modified.'}
    if(@(Get-NetEventSession -ErrorAction Stop).Count -ne 0){throw 'Refusing existing NetEvent sessions/configurations; none were modified.'}
}
function FinishOwnedChild($Child,[string]$Label,[string]$SegmentId) {
    if($null -eq $Child){return}
    try {
        if(-not $Child.process.WaitForExit(2000)){KillOwned $Child;Log ('owned_'+$Label+'_terminated') @{pid=$Child.spawnedPid}}
        $out=$Child.stdout.GetAwaiter().GetResult();$err=$Child.stderr.GetAwaiter().GetResult()
        [IO.File]::WriteAllText((Join-Path $RunDirectory ($Label+'-'+$SegmentId+'.jsonl')),$out,$utf8)
        if($err){[IO.File]::WriteAllText((Join-Path $RunDirectory ($Label+'-'+$SegmentId+'.stderr.txt')),$err,$utf8)}
    } finally{$Child.process.Dispose()}
}
function StopSegment {
    $current=$script:segment;if($null -eq $current){return}
    $cleanupErrors=[Collections.Generic.List[string]]::new()
    # Stop only this segment's flush helper before changing the session it flushes.
    try {
        [IO.File]::WriteAllText($current.flushStop,'stop'+[Environment]::NewLine,$utf8)
    } catch{$cleanupErrors.Add($_.Exception.Message)}
    # Even a failed stop-file write must not skip our owned child cleanup.
    try{FinishOwnedChild $current.flushChild 'flush' $current.id}
    catch{$cleanupErrors.Add($_.Exception.Message)}
    try {
        if($null -ne $current.guid) {
            $fresh=FreshOwnSession $true
            if($null -ne $fresh) {
                $status=[int](CimValue $fresh 'SessionStatus')
                if($status -ne 1){Stop-NetEventSession -InputObject $fresh -Confirm:$false -ErrorAction Stop}
                # Obtain a new instance after stop and re-check raw identity before deletion.
                $fresh=FreshOwnSession $true
                if($null -ne $fresh) {
                    if([int](CimValue $fresh 'SessionStatus') -ne 1){throw 'Owned session did not reach NotRunning; it was not removed.'}
                    Remove-NetEventSession -InputObject $fresh -Confirm:$false -ErrorAction Stop
                    Log 'owned_netevent_session_removed' @{name=$current.name;guid=$current.guid.ToString('D')}
                }
            } else{Log 'owned_netevent_session_already_absent'}
        } elseif($current.createAttempted){$cleanupErrors.Add('Session creation did not yield a verified fresh GUID; no unverified CIM session was stopped or removed.')}
    } catch{$cleanupErrors.Add($_.Exception.Message)}
    finally {
        try{FinishOwnedChild $current.child 'receiver' $current.id}catch{$cleanupErrors.Add($_.Exception.Message)}
        $script:segment=$null;$script:state.consumerPid=0;$script:state.flushPid=0
    }
    if($cleanupErrors.Count){throw ($cleanupErrors -join ' | ')}
}
function WaitFlushReady {
    $current=$script:segment;$timer=[Diagnostics.Stopwatch]::StartNew()
    while(-not (Test-Path -LiteralPath $current.flushReady)) {
        if($current.flushChild.process.HasExited){throw 'Owned flush helper exited before producing its ready record.'}
        if($timer.Elapsed.TotalSeconds -ge 5){throw 'Owned flush helper did not produce a ready record within five seconds.'}
        CheckLifetime;Start-Sleep -Milliseconds 50
    }
    $ready=Get-Content -LiteralPath $current.flushReady -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    $ownerStart=[DateTimeOffset]::Parse([string]$ready.ownerStartUtc,[Globalization.CultureInfo]::InvariantCulture).UtcDateTime
    $readyMode=[Convert]::ToUInt32(([string]$ready.initial.modeHex -replace '^0x',''),16)
    if($ready.quiet -ne $true){throw 'Owned flush helper did not confirm bounded quiet output.'}
    if($ready.kind -cne 'flush_ready' -or (ParsedGuid $ready.expectedGuid) -ne $current.guid -or (ParsedGuid $ready.initial.guid) -ne $current.guid -or -not [string]::Equals([string]$ready.session,$current.logger,[StringComparison]::Ordinal) -or [int]$ready.helperPid -ne $current.flushChild.spawnedPid -or [int]$ready.ownerPid -ne $PID -or $ownerStart.Ticks -ne $script:driverStartUtc.Ticks -or [int]$ready.intervalMs -ne 10 -or [int]$ready.maxSeconds -ne 0 -or -not [string]::Equals([IO.Path]::GetFullPath([string]$ready.stopFile),$current.flushStop,[StringComparison]::OrdinalIgnoreCase) -or ($readyMode -band 0x100) -eq 0 -or [uint32]$ready.initial.bufferSizeKb -ne 4){throw 'Flush ready-file GUID/logger/PID/owner/interval/stop-path configuration did not validate.'}
    if($current.flushChild.process.HasExited){throw 'Owned flush helper exited during ready validation.'}
    $script:state.flushReady=Mark
}
function StartSegment {
    CheckLifetime;$script:state.phase='starting';$script:state.error=$null;WriteState
    RefuseExistingCapture
    $discoveryResult=InvokeOwned $metadataShell @('-NoProfile','-File',$ObserverPath,'-ValidateOnly','-OutputDirectory',$RunDirectory,'-DotnetPath',$DotnetPath,'-GameLogPath',$gameLog) 10 $true
    if($discoveryResult.exitCode -ne 0){throw 'The existing observer validation helper did not complete successfully.'}
    $discovery=$discoveryResult.stdout | ConvertFrom-Json -ErrorAction Stop
    $address=[Net.IPAddress]::Parse([string]$discovery.endpoint.address);$local=[Net.IPAddress]::Parse([string]$discovery.localAddress)
    $serverPort=[int]$discovery.endpoint.port;$ports=@($discovery.gamePorts.exclusivePorts | ForEach-Object{[int]$_} | Sort-Object -Unique)
    if($address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or $local.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or $serverPort -lt 1 -or $serverPort -gt 65535 -or $ports.Count -eq 0 -or @($ports | Where-Object{$_ -lt 1 -or $_ -gt 65535}).Count){throw 'Dynamic endpoint/local-address/exclusive-port discovery did not validate.'}
    $latest=LatestBattle
    if($latest.address -ne $address.ToString() -or $latest.port -ne $serverPort){throw 'Battle endpoint changed during discovery; no session was created.'}
    $id=[DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff')+'-'+[Guid]::NewGuid().ToString('N').Substring(0,8)
    $name='RobrunLowLatency-'+$id;$pcap=Join-Path $RunDirectory ('stream-'+$id+'.pcapng')
    $captureManifest=$pcap+'.manifest.json'
    $flushStop=Join-Path $RunDirectory ('flush-stop-'+$id+'.signal');$flushReady=Join-Path $RunDirectory ('flush-ready-'+$id+'.json')
    foreach($path in @($pcap,$captureManifest,$flushStop,$flushReady)){if(Test-Path -LiteralPath $path){throw 'A selected private segment output already exists.'}}
    $script:segment=[pscustomobject]@{id=$id;name=$name;guid=$null;logger=$null;pcapng=$pcap;child=$null;flushChild=$null;flushStop=$flushStop;flushReady=$flushReady;createAttempted=$false;peerLine=$latest.line;gamePid=[int]$discovery.gamePorts.pid}
    $script:state.segmentId=$id;$script:state.pcapng=$pcap;$script:state.sessionName=$name;$script:state.sessionGuid=$null;$script:state.session=$null
    $script:state.captureManifest=$captureManifest;$script:state.chunkBytes=16777216;$script:state.retainChunks=4
    $script:state.endpoint=[ordered]@{address=$address.ToString();port=$serverPort};$script:state.localAddress=$local.ToString();$script:state.localPorts=$ports
    $script:state.startRequested=Mark;WriteState
    CheckLifetime;RefuseExistingCapture;$script:segment.createAttempted=$true
    $created=New-NetEventSession -Name $name -CaptureMode RealtimeLocal -TraceBufferSize 4 -MaxNumberOfBuffers 64 -Confirm:$false -ErrorAction Stop
    $createdGuid=ParsedGuid (CimValue $created 'Guid')
    if([string](CimValue $created 'Name') -cne $name){throw 'Created CIM name does not match the unique requested owner name.'}
    $script:segment.guid=$createdGuid;$script:state.sessionGuid=$createdGuid.ToString('D')
    ValidateSession (FreshOwnSession)
    CheckLifetime
    Add-NetEventPacketCaptureProvider -SessionName $name -CaptureType Physical -MultiLayer $true -EtherType ([uint16]2048) -IpAddresses $address.ToString() -IpProtocols ([byte]17) -TruncationLength ([uint16]0) -Confirm:$false -ErrorAction Stop | Out-Null
    ValidateProvider
    CheckLifetime
    $fresh=FreshOwnSession;ValidateSession $fresh
    Start-NetEventSession -Name $name -Confirm:$false -ErrorAction Stop
    $fresh=FreshOwnSession;ValidateSession $fresh;ValidateProvider
    if([int](CimValue $fresh 'SessionStatus') -ne 0){throw 'Fresh owned NetEvent session is not Running.'}
    $script:state.traceStarted=Mark
    $queryResult=InvokeOwned $DotnetPath @($GuidQueryPath,'--expected-guid',$createdGuid.ToString('D')) 10 $true
    $query=$queryResult.stdout | ConvertFrom-Json -ErrorAction Stop;$guidMatches=@($query.matches)
    if($queryResult.exitCode -ne 0 -or $query.kind -cne 'guid_query' -or -not $query.uniqueMatch -or (ParsedGuid $query.expectedCimGuid) -ne $createdGuid -or $guidMatches.Count -ne 1){throw 'Exact fresh CIM GUID query did not yield one owned API logger.'}
    $match=$guidMatches[0]
    if(-not $match.matchWnodeGuid -or (ParsedGuid $match.wnodeGuid) -ne $createdGuid -or -not $match.realtime -or [uint32]$match.bufferSizeKb -ne 4 -or [uint32]$match.MaximumBuffers -ne 64 -or -not [string]::IsNullOrWhiteSpace([string]$match.logFileName) -or [string]::IsNullOrWhiteSpace([string]$match.loggerName)){throw 'Exact WNODE GUID/realtime/4KB/64-buffer/no-file logger invariants did not validate.'}
    $script:segment.logger=[string]$match.loggerName;$script:state.session=[string]$match.loggerName;$script:state.guidVerified=Mark
    $child=LaunchOwned $DotnetPath @($ReceiverPath,'--session',[string]$match.loggerName,'--pcapng',$pcap,'--server-ip',$address.ToString(),'--server-port',[string]$serverPort,'--local-ip',$local.ToString(),'--local-ports',($ports -join ','),'--chunk-bytes','16777216','--retain-chunks','4','--quiet')
    $script:segment.child=$child;$script:state.consumerPid=$child.spawnedPid
    $startup=[Diagnostics.Stopwatch]::StartNew()
    while(-not (Test-Path -LiteralPath $captureManifest)) {
        if($child.process.HasExited){throw 'Owned receiver exited before initializing its packet stream.'}
        if($startup.Elapsed.TotalSeconds -ge 5){throw 'Owned receiver did not initialize its packet stream within five seconds.'}
        CheckLifetime;Start-Sleep -Milliseconds 50
    }
    $capture=Get-Content -LiteralPath $captureManifest -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    if($capture.version -ne 1 -or [IO.Path]::GetFullPath([string]$capture.basePath) -cne $pcap -or $capture.generation -lt 1 -or $capture.chunkBytes -ne 16777216 -or $capture.retainChunks -ne 4 -or @($capture.chunks).Count -lt 1 -or @($capture.chunks).Count -gt 4){throw 'Owned receiver did not confirm the bounded capture manifest contract.'}
    $flush=LaunchOwned $DotnetPath @($FlushHelperPath,'--expected-guid',$createdGuid.ToString('D'),'--flush-loop','--interval-ms','10','--max-seconds','0','--owner-pid',[string]$PID,'--stop-file',$flushStop,'--ready-file',$flushReady,'--quiet')
    $script:segment.flushChild=$flush;$script:state.flushPid=$flush.spawnedPid
    WaitFlushReady
    # Re-check fresh raw CIM identity/config after both children attach and before publishing ready.
    ValidateSession (FreshOwnSession);ValidateProvider
    if($child.process.HasExited -or $flush.process.HasExited){throw 'An owned child exited before the segment became ready.'}
    $script:state.phase='ready';$script:state.ready=Mark;$script:state.hostPid=$script:lastLeasePid;WriteState
    Log 'segment_ready' @{backend=$script:state.backend;name=$name;guid=$createdGuid.ToString('D');session=$match.loggerName;consumerPid=$child.spawnedPid;flushPid=$flush.spawnedPid;pcapng=$pcap;ready=$script:state.ready}
}

$mutex=$null;$mutexOwned=$false
try {
    $hash=[Security.Cryptography.SHA256]::Create()
    try{$key=([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($RunDirectory.ToLowerInvariant())))).Replace('-','').Substring(0,24)}finally{$hash.Dispose()}
    $mutex=[Threading.Mutex]::new($false,('Local\RobrunRTProducer-'+$key))
    try{$mutexOwned=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$mutexOwned=$true}
    if(-not $mutexOwned){throw 'A producer already owns this run directory; its state was not overwritten.'}
    WriteState
    $principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Inherited normal Windows Administrator rights are required; no elevation prompt is requested.'}
    foreach($file in @($ObserverPath,$ReceiverPath,$GuidQueryPath,$FlushHelperPath,$DotnetPath,$metadataShell)){if(-not (Test-Path -LiteralPath $file -PathType Leaf)){throw 'A required staged observer/runtime/helper file is missing.'}}
    Import-Module NetEventPacketCapture -ErrorAction Stop
    if(-not (CheckLease)){throw 'A fresh lease from an existing Python host is required before starting capture.'}
    Log 'driver_started' @{backend=$script:state.backend;hostPid=$script:lastLeasePid;oneShotDuration=$OneShotDuration}
    while(-not (ShouldStop)) {
        StartSegment
        while(-not (ShouldStop)) {
            if($script:segment.child.process.HasExited -or $script:segment.flushChild.process.HasExited){throw 'An owned receiver or flush helper exited unexpectedly.'}
            $latest=LatestBattle;$endpoint=$script:state.endpoint
            if($latest.address -ne $endpoint.address -or $latest.port -ne $endpoint.port -or $latest.line -ne $script:segment.peerLine) {
                Log 'battle_connection_changed' @{newEndpoint=@{address=$latest.address;port=$latest.port}}
                StopSegment;$script:state.phase='starting';WriteState;break
            }
            # Fresh raw GUID/name checks prevent operations against a replaced friendly-name session.
            ValidateSession (FreshOwnSession);ValidateProvider
            $game=Get-Process -Id $script:segment.gamePid -ErrorAction Stop
            if($game.ProcessName -ne 'ROB RUN'){throw 'The previously discovered game process no longer matches.'}
            $script:state.hostPid=$script:lastLeasePid;WriteState;Start-Sleep -Seconds 1
        }
    }
} catch [OperationCanceledException]{if($mutexOwned){Log 'driver_lifetime_ended' @{reason=$script:stopReason}}}
catch{$script:failure=$_.Exception.Message;if($mutexOwned){Log 'driver_error' @{message=$script:failure}}}
finally {
    if($mutexOwned) {
        try{StopSegment}catch{if(-not $script:failure){$script:failure=$_.Exception.Message};Log 'cleanup_error' @{message=$_.Exception.Message}}
        $script:state.phase=if($script:failure){'error'}else{'stopped'};$script:state.consumerPid=0;$script:state.flushPid=0
        $script:state.error=$script:failure;$script:state.stopReason=$script:stopReason;$script:state.stopped=Mark
        WriteState;Log 'driver_finished' @{phase=$script:state.phase;reason=$script:stopReason;error=$script:failure}
        $mutex.ReleaseMutex()
    }
    if($mutex){$mutex.Dispose()}
}
if($script:failure){[Console]::Error.WriteLine($script:failure);exit 1}
