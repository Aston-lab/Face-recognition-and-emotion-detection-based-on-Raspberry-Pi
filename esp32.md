# ASDUN ESP32 温湿度上传对接文档

这份文档给 ESP32 端开发使用。ESP32 只负责采集温湿度等传感器数据，并上传到 ASDUN Platform Server；它不需要运行人脸识别、情绪识别、GPU 推理，也不需要关心 `cloud_server/`。

## 1. 对接目标

ESP32 需要做到：

- 连接 Wi-Fi。
- 周期性采集温度、湿度，也可以带光照、RSSI、运行时间等数据。
- 通过 HTTP POST 上传到 Platform Server。
- 网页可以看到 `esp32-01` 在线，并看到最近的遥测数据。

第一阶段先使用 HTTP。后续如果需要更专业的 IoT 通信，再升级 MQTT。

## 2. 访问地址

本地调试时，Platform Server 如果运行在电脑上，ESP32 访问地址应该写电脑的局域网 IP：

```text
http://电脑IP:9000
```

例如：

```text
http://192.168.1.23:9000
```

注意：ESP32 不能使用 `127.0.0.1`。对 ESP32 来说，`127.0.0.1` 是它自己，不是电脑。

如果后续部署到云服务器，则改成公网地址：

```text
https://api.example.com
```

## 3. 健康检查

先确认平台可访问：

```http
GET /health
```

完整例子：

```text
http://192.168.1.23:9000/health
```

正常返回包含：

```json
{
  "ok": true,
  "service": "asdun-platform-server"
}
```

## 4. 推荐接口：上传遥测数据

ESP32 温湿度建议使用：

```http
POST /api/telemetry
Content-Type: application/json
```

完整地址：

```text
http://电脑IP:9000/api/telemetry
```

JSON 格式：

```json
{
  "device_id": "esp32-01",
  "role": "esp32",
  "display_name": "esp32-01",
  "online": true,
  "telemetry": {
    "temperature": 28.4,
    "humidity": 61.0,
    "light": 730,
    "rssi": -52,
    "uptime_ms": 125000
  }
}
```

最小可用 JSON：

```json
{
  "device_id": "esp32-01",
  "role": "esp32",
  "online": true,
  "telemetry": {
    "temperature": 28.4,
    "humidity": 61.0
  }
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `device_id` | string | 设备唯一 ID，建议固定为 `esp32-01` |
| `role` | string | 固定为 `esp32` |
| `display_name` | string | 网页显示名，可省略 |
| `online` | bool | 当前设备是否在线 |
| `telemetry.temperature` | number | 温度，单位摄氏度 |
| `telemetry.humidity` | number | 湿度，百分比 |
| `telemetry.light` | number | 光照，可选 |
| `telemetry.rssi` | number | Wi-Fi 信号强度，可选 |
| `telemetry.uptime_ms` | number | ESP32 运行时间，可选 |

成功返回示例：

```json
{
  "ok": true,
  "event": {
    "device_id": "esp32-01",
    "telemetry": {
      "temperature": 28.4,
      "humidity": 61.0
    }
  }
}
```

ESP32 端只需要判断：

```text
HTTP 状态码是 2xx
返回 JSON 中 ok=true
```

## 5. 可选：设备 Token

本地调试时平台默认不启用 token，可以先不管。

公网部署前建议启用 token。启用后，ESP32 请求需要加：

```http
X-ASDUN-Device-Id: esp32-01
X-ASDUN-Device-Token: esp32-token
```

ESP32 端可以把 token 写成配置：

```cpp
const char* DEVICE_TOKEN = "esp32-token";
```

并在请求里加 header：

```cpp
http.addHeader("X-ASDUN-Device-Id", DEVICE_ID);
http.addHeader("X-ASDUN-Device-Token", DEVICE_TOKEN);
```

## 6. PowerShell 模拟测试

先在电脑上模拟 ESP32 上传：

```powershell
$body = @{
  device_id = "esp32-01"
  role = "esp32"
  display_name = "esp32-01"
  online = $true
  telemetry = @{
    temperature = 28.4
    humidity = 61.0
    light = 730
    rssi = -52
    uptime_ms = 125000
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Uri "http://127.0.0.1:9000/api/telemetry" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

如果平台开启 token：

```powershell
$headers = @{
  "X-ASDUN-Device-Id" = "esp32-01"
  "X-ASDUN-Device-Token" = "esp32-token"
}

Invoke-RestMethod `
  -Uri "http://127.0.0.1:9000/api/telemetry" `
  -Method Post `
  -ContentType "application/json" `
  -Headers $headers `
  -Body $body
```

网页上应看到：

```text
Devices 里出现 esp32-01
Telemetry 里出现最近一条温湿度记录
```

## 7. ESP32 Arduino 示例

需要库：

```cpp
#include <WiFi.h>
#include <HTTPClient.h>
```

示例代码：

```cpp
#include <WiFi.h>
#include <HTTPClient.h>

const char* WIFI_SSID = "your_wifi";
const char* WIFI_PASSWORD = "your_password";

// 必须写电脑局域网 IP 或云端域名，不能写 127.0.0.1。
const char* PLATFORM_URL = "http://192.168.1.23:9000/api/telemetry";

const char* DEVICE_ID = "esp32-01";
const char* DEVICE_TOKEN = "";  // 平台未启用 token 时留空。

unsigned long lastPostMs = 0;
const unsigned long POST_INTERVAL_MS = 5000;

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
  }
}

bool postTelemetry(float temperature, float humidity) {
  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }

  HTTPClient http;
  http.begin(PLATFORM_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-ASDUN-Device-Id", DEVICE_ID);
  if (String(DEVICE_TOKEN).length() > 0) {
    http.addHeader("X-ASDUN-Device-Token", DEVICE_TOKEN);
  }

  String body = "{";
  body += "\"device_id\":\"" + String(DEVICE_ID) + "\",";
  body += "\"role\":\"esp32\",";
  body += "\"display_name\":\"esp32-01\",";
  body += "\"online\":true,";
  body += "\"telemetry\":{";
  body += "\"temperature\":" + String(temperature, 1) + ",";
  body += "\"humidity\":" + String(humidity, 1) + ",";
  body += "\"rssi\":" + String(WiFi.RSSI()) + ",";
  body += "\"uptime_ms\":" + String(millis());
  body += "}}";

  int code = http.POST(body);
  http.end();

  return code >= 200 && code < 300;
}

void setup() {
  Serial.begin(115200);
  connectWifi();
}

void loop() {
  if (millis() - lastPostMs >= POST_INTERVAL_MS) {
    lastPostMs = millis();

    // TODO: 替换成真实传感器读数。
    float temperature = 28.4;
    float humidity = 61.0;

    bool ok = postTelemetry(temperature, humidity);
    Serial.println(ok ? "post ok" : "post failed");
  }
}
```

## 8. 常见问题

ESP32 访问不了平台时检查：

- `PLATFORM_URL` 是否写成电脑 IP，而不是 `127.0.0.1`。
- ESP32 和电脑是否在同一个 Wi-Fi。
- Platform Server 是否正在运行。
- Windows 防火墙是否允许 9000 端口。
- 如果开启 token，token 是否和平台环境变量一致。

网页没有显示 `esp32-01` 时检查：

- PowerShell 模拟 POST 是否成功。
- ESP32 串口是否显示 `post ok`。
- JSON 里是否有 `device_id`。
- 是否 POST 到了 `/api/telemetry`。

建议上传频率：

```text
POST_INTERVAL_MS = 5000
```

也就是 5 秒一次。需要更实时可以改为 1000ms，但不要太频繁，避免网络不稳定时堆积请求。
