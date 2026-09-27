<#
.SYNOPSIS
  Step-by-step setup and pilot run of fwtool on a Windows jump server.

.DESCRIPTION
  Each step prints what it does, runs it, shows the output and waits for Enter.
  Press Ctrl+C at any pause to stop; re-running the script is safe (steps that
  are already done are skipped).

  The script never stores passwords: fwtool prompts for them without echo.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\pilot-setup.ps1
  powershell -ExecutionPolicy Bypass -File .\pilot-setup.ps1 -InstallDir D:\Tools -PilotCsv D:\pilot.csv
#>
param(
    [string]$InstallDir = "C:\Tools",
    [string]$PilotCsv = "",
    [string]$RepoUrl = "https://github.com/moorthyrv/srv-hw-update",
    [string]$Branch = "claude/intelligent-archimedes-q4kz0c",
    [int]$Workers = 10
)

$ErrorActionPreference = "Stop"
$Repo = Join-Path $InstallDir "srv-hw-update"
$VenvPy = Join-Path $Repo ".venv\Scripts\python.exe"
$script:Step = 0

function Step([string]$Title) {
    $script:Step++
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor DarkCyan
    Write-Host (" STEP {0}: {1}" -f $script:Step, $Title) -ForegroundColor Cyan
    Write-Host ("=" * 78) -ForegroundColor DarkCyan
}

function Pause-Step([string]$Msg = "Press Enter to continue, or Ctrl+C to stop") {
    Write-Host ""
    Read-Host $Msg | Out-Null
}

function Ok([string]$Msg)   { Write-Host "  OK   $Msg" -ForegroundColor Green }
function Warn([string]$Msg) { Write-Host "  WARN $Msg" -ForegroundColor Yellow }
function Fail([string]$Msg) {
    Write-Host "  FAIL $Msg" -ForegroundColor Red
    Write-Host "  Copy the output above and send it for help. Re-run this script after fixing." -ForegroundColor Red
    Pause-Step "Press Enter to exit"
    exit 1
}

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Run([string]$Exe, [string[]]$Arguments) {
    Write-Host "  > $Exe $($Arguments -join ' ')" -ForegroundColor DarkGray
    # Send output to the console so it is not mixed into the return value.
    & $Exe @Arguments | Out-Host
    return $LASTEXITCODE
}

function Find-Python {
    foreach ($cand in @(@("py", "-3"), @("python"))) {
        $exe = $cand[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $pyArgs = @($cand | Select-Object -Skip 1) + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
        try { $ver = (& $exe @pyArgs 2>$null | Select-Object -First 1) } catch { continue }
        if ($ver -match '^(\d+)\.(\d+)$' -and ([int]$Matches[1] -eq 3) -and ([int]$Matches[2] -ge 10)) {
            return ,@($cand + @($ver))
        }
    }
    return $null
}

Write-Host ""
Write-Host "fwtool pilot setup (read-only firmware inventory)" -ForegroundColor White
Write-Host "  Install folder : $Repo"
Write-Host "  Branch         : $Branch"
Write-Host "  Workers        : $Workers"
Pause-Step

# ---------------------------------------------------------------- 1. Python
Step "Check Python 3.10 or newer"
$py = Find-Python
if (-not $py) {
    Warn "Python 3.10+ not found."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $ans = Read-Host "  Install Python 3.12 with winget now? (Y/N)"
        if ($ans -match '^[Yy]') {
            Run "winget" @("install", "-e", "--id", "Python.Python.3.12", "--accept-package-agreements", "--accept-source-agreements") | Out-Null
            Refresh-Path
            $py = Find-Python
        }
    }
    if (-not $py) { Fail "Install Python 3.12 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then re-run." }
}
$PyExe = $py[0]
$PyArgs = @($py | Select-Object -Skip 1 | Select-Object -SkipLast 1)
Ok ("Python {0} ({1})" -f $py[-1], (($py | Select-Object -SkipLast 1) -join ' '))
Pause-Step

# ---------------------------------------------------------------- 2. Git
Step "Check Git"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Warn "Git not found."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $ans = Read-Host "  Install Git with winget now? (Y/N)"
        if ($ans -match '^[Yy]') {
            Run "winget" @("install", "-e", "--id", "Git.Git", "--accept-package-agreements", "--accept-source-agreements") | Out-Null
            Refresh-Path
        }
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "Install Git from https://git-scm.com/download/win, then re-run." }
}
git --version
Ok "Git available"
Pause-Step

# ---------------------------------------------------------------- 3. Code
Step "Get or update the fwtool code"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
if (Test-Path (Join-Path $Repo ".git")) {
    Push-Location $Repo
    if ((Run "git" @("fetch", "origin", $Branch)) -ne 0) { Pop-Location; Fail "git fetch failed (network, proxy or GitHub sign-in)." }
    if ((Run "git" @("checkout", $Branch)) -ne 0) { Pop-Location; Fail "git checkout failed." }
    if ((Run "git" @("pull", "--ff-only", "origin", $Branch)) -ne 0) { Pop-Location; Fail "git pull failed. Local changes? Run 'git status' in $Repo." }
    Pop-Location
} else {
    Write-Host "  A GitHub sign-in window may open; sign in with the account that can see the repository."
    if ((Run "git" @("clone", "-b", $Branch, $RepoUrl, $Repo)) -ne 0) { Fail "git clone failed (network, proxy or GitHub sign-in)." }
}
Push-Location $Repo
git log --oneline -1
Pop-Location
Ok "Code is in $Repo"
Pause-Step

# ---------------------------------------------------------------- 4. venv + install
Step "Create Python environment and install fwtool (no admin needed)"
if (-not (Test-Path $VenvPy)) {
    if ((Run $PyExe ($PyArgs + @("-m", "venv", (Join-Path $Repo ".venv")))) -ne 0) { Fail "Could not create the virtual environment." }
}
if ((Run $VenvPy @("-m", "pip", "install", "--upgrade", "pip", "--quiet")) -ne 0) { Warn "pip upgrade failed; continuing." }
if ((Run $VenvPy @("-m", "pip", "install", "-e", $Repo)) -ne 0) {
    Fail "pip install failed. If the server uses a proxy, set it first, e.g.  `$env:HTTPS_PROXY='http://proxy:8080'  and re-run."
}
Run $VenvPy @("-m", "fwtool", "--version") | Out-Null
Ok "fwtool installed"
Pause-Step

# ---------------------------------------------------------------- 5. Dell catalog
Step "Dell firmware catalog (download, or import a file)"
Push-Location $Repo
$rc = Run $VenvPy @("-m", "fwtool", "catalog", "refresh")
if ($rc -ne 0) {
    Warn "Download failed (proxy/TLS inspection is common). Download Catalog.xml.gz in a browser:"
    Write-Host "       https://downloads.dell.com/catalog/Catalog.xml.gz"
    $path = Read-Host "  Full path to the downloaded Catalog.xml.gz (blank to skip Dell)"
    if ($path) {
        if ((Run $VenvPy @("-m", "fwtool", "catalog", "import", $path.Trim('"'))) -ne 0) { Pop-Location; Fail "Catalog import failed." }
    } else {
        Warn "No Dell catalog: Dell components will show 'no-reference'."
    }
}
Pop-Location
Pause-Step

# ---------------------------------------------------------------- 6. ESXi catalog
Step "Dell ESXi validated-stack catalog (optional baseline for ESXi hosts)"
$Esxi = Join-Path $Repo "data\dell\ESXi_Catalog.xml.gz"
if (-not (Test-Path $Esxi)) {
    Push-Location $Repo
    if ((Run "git" @("fetch", "origin", "data/catalog")) -eq 0) {
        # --worktree only: the file is not staged into the code branch
        Run "git" @("restore", "--source", "origin/data/catalog", "--worktree", "--", "data/dell/ESXi_Catalog.xml.gz") | Out-Null
    }
    Pop-Location
}
if (Test-Path $Esxi) { Ok "ESXi catalog: $Esxi" } else { Warn "ESXi catalog not available; continuing without it."; $Esxi = "" }
Pause-Step

# ---------------------------------------------------------------- 7. Pilot CSV
Step "Pilot server list (5 Dell + 5 HPE)"
if (-not $PilotCsv) { $PilotCsv = Join-Path $Repo "pilot.csv" }
if (-not (Test-Path $PilotCsv)) {
    @(
        "name,bmc_ip,support,site,environment,os",
        "dell-r640-01,10.0.0.11,,SITE,prod,esxi",
        "hpe-dl380g10-01,10.0.0.21,hpe,SITE,prod,linux"
    ) | Set-Content -Path $PilotCsv -Encoding UTF8
    Write-Host "  Created a template: $PilotCsv"
    Write-Host "  Replace the example rows with 5 Dell + 5 HPE servers:"
    Write-Host "    Dell: R640, R650, R760XD2, a 13G if any, and the VxFlex node"
    Write-Host "    HPE : Gen8, Gen9, Gen10/Gen10 Plus, Gen11, and at least one support=tpm"
    Write-Host "  'support' is hpe or tpm for HPE servers; other columns are optional."
    Start-Process notepad.exe $PilotCsv
    Pause-Step "Save the file in Notepad, then press Enter here"
}
$servers = @(Import-Csv $PilotCsv | Where-Object { $_.bmc_ip -and $_.bmc_ip.Trim() })
if ($servers.Count -eq 0) { Fail "No rows with a bmc_ip in $PilotCsv." }
$servers | Format-Table -AutoSize | Out-String | Write-Host
Ok ("{0} server(s) in {1}" -f $servers.Count, $PilotCsv)
Pause-Step

# ---------------------------------------------------------------- 8. Reachability
Step "Check HTTPS/443 reachability to each BMC"
$unreachable = 0
foreach ($s in $servers) {
    $ip = $s.bmc_ip.Trim()
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($ip, 443)
        $open = $task.Wait(3000) -and $client.Connected
    } catch { $open = $false } finally { $client.Close() }
    if ($open) { Ok ("{0,-16} {1}" -f $ip, $s.name) } else { Warn ("{0,-16} {1}  port 443 not reachable" -f $ip, $s.name); $unreachable++ }
}
if ($unreachable) { Warn "$unreachable BMC(s) not reachable; they will be reported as 'unreachable'." }
Pause-Step

# ---------------------------------------------------------------- 9. Run
Step "Run the pilot inventory (read-only: HTTP GET only)"
$dellUser = Read-Host "  Dell iDRAC read-only username (blank to skip Dell)"
$hpeUser  = Read-Host "  HPE iLO read-only username (blank to skip HPE)"
if ($dellUser) { $env:DELL_BMC_USER = $dellUser } else { Remove-Item Env:DELL_BMC_USER -ErrorAction SilentlyContinue }
if ($hpeUser)  { $env:HPE_BMC_USER  = $hpeUser }  else { Remove-Item Env:HPE_BMC_USER -ErrorAction SilentlyContinue }
Write-Host "  fwtool will now ask for the password(s); typing is hidden."
$runsDir = Join-Path $Repo "runs"
$fwArgs = @("-m", "fwtool", "inventory", "-i", $PilotCsv, "-o", $runsDir, "--save-raw", "--workers", "$Workers")
if ($Esxi) { $fwArgs += @("--dell-esxi-catalog", $Esxi) }
Push-Location $Repo
# Run directly in the console (not through a pipe) so the password prompt and progress bar work.
Write-Host "  > $VenvPy $($fwArgs -join ' ')" -ForegroundColor DarkGray
& $VenvPy @fwArgs
$rc = $LASTEXITCODE
Pop-Location
Remove-Item Env:DELL_BMC_USER, Env:HPE_BMC_USER -ErrorAction SilentlyContinue
if ($rc -ne 0) { Fail "fwtool inventory exited with code $rc." }
Pause-Step

# ---------------------------------------------------------------- 10. Results
Step "Results"
$run = Get-ChildItem $runsDir -Directory | Sort-Object Name -Descending | Select-Object -First 1
$csv = Join-Path $run.FullName "servers.csv"
Import-Csv $csv | Select-Object rank, name, vendor, model, generation, bmc_type, collection_status, overall_status,
    priority_score, components_behind, error_class |
    Format-Table -AutoSize | Out-String -Width 250 | Write-Host
Write-Host ""
Write-Host "  Validation (checks the results for gaps and problems):" -ForegroundColor Cyan
Push-Location $Repo
$valArgs = @("-m", "fwtool", "validate", "--run-dir", $run.FullName, "--share")
if ($Esxi) { $valArgs += @("--dell-esxi-catalog", $Esxi) }
& $VenvPy @valArgs
Pop-Location
Write-Host "  Run folder: $($run.FullName)"
Write-Host "  Send back: validation-share.txt (names/IPs masked), plus servers.csv / firmware.csv if asked."
Write-Host "  (No passwords are stored in any of these files.)"
$ans = Read-Host "  Open the run folder and report.xlsx now? (Y/N)"
if ($ans -match '^[Yy]') {
    Start-Process explorer.exe $run.FullName
    $xlsx = Join-Path $run.FullName "report.xlsx"
    if (Test-Path $xlsx) { Start-Process $xlsx }
}
Write-Host ""
Write-Host "Done." -ForegroundColor Green
