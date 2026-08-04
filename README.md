# Oracle TCP Monitor

一个轻量、零第三方运行时依赖的 Oracle Cloud 全球区域 TCP 443 延迟监控工具。

它会并行连接 OCI 官方区域表中的 45 个 Object Storage 端点，将结果持续写入本机 SQLite，并通过浏览器展示实时排名、平均延迟、P95、抖动、成功率和历史趋势。

## 功能

- 同时检测 45 个 Oracle Cloud 区域，不逐个排队等待
- 测量 TCP 443 建连时间，不依赖 ICMP Ping
- SQLite + WAL 持久化，关闭后历史记录不会丢失
- 实时排名、平均值、P95、最低/最高、抖动和成功率
- 单区域历史趋势图
- 1 小时至 30 天统计窗口
- 原始记录 CSV 导出
- 默认监控 48 小时，也支持不限时运行
- 网页支持暂停、继续和清空历史后重新开始
- 点击表头即可按各指标升序或降序排列
- 仅使用 Python 标准库；Windows Release 为真正的单文件 EXE

## 快速开始

> **测试前建议关闭梯子。** 如果你的目标是测本机宽带或当前运营商直连 Oracle 的真实延迟，请先关闭 VPN、TUN 模式、全局代理、游戏加速器以及代理客户端的虚拟网卡。否则结果可能反映代理节点到 Oracle 的线路，而不是本地直连线路。普通浏览器 HTTP 代理通常不会接管本程序的原始 TCP 连接，但为避免环境差异和误判，测试期间仍建议全部关闭。

### Windows 用户

从 Releases 下载 `OracleTCPMonitor_SingleFile.exe`，放进一个可写文件夹后双击。程序会自动打开：

```text
http://127.0.0.1:8765
```

首次运行时会在 EXE 同目录创建 `oracle_latency.db`。

### 从源码运行

需要 Python 3.10 或更高版本，不需要安装第三方依赖：

```powershell
python app.py
```

## 配置

通过环境变量调整运行参数：

| 变量 | 默认值 | 说明 |
| --- | ---: | --- |
| `TCP_INTERVAL` | `60` | 每轮检测间隔，单位秒，最低 5 秒 |
| `TCP_TIMEOUT` | `5` | 单个 TCP 连接超时，单位秒 |
| `TCP_DURATION_HOURS` | `48` | 采集时长；设为 `0` 表示不限时 |
| `TCP_OPEN_BROWSER` | `1` | 设为 `0` 时启动后不自动打开浏览器 |

不限时运行示例：

```powershell
$env:TCP_DURATION_HOURS="0"
python app.py
```

## 构建 Windows 单文件版

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile `
  --name OracleTCPMonitor_SingleFile `
  --add-data "targets.json;." `
  --add-data "web\index.html;web" `
  app.py
```

生成文件位于 `dist/OracleTCPMonitor_SingleFile.exe`。

## 数据说明

这是到 Oracle Object Storage 服务入口的 TCP 建连时间，适合比较本地网络到不同 OCI 区域的线路质量，但不等同于某台未来 VPS 的完整业务延迟。DNS 不通、连接超时或端口失败会记录为失败，不会用超时秒数冒充延迟。

数据库和导出的 CSV 可能反映你的网络环境和解析 IP。提交 Issue 时请先检查并删除不希望公开的数据。

## 区域来源

- [Oracle Cloud Infrastructure Regions and Availability Domains](https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm)
- 塞尔维亚 `eu-jovanovac-1` 属于 OC20，使用 `oraclecloud20.com` realm domain

## 许可证

[MIT](LICENSE)
