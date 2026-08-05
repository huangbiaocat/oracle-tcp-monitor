# Oracle TCP Monitor

## 这是什么

本程序是一款用于测试**当前所在地网络到甲骨文云（Oracle Cloud Infrastructure，简称 OCI）各区域服务器连接速度**的工具。

程序会并行连接甲骨文云各区域的 Object Storage HTTPS 服务端口（TCP 443），记录 TCP 建连延迟、平均延迟、P95、抖动和成功率，帮助你比较当地宽带或运营商连接不同甲骨文云区域时的线路质量，并选择更合适的区域。

> 测得的是当前电脑到甲骨文云区域服务入口的 TCP 建连速度，适合做区域间的线路对比，但不等同于某台具体云服务器的完整业务速度或下载带宽。

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
- 页面顶部显示本机名称、本地出口 IP、代理环境变量状态和本次测试目标
- 可在网页中直接修改检测间隔、TCP 连接超时和采集时长，设置会保存到 EXE 同目录
- CSV 导出包含本次网络信息、运行参数和统计窗口
- 网页“节点管理”支持添加、批量导入、编辑、启用/停用和删除自定义服务器
- 自定义节点支持域名、IPv4、IPv6 和任意 `1–65535` TCP 端口，并参与排名、趋势图与 CSV
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

这三个主要参数也可以直接在网页顶部修改并保存，无需使用命令行：

- 检测间隔：默认 60 秒，最低 5 秒
- TCP 连接超时：默认 5 秒
- 采集时长：默认 48 小时，设为 0 表示不限时

网页保存后会生成 `oracle_tcp_settings.json`，下次启动自动继续使用。环境变量用于首次运行默认值，已有网页设置优先。

## 自定义测试节点

点击网页顶部的“节点管理”，可以添加客户自己的服务器地址。单个节点需要填写名称、域名或 IP、TCP 端口；批量导入每行使用：

```text
香港服务器,hk.example.com:443
SSH服务器,1.2.3.4:22
[2001:db8::1]:443
```

- 自定义节点保存在本机 `oracle_latency.db`，不会上传到 GitHub 或其他服务器
- 最多保存 100 个自定义节点，每次最多批量导入 50 个
- 甲骨文默认节点可以停用，但不能删除；自定义节点可以编辑或删除
- 删除自定义节点时会同时删除该节点的历史检测记录
- 请只测试自己拥有或已获授权访问的服务器

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

### 默认测试节点（45 个）

| 国家或地区 | 节点（OCI Region） |
| --- | --- |
| 澳大利亚 | 悉尼 `ap-sydney-1`、墨尔本 `ap-melbourne-1` |
| 巴西 | 圣保罗 `sa-saopaulo-1`、维涅杜 `sa-vinhedo-1` |
| 加拿大 | 蒙特利尔 `ca-montreal-1`、多伦多 `ca-toronto-1` |
| 智利 | 圣地亚哥 `sa-santiago-1`、瓦尔帕莱索 `sa-valparaiso-1` |
| 哥伦比亚 | 波哥大 `sa-bogota-1` |
| 法国 | 巴黎 `eu-paris-1`、马赛 `eu-marseille-1` |
| 德国 | 法兰克福 `eu-frankfurt-1` |
| 印度 | 海得拉巴 `ap-hyderabad-1`、孟买 `ap-mumbai-1` |
| 印度尼西亚 | 巴淡 `ap-batam-1` |
| 以色列 | 耶路撒冷 `il-jerusalem-1` |
| 意大利 | 米兰 `eu-milan-1`、都灵 `eu-turin-1` |
| 日本 | 大阪 `ap-osaka-1`、东京 `ap-tokyo-1` |
| 马来西亚 | 居銮 `ap-kulai-2` |
| 墨西哥 | 克雷塔罗 `mx-queretaro-1`、蒙特雷 `mx-monterrey-1` |
| 摩洛哥 | 卡萨布兰卡 `af-casablanca-1` |
| 荷兰 | 阿姆斯特丹 `eu-amsterdam-1` |
| 沙特阿拉伯 | 利雅得 `me-riyadh-1`、吉达 `me-jeddah-1` |
| 塞尔维亚 | 约万诺瓦茨 `eu-jovanovac-1` |
| 新加坡 | 新加坡 `ap-singapore-1`、新加坡西部 `ap-singapore-2` |
| 南非 | 约翰内斯堡 `af-johannesburg-1` |
| 韩国 | 首尔 `ap-seoul-1`、春川 `ap-chuncheon-1` |
| 西班牙 | 马德里 `eu-madrid-1`、马德里 3 `eu-madrid-3` |
| 瑞典 | 斯德哥尔摩 `eu-stockholm-1` |
| 瑞士 | 苏黎世 `eu-zurich-1` |
| 阿联酋 | 阿布扎比 `me-abudhabi-1`、迪拜 `me-dubai-1` |
| 英国 | 伦敦 `uk-london-1`、纽波特 `uk-cardiff-1` |
| 美国 | 阿什本 `us-ashburn-1`、芝加哥 `us-chicago-1`、凤凰城 `us-phoenix-1`、圣何塞 `us-sanjose-1` |

- [Oracle Cloud Infrastructure Regions and Availability Domains](https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm)
- 塞尔维亚 `eu-jovanovac-1` 属于 OC20，使用 `oraclecloud20.com` realm domain

## 许可证

[MIT](LICENSE)
