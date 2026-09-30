<#
.SYNOPSIS
  Brings up everything StepRealm needs to run, in one command.

.DESCRIPTION
  The app on the phone is already built, so starting it is not a build step --
  it is four separate things that have to be alive at once, and forgetting the
  fourth one is what produces the "cannot reach server" screen:

    1. Docker Desktop, and the Postgres + Redis containers from it
    2. uvicorn, the API on :8000 (the containers are only the datastore)
    3. adb running
    4. `adb reverse tcp:8000`, the tunnel that makes your laptop's :8000
       reachable from the phone

  Step 4 is the one that surprises people. The installed APK is built with
  EXPO_PUBLIC_API_URL=http://localhost:8000, and on Android `localhost` means
  the phone itself, so the tunnel over USB is the only route to this machine.
  The tunnel lives inside the adb daemon, which restarts on its own and takes
  the tunnels with it -- this script re-establishes them every run rather than
  making you remember.

.PARAMETER Stop
  Stop the API and remove the containers. Leaves Docker Desktop itself alone.

.EXAMPLE
  ./start.ps1
  ./start.ps1 -Stop
#>
[CmdletBinding()]
param(
  [switch]$Stop
)

$ErrorActionPreference = 'Stop'

$Root       = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Join-Path $Root 'backend'
$Compose    = Join-Path $BackendDir 'docker-compose.yml'
$Python     = Join-Path $BackendDir '.venv\Scripts\python.exe'
$DockerApp  = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$Adb        = Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe'

$ApiPort   = 8000
$MetroPort = 8081
$RunDir    = Join-Path $env:TEMP 'steprealm'
$ApiLog    = Join-Path $RunDir 'api.log'
$ApiErr    = Join-Path $RunDir 'api.err.log'
$ApiPid    = Join-Path $RunDir 'api.pid'

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    ok  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    !!  $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "    XX  $msg" -ForegroundColor Red; exit 1 }

function Test-Api {
  # 127.0.0.1 rather than "localhost": on Windows "localhost" can resolve to ::1
  # first, and uvicorn binds IPv4 only, so the name would fail on a machine
  # where the address works.
  try {
    $r = Invoke-RestMethod "http://127.0.0.1:$ApiPort/health" -TimeoutSec 2
    return $r.status -eq 'ok'
  } catch { return $false }
}

function Test-Docker {
  docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
  return ($LASTEXITCODE -eq 0)
}

function Get-ApiPids {
  <#
    Every process that could be serving the API.

    Matching on names alone is not enough, and the way it fails is worth
    spelling out. A --reload reloader forks a worker whose command line is
    just `multiprocessing.spawn_main` -- it never mentions uvicorn. Kill the
    reloader and the worker survives as an orphan, still holding the port, and
    now every simple way of finding it comes up empty:

      * the pid file names the reloader, which is dead
      * the port owner is reported as the reloader too, also dead
      * a command-line search for "uvicorn" misses the worker entirely

    So the dead ids are kept as seeds and the tree is walked downwards through
    Win32_Process, which still records the parent of an orphan. Only at the end
    are dead ids dropped, so a stale seed can still lead to a live descendant.
  #>
  $wmi  = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
  $seed = @()

  if (Test-Path $ApiPid) { $seed += (Get-Content $ApiPid -ErrorAction SilentlyContinue) }
  Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { $seed += $_.OwningProcess }
  $wmi | Where-Object { $_.CommandLine -like '*uvicorn*app.main:app*' } |
    ForEach-Object { $seed += $_.ProcessId }

  $all      = @($seed)
  $frontier = @($seed)
  while ($frontier.Count -gt 0) {
    $children = @($wmi | Where-Object { $frontier -contains $_.ParentProcessId } |
                    ForEach-Object { $_.ProcessId } |
                    Where-Object { $all -notcontains $_ })
    if ($children.Count -eq 0) { break }
    $all      += $children
    $frontier = $children
  }

  $all | Where-Object { $_ -and (Get-Process -Id $_ -ErrorAction SilentlyContinue) } |
    Select-Object -Unique
}

function Stop-Api {
  $killed = $false
  # Twice, because the reloader can be mid-restart and put a fresh child on the
  # port between the kill and the check.
  for ($pass = 1; $pass -le 2; $pass++) {
    foreach ($processId in @(Get-ApiPids)) {
      taskkill /PID $processId /T /F 2>&1 | Out-Null
      $killed = $true
    }
    if (-not (Test-Api)) { break }
    Start-Sleep -Milliseconds 500
  }
  Remove-Item $ApiPid -ErrorAction SilentlyContinue
  # Report on the probe, not on the socket table: a killed `--reload` parent
  # leaves a phantom listener behind that never answers anything.
  return (-not (Test-Api))
}

# ---------------------------------------------------------------------------

if ($Stop) {
  Step 'Stopping the API'
  if (Stop-Api) { Ok 'stopped' } else { Ok 'nothing was running' }

  Step 'Removing containers'
  docker compose -f $Compose down 2>&1 | Out-Null
  Ok 'containers removed (Docker Desktop left running)'
  exit 0
}

New-Item -ItemType Directory -Path $RunDir -Force | Out-Null

# --- 1. Docker --------------------------------------------------------------

Step 'Docker'
if (Test-Docker) {
  Ok 'daemon already up'
} else {
  if (-not (Test-Path $DockerApp)) {
    Die "Docker Desktop not found at $DockerApp -- install it, or run without the database."
  }
  Write-Host '    starting Docker Desktop, this takes 20-60s the first time...'
  Start-Process $DockerApp -WindowStyle Hidden | Out-Null
  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 3
    if (Test-Docker) { break }
  }
  if (-not (Test-Docker)) { Die 'Docker daemon did not come up. Open Docker Desktop and wait for "Engine running", then re-run.' }
  Ok 'daemon up'
}

Step 'Containers (Postgres + Redis)'
docker compose -f $Compose up -d 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Die 'docker compose up failed.' }

# "started" is not "accepting connections", and the API fails on the first
# query if Postgres is still replaying its WAL.
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
  $healthy = docker inspect --format '{{.State.Health.Status}}' steprealm-postgres 2>$null
  if ($healthy -eq 'healthy') { $ready = $true; break }
  Start-Sleep -Seconds 2
}
if ($ready) { Ok 'postgres healthy' } else { Die 'postgres never reported healthy. Try: docker compose -f backend/docker-compose.yml logs postgres' }

# --- 2. The API -------------------------------------------------------------

Step "API on :$ApiPort"
if (Test-Api) {
  Ok 'already serving'
} else {
  Stop-Api | Out-Null   # clear a half-dead listener so the port is free
  if (-not (Test-Path $Python)) { Die "no virtualenv at $Python -- create it and install backend/requirements.txt" }
  # --reload-dir is not optional here. Left to itself, --reload watches all of
  # backend/, which includes .venv -- tens of thousands of files that never
  # change and that nothing imports -- and startup then takes longer than
  # anyone will wait for. Watching app/ and alembic/ is both faster and what
  # you actually want reloading.
  $process = Start-Process $Python `
    -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--port', $ApiPort,
                    '--reload', '--reload-dir', 'app', '--reload-dir', 'alembic') `
    -WorkingDirectory $BackendDir -WindowStyle Hidden `
    -RedirectStandardOutput $ApiLog -RedirectStandardError $ApiErr -PassThru
  Set-Content $ApiPid $process.Id

  $serving = $false
  for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Api) { $serving = $true; break }
    if ($process.HasExited) { break }
  }
  if ($serving) {
    Ok "serving (pid $($process.Id), logs: $ApiLog)"
  } else {
    if ($process.HasExited) {
      Write-Host '    the API process exited. Its last words:' -ForegroundColor Red
      Write-Host (Get-Content $ApiErr -Tail 20 -ErrorAction SilentlyContinue) -ForegroundColor DarkGray
      Die "startup failed. Full log: $ApiErr"
    }
    Write-Host '    still not answering after 60s. Its last words:' -ForegroundColor Yellow
    Write-Host (Get-Content $ApiErr -Tail 20 -ErrorAction SilentlyContinue) -ForegroundColor DarkGray
    Die "timed out. Full log: $ApiErr"
  }
}

# --- 3. The phone -----------------------------------------------------------

$phoneReady = $false
if (-not (Test-Path $Adb)) {
  Warn "adb not found at $Adb -- skipping the phone"
} else {
  Step 'Phone'
  # Starting the daemon explicitly, because a dead daemon makes every later
  # adb call fail in a way that reads like "device not found".
  & $Adb start-server 2>&1 | Out-Null

  $devices = @(& $Adb devices | Select-String "`tdevice$")
  if ($devices.Count -eq 0) {
    Warn 'no device connected. The phone needs USB to reach this machine -- see below.'
  } else {
    Ok "connected ($($devices[0].ToString().Split("`t")[0]))"

    # Re-issued every run on purpose: these live in the adb daemon and do not
    # survive it restarting, which is what causes "cannot reach server".
    & $Adb reverse "tcp:$ApiPort" "tcp:$ApiPort" 2>&1 | Out-Null
    & $Adb reverse "tcp:$MetroPort" "tcp:$MetroPort" 2>&1 | Out-Null
    $tunnels = @(& $Adb reverse --list)
    if ($tunnels -match ":$ApiPort") {
      Ok "tunnel :$ApiPort -> this machine ($($tunnels.Count) total)"
      $phoneReady = $true
    } else {
      Warn "could not create the :$ApiPort tunnel"
    }
  }
}

# --- summary ----------------------------------------------------------------

Write-Host ''
if ($phoneReady) {
  Write-Host '  Ready. Open StepRealm on the phone.' -ForegroundColor Green
  exit 0
}

Write-Host '  Backend is up, but the phone cannot reach it yet.' -ForegroundColor Yellow
Write-Host ''
Write-Host '  The installed app points at http://localhost:8000, and on Android'
Write-Host '  localhost means the phone itself -- so it reaches this machine only'
Write-Host '  through the USB tunnel created above.'
Write-Host ''
Write-Host '  Connect the phone by USB with USB debugging on, then re-run:'
Write-Host '      ./start.ps1'
Write-Host ''
Write-Host '  Or point the app at this machine over Wi-Fi instead, which frees the'
Write-Host '  cable -- one rebuild, then no USB at all:'
Write-Host '      $env:EXPO_PUBLIC_API_URL = "http://<this-machine-lan-ip>:8000"'
Write-Host '      npx expo run:android --variant release --no-bundler   (from mobile\)'
exit 1