# Voicisst installer for Windows (PowerShell 5.1+ / pwsh).
#
# Installs voicisst[local,server,ui] as an isolated CLI tool using uv
# (preferred) or pipx (installed via pip --user as a fallback). Tries PyPI
# first; if the package is not published there yet, installs straight from
# the git repository.
#
# Usage (from a regular PowerShell prompt — no admin needed):
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1

$ErrorActionPreference = 'Stop'

$RepoUrl = 'https://github.com/lucyfromnaarm/voicisst'
$Extras = 'local,server,ui'
$PypiSpec = "voicisst[$Extras]"
$GitSpec = "voicisst[$Extras] @ git+$RepoUrl"

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Warning2([string]$Message) { Write-Host "!!  $Message" -ForegroundColor Yellow }

# --- 1. Python ---------------------------------------------------------------
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Warning2 'Python not found. Install Python 3.10+ first, e.g.:'
    Write-Warning2 '    winget install Python.Python.3.12'
    Write-Warning2 '(or from https://www.python.org/downloads/ — tick "Add python.exe to PATH")'
    exit 1
}
& python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Warning2 'Voicisst needs Python 3.10 or newer. Upgrade, e.g.: winget install Python.Python.3.12'
    exit 1
}

# --- 2. pick an installer: uv > pipx > (pip --user pipx) ----------------------
$tool = $null
if (Get-Command uv -ErrorAction SilentlyContinue) {
    $tool = 'uv'
} elseif (Get-Command pipx -ErrorAction SilentlyContinue) {
    $tool = 'pipx'
} else {
    Write-Step 'Neither uv nor pipx found - installing pipx into your user site'
    & python -m pip install --user pipx
    if ($LASTEXITCODE -ne 0) {
        Write-Warning2 'Could not install pipx. Install uv (winget install astral-sh.uv) or pipx manually.'
        exit 1
    }
    & python -m pipx ensurepath
    $tool = 'pipx-module'
}
Write-Step "Using installer: $tool"

function Install-Spec([string]$Spec) {
    # `| Out-Host` keeps the native command's stdout on the console instead of
    # leaking into the function's return value (a classic PowerShell trap).
    switch ($tool) {
        'uv'          { & uv tool install --force $Spec | Out-Host }
        'pipx'        { & pipx install --force $Spec | Out-Host }
        'pipx-module' { & python -m pipx install --force $Spec | Out-Host }
    }
    return ($LASTEXITCODE -eq 0)
}

# --- 3. install: PyPI first, git fallback --------------------------------------
Write-Step "Installing $PypiSpec from PyPI"
if (-not (Install-Spec $PypiSpec)) {
    Write-Warning2 'PyPI install failed (package not published yet?) - installing from git'
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Warning2 'git not found - install it (winget install Git.Git) and re-run.'
        exit 1
    }
    if (-not (Install-Spec $GitSpec)) {
        Write-Warning2 "Install from $RepoUrl failed."
        exit 1
    }
}

# --- 4. next steps --------------------------------------------------------------
Write-Host ''
Write-Step 'Voicisst is installed. Next steps:'
Write-Host @'
  1. Open a NEW terminal so the install directory is on PATH.
     (uv: %USERPROFILE%\.local\bin; pipx: shown by `pipx environment`.
      If `voicisst` is not found, also check your Python user Scripts dir,
      e.g. %APPDATA%\Python\Python312\Scripts.)
  2. voicisst ui              # guided setup in your browser
                              # (or by hand: voicisst config init)
  3. voicisst selftest        # check mic, hotkeys, whisper, polish, injection
  4. voicisst run             # hold the hotkey (right Ctrl), speak, release

For LLM polish, install Ollama (https://ollama.com) and pull a model, or
point [polish] at any OpenAI-compatible server in the config.
'@

# --- 5. optional: start at login (shell:startup shortcut) -----------------------
$answer = Read-Host 'Start Voicisst automatically at login (create a Startup shortcut)? [y/N]'
if ($answer -match '^[Yy]') {
    $flowCmd = Get-Command voicisst -ErrorAction SilentlyContinue
    if (-not $flowCmd) {
        Write-Warning2 '`voicisst` is not on PATH yet in this session. Open a new terminal and create'
        Write-Warning2 'the shortcut by hand: Win+R -> shell:startup -> New shortcut -> "voicisst run".'
    } else {
        $startupDir = [Environment]::GetFolderPath('Startup')
        $shortcutPath = Join-Path $startupDir 'Voicisst Dictation.lnk'
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $flowCmd.Source
        $shortcut.Arguments = 'run'
        $shortcut.Description = 'Voicisst voice dictation'
        $shortcut.WindowStyle = 7  # start minimized
        $shortcut.Save()
        Write-Step "Created $shortcutPath"
    }
} else {
    Write-Step 'Skipped. Create one later: Win+R -> shell:startup -> New shortcut -> "voicisst run".'
}
