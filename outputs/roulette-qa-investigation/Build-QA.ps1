param([string]$DotnetPath=$env:ROBRUN_DOTNET,[string]$TraceEventDir=$env:ROBRUN_TRACEEVENT_DIR)
$ErrorActionPreference='Stop'
if(-not $DotnetPath){$DotnetPath='C:\custom\bins\dotnet\10\dotnet.exe'}
if(-not (Test-Path -LiteralPath $DotnetPath)){throw 'Set ROBRUN_DOTNET to an installed .NET 10 SDK dotnet.exe.'}
if(-not $TraceEventDir){$TraceEventDir='C:\custom\bins\spellstorm-trace\bin\Release\net10.0'}
if(-not (Test-Path -LiteralPath (Join-Path $TraceEventDir 'Microsoft.Diagnostics.Tracing.TraceEvent.dll'))){throw 'Set ROBRUN_TRACEEVENT_DIR to your installed TraceEvent assembly directory.'}
foreach($taskProject in @(@('rotating-receiver','LiveNdisFlex','receiver'),@('guid-query','QueryGuidSession','guid-query'),@('flush-own-session','FlushOwnSession','flush-own-session'))){
    $taskSource=Join-Path $PSScriptRoot ('native-source\'+$taskProject[0]+'\'+$taskProject[1]+'.csproj')
    $taskOutput=Join-Path $PSScriptRoot ('network-observer\'+$taskProject[2])
    & $DotnetPath build $taskSource -c Release -o $taskOutput "-p:TraceEventDir=$TraceEventDir"
    if($LASTEXITCODE -ne 0){throw "Could not build $($taskProject[1])"}
}
