# Installerar (eller uppdaterar) Predikan -> Podcast som en Windows-tjänst.
#
# Enklast: dubbelklicka på Installera.cmd i appens mapp. Den startar det här
# skriptet och ber om administratörsbehörighet (behövs för att skapa en tjänst).
#
# Skriptet:
#   1. letar upp Python 3.11+ och ffmpeg - och erbjuder att installera dem
#      med winget om de saknas
#   2. skapar venv:en och installerar appen (via setup.ps1), och skapar .env
#   3. kompilerar tjänsten (PredikanService.cs) med C#-kompilatorn som redan
#      finns i Windows
#   4. registrerar tjänsten "PredikanApp" (startar automatiskt med Windows,
#      och startas om av Windows om den skulle krascha) och startar den
#   5. öppnar appen i webbläsaren
#
# Går att köra igen, t.ex. efter att en ny version packats upp i samma mapp:
# tjänsten stoppas, uppdateras och startas igen. .env, databasen och
# ljudfilerna rörs aldrig.
#
# Parametrar (valfria):
#   -Port 8000          porten appen lyssnar på
#   -ListenOnNetwork    nåbar från andra datorer i nätverket (annars bara
#                       från den här datorn) - öppnar porten i brandväggen
#   -WithLocalWhisper   installera även lokal transkribering (KB-Whisper via
#                       faster-whisper, flera GB). Behövs inte med Groq,
#                       som är standard - lägg till den när ni vill köra lokalt.
#   -Python <sökväg>    en bestämd python.exe i stället för att leta själv
param(
  [int]$Port = 8000,
  [switch]$ListenOnNetwork,
  [switch]$WithLocalWhisper,
  [string]$Python = "",
  # Intern: sätts när skriptet startat om sig självt som administratör, så
  # att det nya fönstret inte stängs innan man hunnit läsa resultatet.
  [switch]$PauseAtEnd
)

$ErrorActionPreference = "Stop"
$ServiceName = "PredikanApp"
$DisplayName = "Predikan → Podcast"
$AppDir = Split-Path -Parent $PSScriptRoot
$WindowsDir = $PSScriptRoot

# --- Administratörsbehörighet -------------------------------------------------
# En tjänst kan bara skapas som administratör. Startades skriptet utan det
# startas det om med samma parametrar - Windows frågar då om lov (UAC).
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Port", $Port)
  if ($ListenOnNetwork) { $argList += "-ListenOnNetwork" }
  if ($WithLocalWhisper) { $argList += "-WithLocalWhisper" }
  if ($Python) { $argList += @("-Python", "`"$Python`"") }
  $argList += "-PauseAtEnd"
  Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
  exit
}

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Ask($question) {
  $answer = Read-Host "$question [J/n]"
  return ($answer -eq "" -or $answer -match "^[jJyY]")
}

# Läser om PATH från registret, så att program som winget just installerat
# hittas utan att ett nytt fönster behöver öppnas.
function Update-SessionPath {
  $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $user = [Environment]::GetEnvironmentVariable("Path", "User")
  $env:Path = "$machine;$user"
}

function Install-WithWinget($id, $what) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "$what saknas och winget finns inte på datorn. Installera $what själv och kör Installera.cmd igen."
  }
  if (-not (Ask "$what saknas. Installera det nu med winget ($id)?")) {
    throw "$what behövs. Installera det och kör Installera.cmd igen."
  }
  # --scope machine: installeras för hela datorn, så att tjänsten (som inte
  # kör som din användare) också hittar det.
  winget install --exact --id $id --scope machine --silent --accept-package-agreements --accept-source-agreements
  Update-SessionPath
}

# Kör ett program (t.ex. python.exe) och returnerar dess utskrift, med
# felutskriften (stderr) som vanliga rader. Windows PowerShell 5.1 gör annars
# varje rad på stderr till ett stoppande fel när $ErrorActionPreference är
# "Stop" - även en ofarlig varning, som python-dotenvs "could not parse
# statement". Om programmet lyckades avgörs av $LASTEXITCODE efteråt.
function Invoke-Native([scriptblock]$Command) {
  $previous = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & $Command 2>&1 | ForEach-Object { "$_" }
  } finally {
    $ErrorActionPreference = $previous
  }
}

# Returnerar sökvägen till en python.exe (3.11 eller senare) som tjänsten kan köra, eller $null.
function Find-Python {
  $candidates = @()
  if ($Python) { $candidates += , @($Python) }
  $candidates += , @("py", "-3")
  $candidates += , @("python")
  foreach ($cmd in $candidates) {
    $exe = $cmd[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $arguments = @($cmd | Select-Object -Skip 1)
    $info = @(Invoke-Native { & $exe @arguments -c "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2])" })
    if ($LASTEXITCODE -ne 0 -or $info.Count -lt 2) { continue }
    $path = $info[0].Trim()
    $version = [version]$info[1].Trim()
    if ($version -lt [version]"3.11") {
      Write-Host "    Hoppar över Python $version ($path) - appen kräver 3.11 eller senare." -ForegroundColor Yellow
      continue
    }
    # Python från Microsoft Store går inte att köra från en tjänst.
    if ($path -like "*\WindowsApps\*") {
      Write-Host "    Hoppar över Python från Microsoft Store ($path) - fungerar inte i en tjänst." -ForegroundColor Yellow
      continue
    }
    return $path
  }
  return $null
}

$failed = $false
try {
  Set-Location $AppDir
  # Filer från en nedladdad zip är märkta "från internet" - ta bort märkningen
  # så att skripten och den kompilerade tjänsten får köras utan varningar.
  Get-ChildItem -Recurse -File -Path $AppDir -Include *.ps1, *.cmd, *.cs -ErrorAction SilentlyContinue | Unblock-File

  # --- 1. Python och ffmpeg ---------------------------------------------------
  Step "Letar efter Python 3.11 eller senare..."
  $pythonExe = Find-Python
  if (-not $pythonExe) {
    Install-WithWinget "Python.Python.3.13" "Python"
    $pythonExe = Find-Python
    if (-not $pythonExe) { throw "Python hittades inte efter installationen. Starta om datorn och kör Installera.cmd igen." }
  }
  Write-Host "    $pythonExe"
  if ($pythonExe -like "$env:SystemDrive\Users\*") {
    Write-Host "    OBS: Python är installerat för en enskild användare. Det fungerar, men tjänsten" -ForegroundColor Yellow
    Write-Host "    slutar fungera om den användaren tar bort Python." -ForegroundColor Yellow
  }

  Step "Letar efter ffmpeg..."
  $ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
  if (-not $ffmpeg) {
    Install-WithWinget "Gyan.FFmpeg" "ffmpeg"
    $ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $ffmpeg) { throw "ffmpeg hittades inte efter installationen. Starta om datorn och kör Installera.cmd igen." }
  }
  $ffmpegDir = Split-Path -Parent $ffmpeg.Source
  Write-Host "    $($ffmpeg.Source)"

  # --- 2. Appen ----------------------------------------------------------------
  # Tjänsten stoppas först vid en uppdatering, så att inga filer är låsta.
  $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  if ($existing -and $existing.Status -ne "Stopped") {
    Step "Stoppar den befintliga tjänsten..."
    Stop-Service -Name $ServiceName -Force
    $existing.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(60))
  }

  # En befintlig .env som redan transkriberar lokalt behöver lokal Whisper,
  # annars slutar den fungera efter en uppdatering.
  $envFile = Join-Path $AppDir ".env"
  if (-not $WithLocalWhisper -and (Test-Path $envFile)) {
    $envText = Get-Content $envFile -Raw
    $provider = [regex]::Match($envText, '(?m)^\s*TRANSCRIPTION_PROVIDER\s*=\s*(\w+)').Groups[1].Value
    $oldLocal = $envText -match '(?m)^\s*USE_LOCAL_WHISPER\s*=\s*true'
    if ($provider -eq "local" -or (-not $provider -and $oldLocal)) {
      Write-Host "    .env transkriberar lokalt - installerar även lokal Whisper." -ForegroundColor Yellow
      $WithLocalWhisper = $true
    }
  }

  Step "Installerar appen (kan ta flera minuter första gången)..."
  $setupArgs = @{ Python = $pythonExe }
  if (-not $WithLocalWhisper) { $setupArgs.NoWhisper = $true }
  & (Join-Path $AppDir "setup.ps1") @setupArgs
  $venvPython = Join-Path $AppDir "venv\Scripts\python.exe"
  # setup.ps1 avbryter inte själv om pip misslyckas - kontrollera att appen går att läsa in.
  $importOutput = @(Invoke-Native { & $venvPython -c "import app" })
  if ($LASTEXITCODE -ne 0) {
    throw "Appen går inte att starta:`n$($importOutput -join "`n")"
  }
  # Varningar stoppar inte installationen, men visas - t.ex. en rad i .env
  # som inte går att läsa och därför hoppas över.
  $envWarnings = @($importOutput | Where-Object { $_ -match "could not parse" })
  if ($envWarnings.Count) {
    Write-Host "    OBS: .env innehåller rader som inte går att läsa och därför hoppas över:" -ForegroundColor Yellow
    $envWarnings | ForEach-Object { Write-Host "      $_" -ForegroundColor Yellow }
    Write-Host "    Öppna .env i Anteckningar och rätta eller ta bort raden (se manualen, avsnitt 15)." -ForegroundColor Yellow
  }

  # --- 3. Tjänsten ---------------------------------------------------------------
  Step "Bygger tjänsten..."
  $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
  if (-not (Test-Path $csc)) { throw "C#-kompilatorn i .NET Framework 4 hittades inte ($csc)." }
  $serviceExe = Join-Path $WindowsDir "PredikanService.exe"
  & $csc /nologo /target:exe /optimize /out:$serviceExe /reference:System.ServiceProcess.dll (Join-Path $WindowsDir "PredikanService.cs")
  if ($LASTEXITCODE -ne 0) { throw "Tjänsten gick inte att kompilera." }

  $listenHost = if ($ListenOnNetwork) { "0.0.0.0" } else { "127.0.0.1" }
  $logDir = Join-Path $AppDir "logs"
  # Inställningarna som PredikanService.exe läser (se PredikanService.cs).
  $config = @(
    "# Skapad av install-service.ps1 - kör Installera.cmd igen i stället för att ändra här.",
    "Python=$venvPython",
    "Arguments=-m uvicorn app:app --host $listenHost --port $Port",
    "WorkingDirectory=$AppDir",
    "PathPrepend=$ffmpegDir",
    "LogFile=$(Join-Path $logDir 'service.log')",
    # Nedladdade modeller (t.ex. KB-Whisper) i appens mapp, inte i
    # systemkontots dolda profil.
    "env.HF_HOME=$(Join-Path $AppDir 'models\huggingface')"
  )
  [IO.File]::WriteAllLines((Join-Path $WindowsDir "service.config"), $config, (New-Object Text.UTF8Encoding($false)))

  Step "Registrerar tjänsten $ServiceName..."
  if ($existing) {
    # Tas bort och skapas igen, så att sökvägen alltid stämmer även om
    # appens mapp har flyttats.
    sc.exe delete $ServiceName | Out-Null
    for ($i = 0; $i -lt 20 -and (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 500 }
  }
  New-Service -Name $ServiceName -BinaryPathName "`"$serviceExe`"" -DisplayName $DisplayName `
    -Description "Klipper, transkriberar och publicerar predikningar på Spreaker. Webbgränssnitt: http://127.0.0.1:$Port" `
    -StartupType Automatic | Out-Null
  # Fördröjd automatisk start (datorn hinner starta klart först), och
  # automatisk omstart om tjänsten skulle krascha.
  sc.exe config $ServiceName start= delayed-auto | Out-Null
  sc.exe failure $ServiceName reset= 86400 actions= restart/10000/restart/30000/restart/60000 | Out-Null

  if ($ListenOnNetwork) {
    Step "Öppnar port $Port i brandväggen (privata nätverk)..."
    Get-NetFirewallRule -DisplayName "Predikan-appen" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName "Predikan-appen" -Direction Inbound -Protocol TCP -LocalPort $Port `
      -Action Allow -Profile Private | Out-Null
  }

  # --- 4. Starta ---------------------------------------------------------------
  Step "Startar tjänsten..."
  Start-Service -Name $ServiceName
  $url = "http://127.0.0.1:$Port"
  $ok = $false
  for ($i = 0; $i -lt 60; $i++) {
    try {
      Invoke-WebRequest "$url/api/version" -UseBasicParsing -TimeoutSec 2 | Out-Null
      $ok = $true
      break
    } catch { Start-Sleep -Seconds 1 }
  }
  if (-not $ok) {
    throw "Tjänsten startade men appen svarar inte. Se $(Join-Path $logDir 'service.log')."
  }

  Write-Host ""
  Write-Host "Klart! Appen körs som tjänsten '$DisplayName' och startar automatiskt med Windows." -ForegroundColor Green
  Write-Host "    Öppna:  $url"
  if ($ListenOnNetwork) {
    Write-Host "    Från andra datorer: http://$($env:COMPUTERNAME):$Port" -ForegroundColor Yellow
    Write-Host "    OBS: inställningsfliken fungerar bara från den här datorn (se SETUP_ALLOW_REMOTE i README)." -ForegroundColor Yellow
  }
  Write-Host "    Logg:   $(Join-Path $logDir 'service.log')"
  Write-Host "Fyll i resten under fliken '⚙️ Inställningar'."
  Start-Process $url
} catch {
  Write-Host ""
  Write-Host "Installationen avbröts: $($_.Exception.Message)" -ForegroundColor Red
  $failed = $true
} finally {
  if ($PauseAtEnd -or $failed) { Read-Host "Tryck Enter för att stänga" | Out-Null }
}
