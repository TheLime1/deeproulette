param(
    [ValidateRange(1,60)][int]$Seconds=3,
    [ValidateRange(1,12)][int]$Iterations=1,
    [string]$LocalAddress,
    [string]$OutputDirectory,
    [string]$GameLogPath=(Join-Path $env:USERPROFILE 'AppData\LocalLow\Kefir\ROB RUN\Player.log'),
    [switch]$ValidateOnly,
    [string]$DotnetPath='C:\custom\bins\dotnet\10\dotnet.exe'
)
$ErrorActionPreference='Stop'
$workspace=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$privateRoot=Join-Path $workspace 'work'
$privateOutput=Join-Path $privateRoot 'roulette-qa\private-capture'
if($OutputDirectory){$privateOutput=$OutputDirectory}
$gameLog=$GameLogPath
if(-not $LocalAddress){
    $game=@(Get-Process -Name 'ROB RUN' -ErrorAction Stop)
    if($game.Count -ne 1){throw 'Exactly one running ROB RUN client is required.'}
    $bound=@(Get-NetUDPEndpoint -OwningProcess $game[0].Id | Select-Object -ExpandProperty LocalAddress -Unique | Where-Object {$_ -match '^\d+\.\d+\.\d+\.\d+$' -and $_ -ne '0.0.0.0' -and $_ -notmatch '^(127|169\.254)\.'})
    if($bound.Count -ne 1){throw 'A unique game-bound local IPv4 address was not found. Provide -LocalAddress explicitly.'}
    $LocalAddress=$bound[0]
}
$observer=Join-Path $PSScriptRoot 'network-observer\Invoke-RobrunBoundedCapture.ps1'
$receiver=Join-Path $PSScriptRoot 'network-observer\receiver\LiveNdisFlex.dll'
& $observer -OutputDirectory $privateOutput -AllowedWorkRoot $privateRoot -Seconds $Seconds -Iterations $Iterations -LocalAddress $LocalAddress -GameLogPath $gameLog -ReceiverPath $receiver -DotnetPath $DotnetPath -ValidateOnly:$ValidateOnly
