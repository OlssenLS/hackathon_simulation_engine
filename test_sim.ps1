[CmdletBinding()]
param(
    [string]$Elder = "Budi",
    [string]$Scenario = "",
    [int]$Duration = 30
)

$BaseUrl = "http://127.0.0.1:8000"

Write-Host "Authenticating as caregiver@demo.id..." -ForegroundColor Cyan
try {
    $login = Invoke-RestMethod -Uri "$BaseUrl/auth/login" -Method Post -ContentType "application/json" -Body '{"email":"caregiver@demo.id","password":"demo1234"}'
} catch {
    Write-Host "Failed to connect to backend at $BaseUrl. Make sure uvicorn is running!" -ForegroundColor Red
    exit 1
}

$token = $login.access_token
$headers = @{ Authorization = "Bearer $token" }

$elders = Invoke-RestMethod -Uri "$BaseUrl/elders" -Headers $headers
$target = $elders | Where-Object { $_.name -match $Elder } | Select-Object -First 1

if (-not $target) {
    Write-Host "Elder '$Elder' not found. Available elders:" -ForegroundColor Yellow
    $elders | Format-Table id, name, conditions
    exit 1
}

Write-Host "Target Elder: $($target.name) ($($target.id))`n" -ForegroundColor Green

if ($Scenario -ne "") {
    Write-Host "Triggering scenario '$Scenario' ($Duration s)..." -ForegroundColor Cyan
    $body = @{ scenario = $Scenario; duration_s = $Duration } | ConvertTo-Json
    $res = Invoke-RestMethod -Uri "$BaseUrl/sim/$($target.id)/trigger" -Method Post -Headers $headers -ContentType "application/json" -Body $body
    Write-Host "Result:" -ForegroundColor Green
    $res | Format-List
} else {
    Write-Host "Available Linked Elders:" -ForegroundColor Cyan
    $elders | Format-Table id, name, doctor_name
    Write-Host "To trigger a scenario, run:" -ForegroundColor Yellow
    Write-Host "  .\test_sim.ps1 -Scenario fall"
    Write-Host "  .\test_sim.ps1 -Scenario hypoxia"
    Write-Host "  .\test_sim.ps1 -Scenario tachycardia"
    Write-Host "  .\test_sim.ps1 -Scenario bradycardia"
    Write-Host "  .\test_sim.ps1 -Scenario fever"
    Write-Host "  .\test_sim.ps1 -Scenario wander"
    Write-Host "  .\test_sim.ps1 -Scenario hypertensive"
    Write-Host "  .\test_sim.ps1 -Scenario hypoglycemia"
    Write-Host "  .\test_sim.ps1 -Scenario normal"
}
