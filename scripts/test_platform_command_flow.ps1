param(
  [string]$PlatformUrl = "http://127.0.0.1:9000",
  [string]$DeviceId = "esp32-01",
  [string]$Command = "set_mode",
  [string]$PayloadJson = '{"mode":"test"}',
  [string]$AdminToken = "",
  [string]$DeviceToken = ""
)

$ErrorActionPreference = "Stop"

$baseUrl = $PlatformUrl.TrimEnd("/")
$deviceIdEscaped = [uri]::EscapeDataString($DeviceId)

$adminHeaders = @{
  "Content-Type" = "application/json"
}
if ($AdminToken) {
  $adminHeaders["X-ASDUN-Admin-Token"] = $AdminToken
}

$deviceHeaders = @{
  "Content-Type" = "application/json"
  "X-ASDUN-Device-Id" = $DeviceId
}
if ($DeviceToken) {
  $deviceHeaders["X-ASDUN-Device-Token"] = $DeviceToken
}

$payload = @{}
if ($PayloadJson.Trim()) {
  $payload = $PayloadJson | ConvertFrom-Json
}

$createBody = @{
  device_id = $DeviceId
  command = $Command
  payload = $payload
} | ConvertTo-Json -Depth 8

$created = Invoke-RestMethod `
  -Uri "$baseUrl/api/commands" `
  -Method Post `
  -Headers $adminHeaders `
  -Body $createBody

$pending = Invoke-RestMethod `
  -Uri "$baseUrl/api/commands/pending?device_id=$deviceIdEscaped&limit=10" `
  -Method Get `
  -Headers $deviceHeaders

$resultBody = @{
  device_id = $DeviceId
  ok = $true
  message = "simulated command result"
  result = @{
    handled_by = "test_platform_command_flow.ps1"
  }
} | ConvertTo-Json -Depth 8

$completed = Invoke-RestMethod `
  -Uri "$baseUrl/api/commands/$($created.command.command_id)/result" `
  -Method Post `
  -Headers $deviceHeaders `
  -Body $resultBody

[pscustomobject]@{
  created = $created.command
  pending_count = ($pending.commands | Measure-Object).Count
  completed = $completed.command
}
