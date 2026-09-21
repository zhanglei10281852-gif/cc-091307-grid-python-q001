# 积水隐患巡查

暴雨后网格员多人、多次、信号断续地上报同一处井盖积水时，本服务负责：

- 接收上报（地点、时间、现场描述、图片摘要、严重级别）；
- 将**同一地点、短时间内**的重复上报聚合为一个事件，同时逐条保留原始证据；
- 坐标精度不足或描述缺失时进入**待补充**状态，禁止直接派发；
- 事件可核实、派发/转派、合并、关闭、重新打开，**每次状态变化记录操作者与依据**；
- 上报编号幂等，弱网重试不产生重复证据；
- 调度端可查询未处理隐患、当前责任部门与处理时限；
- 全部聚合关系与完整时间线持久化在 SQLite，**服务重启后仍可追溯**。

运行环境：Python 3.11，仅使用标准库（sqlite3），无外部依赖。

## 快速开始

```python
from src.service import Service, HIGH

svc = Service("patrol.db")          # 文件路径持久化；":memory:" 用于测试

r = svc.report(
    reporter="网格员老周",
    location_name="幸福路与建设街交叉口北侧井盖",
    description="井盖周边积水约20厘米",
    image_summary="近景照片：积水淹没井盖边缘",
    severity=HIGH,                  # LOW / MEDIUM / HIGH / CRITICAL，也接受中文
    lat=31.23041, lng=121.47372,
    coord_accuracy=6,               # 米；超过 100m 视为精度不足
    report_id="zhou-0805",          # 客户端幂等键，重复提交返回原事件
)
# r["status"] == "PENDING"；若缺坐标或描述则为 "PENDING_SUPPLEMENT"

svc.verify(r["event_id"], operator="值班长王敏", basis="现场视频确认")
svc.dispatch(r["event_id"], operator="调度员张强", basis="工单 WX-1",
             responsible_dept="市政排水所")      # 按级别自动设定处理时限
svc.transfer(r["event_id"], "排水所值班室", "井盖属供水设施", "自来水公司抢修队")
svc.close_event(r["event_id"], "赵磊", "抽水完毕、更换垫圈，现场恢复正常")
svc.reopen(r["event_id"], "老周", "再次降雨，同位置积水复发")

svc.list_unhandled()     # 未处理清单（紧急优先，含缺失项与可派发标记）
svc.dispatch_board()     # 已派发事件的责任部门、时限、是否超时
svc.get_event(r["event_id"])          # 事件详情 + 全部证据 + 完整时间线
```

端到端暴雨场景演示：

```bash
python3 demo_storm.py
```

## 状态机

```
                 信息缺失
   新上报 ───────────────▶ 待补充 ──补充/新证据补齐──▶ 待处理
     │                                                  │
     └──────────────────────────────────────────────────┘
                              │ 核实
                              ▼
                            已核实 ──派发──▶ 已派发 ──转派──▶ 已派发
                              └──派发──┘        │
                                                ├── 关闭 ──▶ 已关闭 ──重新打开──▶ 待处理
                                                └── 合并 ──▶ 已合并（终态，证据并入目标事件）
```

- 待补充事件调用核实/派发会抛出 `InvalidTransition`；
- 处理时限自**首次上报**起算：紧急 2h、高 4h、中 12h、低 24h（可在 `Service(sla_seconds=...)` 覆盖）；转派不重新计时；
- 合并保留被合并事件的原始证据归属，通过合并关系链在目标事件上可一并查询。

## 聚合规则

- 时间窗：与活跃事件最后一次上报间隔 ≤ 2 小时（`aggregation_window_seconds` 可调）；
- 同地点：归一化地点名称相同（忽略空白、标点、大小写），或坐标在 50 米半径内（Haversine，可调）；
- 聚合后严重级别取最高，事件定位与地点名在缺失时由新证据补齐。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 数据模型（SQLite）

| 表 | 说明 |
| --- | --- |
| `events` | 事件主表：状态、严重级别、责任部门、派发时间、处理时限、聚合定位 |
| `reports` | 每条原始上报（证据），独立留痕，含坐标/描述完整标记 |
| `timeline` | 完整状态时间线：动作、前后状态、操作者、依据、明细 |
| `merges` | 事件合并关系（source → target），支持递归追溯证据链 |
