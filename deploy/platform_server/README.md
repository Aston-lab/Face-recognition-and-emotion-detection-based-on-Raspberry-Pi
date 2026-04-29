# ASDUN Platform Server 云端部署指南

这份文档对应 `up.md` 的“路线一”：

```text
Platform Server / 网站 / 数据库：部署到云服务器长期运行
Windows/NVIDIA Cloud Server：继续放在本机，负责 GPU 推理
Raspberry Pi：继续通过 Tailscale 调用 Windows 推理服务
ESP32：只向云端 Platform Server 上传温湿度等数据
```

## 1. 部署后现象

部署完成后，你应该得到一个长期可访问的平台地址，例如：

```text
https://api.asdun.example.com
```

之后：

- 浏览器访问这个地址就能打开 ASDUN Platform 网页。
- Windows 推理服务向这个地址上报 `asdun-cloud` 状态和识别事件。
- Raspberry Pi 向这个地址上报 `pi-01` 状态。
- ESP32 向这个地址上报温湿度。
- 本地电脑不再需要长期运行 `platform_server` 的 `9000` 端口。

注意：如果不买云 GPU，Windows 本机的 `cloud_server:8000` 仍然要运行，因为人脸识别和情绪识别还在本机 GPU 上做。

## 2. 推荐服务器

轻量云服务器即可起步：

```text
1 核 / 2 GB 内存
Ubuntu 22.04 或 24.04
Python 3.11+
Nginx
HTTPS 证书
```

长期运行建议：

- 先用 SQLite。
- 定期备份数据库。
- 后续数据量变大再迁 PostgreSQL。

## 3. 设备 Token

公网部署前建议打开设备 token。

生成三个随机 token，分别给：

```text
pi-01
asdun-cloud
esp32-01
```

平台端环境变量：

```text
ASDUN_DEVICE_AUTH_ENABLED=true
ASDUN_DEVICE_TOKENS=pi-01=pi-token,asdun-cloud=cloud-token,esp32-01=esp32-token
ASDUN_ADMIN_TOKEN=admin-token
ASDUN_PLATFORM_ONLINE_TTL_MS=30000
```

设备请求头：

```http
X-ASDUN-Device-Id: pi-01
X-ASDUN-Device-Token: pi-token
```

创建控制命令时使用管理员请求头：

```http
X-ASDUN-Admin-Token: admin-token
```

本地调试可以保持：

```text
ASDUN_DEVICE_AUTH_ENABLED=false
```

## 4. Docker Compose 部署

这是最快方式。

在云服务器上安装 Docker 后，将项目放到服务器，例如：

```bash
cd /opt
git clone <your-repo-url> asdun_pi
cd /opt/asdun_pi/deploy/platform_server
cp platform.env.example platform.env
nano platform.env
```

把 `platform.env` 改成类似：

```text
ASDUN_DEVICE_AUTH_ENABLED=true
ASDUN_DEVICE_TOKENS=pi-01=pi-token,asdun-cloud=cloud-token,esp32-01=esp32-token
ASDUN_ADMIN_TOKEN=admin-token
ASDUN_PLATFORM_ONLINE_TTL_MS=30000
```

启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f platform
```

测试：

```bash
curl http://127.0.0.1:9000/health
curl http://127.0.0.1:9000/api/config/public
```

如果只用 Docker Compose，不经过 Nginx，公网访问地址会是：

```text
http://服务器IP:9000
```

正式使用建议再配 Nginx + HTTPS。

## 5. systemd 部署

这是更传统的云服务器方式。

创建用户和目录：

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin asdun
sudo mkdir -p /opt/asdun_pi
sudo mkdir -p /var/lib/asdun-platform
sudo mkdir -p /etc/asdun
sudo chown -R asdun:asdun /var/lib/asdun-platform
```

把项目放到：

```text
/opt/asdun_pi
```

安装 Python 依赖：

```bash
cd /opt/asdun_pi/platform_server
sudo python3 -m venv .venv
sudo .venv/bin/python -m pip install -U pip
sudo .venv/bin/python -m pip install -r requirements.txt
sudo chown -R asdun:asdun /opt/asdun_pi/platform_server/.venv
```

创建环境变量文件：

```bash
sudo cp /opt/asdun_pi/deploy/platform_server/platform.env.example /etc/asdun/platform.env
sudo nano /etc/asdun/platform.env
```

安装 systemd 服务：

```bash
sudo cp /opt/asdun_pi/deploy/platform_server/asdun-platform.service /etc/systemd/system/asdun-platform.service
sudo systemctl daemon-reload
sudo systemctl enable --now asdun-platform
```

查看状态：

```bash
sudo systemctl status asdun-platform
sudo journalctl -u asdun-platform -f
```

本机测试：

```bash
curl http://127.0.0.1:9000/health
```

## 6. Nginx 反向代理

复制配置：

```bash
sudo cp /opt/asdun_pi/deploy/platform_server/nginx-asdun-platform.conf.example /etc/nginx/sites-available/asdun-platform
sudo nano /etc/nginx/sites-available/asdun-platform
```

把里面的：

```text
api.asdun.example.com
```

改成你的真实域名。

启用站点：

```bash
sudo ln -s /etc/nginx/sites-available/asdun-platform /etc/nginx/sites-enabled/asdun-platform
sudo nginx -t
sudo systemctl reload nginx
```

之后浏览器访问：

```text
http://你的域名
```

再配置 HTTPS 证书后，最终使用：

```text
https://你的域名
```

## 7. 部署后修改设备配置

### Windows Cloud Server

修改 `cloud_server/config.yaml`：

```yaml
platform:
  enabled: true
  base_url: "https://你的域名"
  device_id: "asdun-cloud"
  device_token: "cloud-token"
  role: "inference_server"
  display_name: "asdun-cloud"
```

然后重启 Windows 推理服务：

```powershell
.\scripts\run_cloud_server.ps1 -SkipInstall
```

### Raspberry Pi

修改 `config/app.yaml`：

```yaml
platform_enabled: true
platform_base_url: "https://你的域名"
platform_device_id: "pi-01"
platform_device_token: "pi-token"
```

注意：Pi 调用 Windows 推理服务的地址仍然保持 Tailscale 地址，例如：

```yaml
cloud_server_urls:
  - "http://asdun-cloud:8000"
```

### ESP32

ESP32 上传地址改成：

```text
https://你的域名/api/telemetry
```

并设置：

```text
DEVICE_ID=esp32-01
DEVICE_TOKEN=esp32-token
```

## 8. 快速测试

从你的 Windows 电脑测试云平台：

```powershell
curl.exe https://你的域名/health
curl.exe https://你的域名/api/config/public
```

模拟 ESP32 上传：

```powershell
.\scripts\test_platform_telemetry.ps1 `
  -PlatformUrl "https://你的域名" `
  -DeviceId "esp32-01" `
  -DeviceToken "esp32-token"
```

模拟创建命令、设备轮询、设备回传结果：

```powershell
.\scripts\test_platform_command_flow.ps1 `
  -PlatformUrl "https://你的域名" `
  -DeviceId "esp32-01" `
  -AdminToken "admin-token" `
  -DeviceToken "esp32-token"
```

模拟 Pi 状态：

```powershell
.\scripts\post_platform_status.ps1 `
  -PlatformUrl "https://你的域名" `
  -Preset pi `
  -DeviceToken "pi-token"
```

模拟 Windows 推理服务状态：

```powershell
.\scripts\post_platform_status.ps1 `
  -PlatformUrl "https://你的域名" `
  -Preset cloud `
  -DeviceToken "cloud-token"
```

## 9. 备份数据库

Docker Compose 默认数据库在 Docker volume 中。

systemd 部署默认数据库在：

```text
/var/lib/asdun-platform/asdun_platform.sqlite
```

建议定期备份这个文件。

备份前可以先停止服务：

```bash
sudo systemctl stop asdun-platform
sudo cp /var/lib/asdun-platform/asdun_platform.sqlite /var/lib/asdun-platform/asdun_platform.sqlite.bak
sudo systemctl start asdun-platform
```

## 10. 下一阶段

云端部署稳定后，再继续：

- React 前端重构。
- 设备注册页面。
- 控制命令下发。
- 告警规则。
- PostgreSQL 迁移。
