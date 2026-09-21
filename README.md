# 积水隐患巡查

该项目服务于网格员和社区管理工作，负责积水隐患巡查相关信息的规范化处理与留痕。

运行环境：Python 3.11（仅标准库，无第三方依赖）。代码位于 `src` 目录，配置与数据文件应按部署环境提供。

## 能力概览

- **上报受理**：接收网格员上报的地点、时间、现场描述、图片摘要与严重级别；
- **自动聚合**：同一地点（默认 50 米内）短时（默认 30 分钟滑动窗口）的重复上报聚合为一个事件，每条原始证据完整保留；
- **待补充拦截**：坐标精度不足（默认差于 50 米）或现场描述缺失的事件进入「待补充」，补齐前不能核实/派发，资料齐全后自动转入「待核实」；
- **状态机**：核实、转派、合并、关闭、重新打开，每次变化记录操作者与依据；
- **幂等**：上报编号（`report_id`）与操作编号（`op_id`）幂等，弱网重复提交返回首次结果；
- **调度查询**：未处理隐患列表含当前责任部门、处理时限（按严重级别 2/4/8/24/48 小时）与超时标记；
- **持久化**：全部状态落 SQLite，服务重启后聚合关系与完整时间线仍可追溯。

## 快速开始

```python
from src.service import HazardService
from src.models import Severity

svc = HazardService("hazard.db")

# 网格员上报（report_id 为幂等键）
result = svc.submit_report(
    report_id="RPT-0901", reporter_id="网格员-老周",
    latitude=31.2304, longitude=121.4737, accuracy_m=8.0,
    reported_at="2026-09-21T08:00:00+00:00",
    description="井盖周边积水约10厘米", image_digest="sha256:img-a",
    severity=Severity.MAJOR,
)
event_id = result.event.event_id

# 核实 -> 转派（op_id 幂等，reason 为必填的操作依据）
svc.verify(event_id, operator="调度员-李", reason="视频连线确认", op_id="OP-V-001")
svc.reassign(event_id, department="市政排水所", operator="调度员-李",
             reason="按辖区分派", op_id="OP-R-001")

# 调度端：未处理隐患 + 责任部门 + 处理时限
for item in svc.list_unhandled():
    print(item.event_id, item.department, item.deadline, item.is_overdue)

# 完整档案：原始证据 + 时间线
detail = svc.get_event_detail(event_id)
svc.shutdown()
```

## 运行测试与演示

```bash
python3 -m unittest discover -s tests -v   # 单元测试
python3 demo.py                            # 暴雨积水场景端到端演示
```

## 目录结构

- `src/models.py` — 状态机、严重级别、SLA 与数据结构
- `src/storage.py` — SQLite 持久化（事件 / 证据 / 时间线 / 幂等记录）
- `src/service.py` — 领域服务：聚合、状态流转、幂等、调度查询
- `tests/` — 单元测试；`demo.py` — 场景演示
