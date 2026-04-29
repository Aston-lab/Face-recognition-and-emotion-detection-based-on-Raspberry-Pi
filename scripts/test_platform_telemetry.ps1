param(
  [string]$PlatformUrl = "http://127.0.0.1:9000",
  [string]$DeviceId = "esp32-01",
  [string]$DeviceToken = "",
  [double]$Temperature = 28.4,
  [double]$Humidity = 61.0,
  [int]$Light = 730,
  [int]$Rssi = -52
)

$ErrorActionPreference = "Stop"

$payload = @{
  device_id = $DeviceId
  role = "esp32"
  display_name = $DeviceId
  online = $true
  telemetry = @{
    temperature = $Temperature
    humidity = $Humidity
    light = $Light
    rssi = $Rssi
    uptime_ms = [math]::Abs([Environment]::TickCount)
  }
}

$headers = @{
  "Content-Type" = "application/json"
  "X-ASDUN-Device-Id" = $DeviceId
}
if ($DeviceToken) {
  $headers["X-ASDUN-Device-Token"] = $DeviceToken
}

$url = "$($PlatformUrl.TrimEnd('/'))/api/telemetry"
$body = $payload | ConvertTo-Json -Depth 8
Invoke-RestMethod -Uri $url -Method Post -Headers $headers -Body $body
