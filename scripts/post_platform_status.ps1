param(
  [string]$PlatformUrl = "http://127.0.0.1:9000",
  [ValidateSet("pi", "cloud")]
  [string]$Preset = "pi",
  [string]$DeviceToken = "",
  [switch]$Offline
)

$ErrorActionPreference = "Stop"

$online = -not $Offline

if ($Preset -eq "cloud") {
  $payload = @{
    device_id = "asdun-cloud"
    role = "inference_server"
    display_name = "asdun-cloud"
    online = $online
    status = @{
      service = "inference"
      provider = "CUDAExecutionProvider"
      gallery_count = 0
      avg_latency_ms = 45
    }
  }
} else {
  $payload = @{
    device_id = "pi-01"
    role = "raspberry_pi"
    display_name = "asdun@asdun"
    online = $online
    status = @{
      mode = "hybrid"
      fps = 22.5
      cloud_connected = $true
      active_tracks = 0
    }
  }
}

$body = $payload | ConvertTo-Json -Depth 8
$url = "$($PlatformUrl.TrimEnd('/'))/api/status"
$headers = @{
  "Content-Type" = "application/json"
  "X-ASDUN-Device-Id" = $payload.device_id
}
if ($DeviceToken) {
  $headers["X-ASDUN-Device-Token"] = $DeviceToken
}

Invoke-RestMethod -Uri $url -Method Post -Headers $headers -Body $body
