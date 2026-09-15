# 更新日志

本项目的所有重要变更都记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)（主版本.次版本.修订）。

> 说明：v2.x 及更早版本包含登录/绑定/余额查询等方向的功能，已在 v3.0.0 重构时全部移除。
> 本插件只保留电费查询与提醒相关能力，不再记录旧版本历史。

## [4.1.2] - 2026-09-15

### 变更

- WebUI 配置项顺序调整为「开关在前、数值在后」：每个提醒开关紧邻其对应
  的阈值/小时数，说明文案同步改为引用其下方数值的写法。

## [4.1.1] - 2026-09-15

### 修复

- WebUI 配置项按「数值 + 开关」成对排列：每个提醒阈值紧邻其对应的启用开关，
  避免所有数值和所有开关分堆显示（AstrBot 表单按 schema 键顺序渲染）。

## [4.1.0] - 2026-09-15

### 新增

- 提醒级别细分开关，固定阈值与预测式两条提醒通道完全独立：
  - `enable_degree_warn` / `enable_degree_alert`：固定阈值-普通/强提醒开关
  - `enable_hour_warn` / `enable_hour_alert` / `enable_hour_urgent`：
    预测式-普通/强/紧急提醒开关
- 级别禁用时自动降级到下一已启用级别（如关闭强提醒则按普通提醒发），全部关闭则静默。
- 所有新开关可在 AstrBot WebUI 配置。

## [4.0.0] - 2026-09-15

### 新增

- 宿舍电量管家能力，由「查询插件」升级为「查询 + 统计 + 预测 + 提醒」：
  - 历史电量记录：SQLite（`electric_history.db` / `power_history` 表），
    每次拿到新鲜数据自动入库，60 秒内重复采样去重，按保留天数自动清理。
  - 新指令 `用电统计`：最近 24 小时消耗、7 天日均、最高耗电时间段。
  - 新指令 `预测停电`：基于历史数据预测每小时耗电、剩余可用时间与断电时间。
  - 新指令 `用电趋势`：今日 vs 昨日消耗及变化百分比。
  - 智能提醒：按「预计断电剩余小时」分级（>24h 不提醒 / 12-24h 普通 /
    <12h 强提醒 / <3h 紧急），预测数据不足时自动回退固定阈值。
  - 夜间断电保护：每天 22:00 检查，预测凌晨 0-8 点断电且电量偏低时推送
    「夜间断电风险」提醒，每天只推一次。
- 新增模块：`database.py`（存储）、`forecast.py`（线性回归 + 移动平均预测）、
  `statistics.py`（统计与趋势）；原有电费查询接口与指令不变。

### 修复

- 断电预测对时间跨度 <30 分钟的样本拒绝线性拟合（避免同秒/短间隔采样导致
  耗电速度计算出天文数字），并回退移动平均。

## [3.3.0] - 2026-09-15

### 变更

- 采用 AstrBot 插件社区规范：`metadata.yaml` 增加 `display_name`、
  `short_desc`、`astrbot_version`、`support_platforms` 字段。
- 新增 `requirements.txt`、`__init__.py`，扩充 `.gitignore`。
- README 增加「数据来源与隐私」声明。

## [3.2.0] - 2026-09-14

### 变更

- 插件品牌更名为「山文宿舍电费查询」，面向山东文化产业职业学院学生；
  README 全面改版并补充 GitHub 发布说明。

## [3.1.1] - 2026-09-14

### 修复

- 修复 `_conf_schema.json` 结构：AstrBot 4.26.x 要求顶层直接是配置键映射，
  去掉多余的 `"schema"` 包裹层，解决插件加载报 `KeyError: 'type'` 的问题。

## [3.1.0] - 2026-09-14

### 新增

- 支持 AstrBot WebUI 可视化配置（`_conf_schema.json`）：监控开关、
  检查间隔、提醒阈值等可直接在面板调整，`__init__` 接收 AstrBot 传入的配置。

## [3.0.0] - 2026-09-14

### 变更

- 彻底重构为「电费查询 + 定时提醒」插件：移除账号密码登录、验证码绑定、
  余额查询、充值指令等全部登录相关功能，改为调用校园一卡通公开接口
  （`/ecu/api/roomInfo/query/*`），无需任何登录凭据。
- 仅保留并强化：电量查询（`电费` / `查询电费` / `电量`）、默认宿舍配置、
  多房间监控、固定阈值分级提醒、缓存与网络重试。

[4.1.1]: https://github.com/YourName/astrbot_plugin_electric_query/releases/tag/v4.1.1
[4.1.0]: https://github.com/YourName/astrbot_plugin_electric_query/releases/tag/v4.1.0
[4.0.0]: https://github.com/YourName/astrbot_plugin_electric_query/releases/tag/v4.0.0
[3.3.0]: https://github.com/YourName/astrbot_plugin_electric_query/releases/tag/v3.3.0
[3.2.0]: https://github.com/YourName/astrbot_plugin_electric_query/releases/tag/v3.2.0
[3.1.1]: https://github.com/YourName/astrbot_plugin_electric_query/releases/tag/v3.1.1
[3.1.0]: https://github.com/YourName/astrbot_plugin_electric_query/releases/tag/v3.1.0
[3.0.0]: https://github.com/YourName/astrbot_plugin_electric_query/releases/tag/v3.0.0
