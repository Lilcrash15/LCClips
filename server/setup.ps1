# LCClips share server: one-shot setup / redeploy.
#   .\setup.ps1            first-time setup (login, bucket, secret, deploy, configure app)
#   .\setup.ps1 -NewToken  also rotate the upload key
param([switch]$NewToken)
$ErrorActionPreference = "Continue"  # native tools write warnings to stderr; we check $LASTEXITCODE
Set-Location $PSScriptRoot

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "`n$msg" -ForegroundColor Red; exit 1 }

if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail "Node.js is not installed. Get the LTS from https://nodejs.org and re-run." }

Step "Installing wrangler (local to this folder)"
npm install --no-fund --no-audit --loglevel=error
if ($LASTEXITCODE) { Fail "npm install failed." }
$wr = Join-Path $PSScriptRoot "node_modules\.bin\wrangler.cmd"

Step "Logging in to Cloudflare (a browser window opens if needed)"
$who = & $wr whoami 2>&1 | Out-String
if ($who -match "not authenticated|You are not") { & $wr login; if ($LASTEXITCODE) { Fail "Login failed." } }

Step "Creating R2 bucket 'lcclips'"
$out = & $wr r2 bucket create lcclips 2>&1 | Out-String
if ($LASTEXITCODE) {
  if ($out -match "already (exists|owned)") { Write-Host "Bucket already exists - fine." }
  elseif ($out -match "enable R2|10042") {
    Fail ("R2 isn't enabled on your account yet. Open https://dash.cloudflare.com/?to=/:account/r2 ,`n" +
          "click 'Enable R2' (free tier; Cloudflare asks for a card but won't charge under 10 GB), then re-run this script.")
  } else { Write-Host $out; Fail "Could not create bucket." }
}

$appCfgPath = Join-Path $env:APPDATA "LCClips\config.json"
$cfg = New-Object psobject
if (Test-Path $appCfgPath) { $cfg = Get-Content $appCfgPath -Raw | ConvertFrom-Json }

$token = $cfg.share_token
if ($NewToken -or -not $token) {
  Step "Generating a new upload key"
  $bytes = New-Object byte[] 32
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $token = ([Convert]::ToBase64String($bytes) -replace '[+/=]', '')
}

Step "Deploying the worker"
$deploy = & $wr deploy 2>&1 | Out-String
Write-Host $deploy
if ($LASTEXITCODE) { Fail "Deploy failed (see above). If it asked you to register a workers.dev subdomain, do that in the dashboard and re-run." }
$url = ([regex]::Match($deploy, "https://[a-zA-Z0-9.-]+\.workers\.dev")).Value

Step "Storing the upload key on the worker"
$token | & $wr secret put UPLOAD_TOKEN | Out-Null
if ($LASTEXITCODE) { Fail "Could not set the UPLOAD_TOKEN secret." }

if ($url) {
  Step "Configuring the LCClips app"
  New-Item -ItemType Directory -Force (Split-Path $appCfgPath) | Out-Null
  $cfg | Add-Member -NotePropertyName share_server_url -NotePropertyValue $url -Force
  $cfg | Add-Member -NotePropertyName share_token -NotePropertyValue $token -Force
  [IO.File]::WriteAllText($appCfgPath, ($cfg | ConvertTo-Json))  # UTF-8 without BOM
}

Write-Host "`nDone!" -ForegroundColor Green
Write-Host "  Server URL : $url"
Write-Host "  Upload key : $token"
Write-Host "These were saved into LCClips' settings. Restart LCClips if it's running."
