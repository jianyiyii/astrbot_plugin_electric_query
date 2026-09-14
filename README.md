# 山文宿舍电费查询 (astrbot_plugin_electric_query)

面向 **山东文化产业职业学院（山文）** 学生的 AstrBot 插件：宿舍电费/剩余电量查询、
低电量定时分级提醒、快捷充值入口导航。

- 数据来源：校园一卡通 `card.sdcivc.edu.cn` 公开接口，**无需登录、无需 token**
- 充值只提供官方入口链接，支付由学生在微信手动完成，插件不涉及任何支付
- WebUI 可视化配置：监控开关、检查间隔、提醒阈值等全部可在 AstrBot 面板调整

## 功能

| 功能 | 说明 | 指令 |
|---|---|---|
| 电量查询 | 实时剩余电量（照明/空调分表） | `电费`、`电费 319`、`电费 2-319` |
| 用法说明 | 查询示例 | `电费帮助` |
| 自动监控 | 每 6 小时（WebUI 可调）自动查询 | 无需指令 |
| 分级提醒 | `<20度` 普通提醒 ⚠️；`<10度` 强提醒 🚨（附充值入口） | 自动推送 |

## 接口（公开，2026-09-14 实测有效）

网关基址：`https://card.sdcivc.edu.cn/bc/gateway`

```
POST {base}/ecu/api/roomInfo/query/1   body {"loudongId": null}    → 楼栋列表
POST {base}/ecu/api/roomInfo/query/2   body {"loudongId": 楼栋id}   → 房间表具列表
```

房间条目：`{loudong, loudongId, room("2-319照明"), roomId, allAmp, usedAmp}`，
**剩余电量 = allAmp − usedAmp（度）**。同一房间有 照明 / 空调 / 水表 三块表，
默认展示 照明 + 空调。

## 安装（AstrBot）

1. WebUI「插件管理」→ 导入 `electric_query.zip`（或把 `electric_query/` 文件夹放进 `plugins/`）。
2. 安装依赖：`pip install aiohttp apscheduler`（aiohttp 为 AstrBot 自带，重点是 apscheduler）。
3. 启用插件即可用；在「插件管理 → electric_query → 配置」中调整参数。

## WebUI 配置项

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `building` | 文本 | `2号公寓` | 默认楼栋（支持 2 / 2号楼 / 2号公寓） |
| `room` | 文本 | `319` | 默认房间号 |
| `cache_ttl` | 数字 | `300` | 查询结果缓存秒数 |
| `timeout` | 数字 | `15` | 请求超时（秒） |
| `retries` | 数字 | `2` | 请求失败重试次数 |
| `check_enabled` | 开关 | 开 | 定时低电量监控总开关 |
| `check_interval_hours` | 数字 | `6` | 自动检查间隔（小时），24=每天一次 |
| `warn_threshold` | 数字 | `20` | 普通提醒阈值（度）⚠️ |
| `alert_threshold` | 数字 | `10` | 强提醒阈值（度）🚨 |
| `warn_repeat_hours` | 数字 | `24` | 普通提醒重复间隔（小时） |
| `alert_repeat_hours` | 数字 | `6` | 强提醒重复间隔（小时） |
| `recharge_url` | 文本 | 官方充值页 | 提醒附带的充值入口链接 |

> 插件目录内 `config.json` 仍是初始默认值来源；在 WebUI 修改后，以 AstrBot
> 数据目录 `data/config/electric_query_config.json` 为准。
> `meter_types`（表具类型，`[]`=全部）、`watch_rooms`（额外监控房间，
> 如 `["2号公寓:319","5号公寓:206"]`）、`notify_sessions`（指定推送会话，
> 留空=自动记住使用过指令的会话）三个列表字段暂未进 WebUI，可直接改 `config.json`。

## 使用示例

```
学生：电费
机器人：🏠 宿舍查询结果

        💡 2-319照明：5.38 度
        ❄️ 2-319空调：0.68 度

        📅 查询时间：2026-09-14 20:30

学生：电费 2-319
学生：电量 5-206
```

低电量提醒（自动推送）：

```
🚨 电量严重不足，请立即充值！

🏠 2号公寓 319
💡 2-319照明：5.38 度
❄️ 2-319空调：0.68 度
剩余仅 0.68 度（低于 10 度强提醒线）

👉 充值入口：https://card.sdcivc.edu.cn/#/pages/home/sdwhcyxy-card/electronic-pay
（微信打开后选楼栋→选房间→选金额，支付请手动确认）
📅 2026-09-14 20:30
```

## 数据来源与隐私

- 数据来源：山东文化产业职业学院校园一卡通公开接口 `card.sdcivc.edu.cn/bc/gateway`，
  仅查询楼栋与房间电表读数。
- **不保存任何登录凭据**：无需账号密码、无需 token、不保存 Cookie，任何情况下
  都不会触发登录或支付流程。
- 插件只读：只调用查询类接口，不触碰下单/支付接口。

## 发布到 GitHub

仓库结构（**插件文件平铺在仓库根**，与 AstrBot 社区惯例一致）：

```
astrbot_plugin_electric_query/          ← GitHub 仓库根（= 插件根）
├── README.md
├── requirements.txt
├── metadata.yaml
├── main.py
├── config.json
├── _conf_schema.json
├── __init__.py
├── logo.png  (可选：插件图标，WebUI 中显示)
└── LICENSE   (推荐：如 MIT)
```

上传后建议：

1. 把 `metadata.yaml` 与 `main.py` 里 `@register` 的 `repo` 字段改成你的仓库地址。
2. 把 `metadata.yaml` 的 `author` 改成你的 GitHub 用户名。
3. 附带开源协议（如 MIT）`LICENSE` 文件。
4. GitHub Release 里附上 `electric_query.zip` 供 WebUI 一键导入
   （zip 内为插件文件或带一层文件夹均可，AstrBot 导入时会自动处理）。
5. 参考仓库示例（同类项目，含更多进阶玩法）：
   https://github.com/Scarbal486/astrbot_plugin_buaa_power

## 常见问题

- **提示「查询结果为空」**：房间号写错，或该房间只有水表而被 `meter_types` 过滤。
- **收不到自动提醒**：先发一次 `电费` 让插件记住会话，或在 `notify_sessions`
  里显式填写会话 ID（形如 `aiocqhttp:GroupMessage:123456789`）。
- **接口变更排查**：Chrome 打开一卡通 → F12 → Network → Fetch/XHR，重放上述
  两个请求对比 URL/参数/响应即可定位变化。