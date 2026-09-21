"""暴雨后井盖积水隐患：端到端演示。

场景：暴雨过后，网格员老周接连收到多名居民对同一处井盖积水的反映。
手机信号断续，照片与定位分散在多条记录里。本脚本演示：
上报 -> 自动聚合 -> 待补充拦截 -> 补齐 -> 核实 -> 转派 -> 调度查询
-> 幂等重试 -> 合并 -> 关闭/重开 -> 重启后追溯。
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

from src.models import LogAction, Severity
from src.service import HazardService

LAT, LNG = 31.2304, 121.4737  # 事发井盖
T0 = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)


def show(title):
    print(f"\n=== {title} ===")


def main():
    db = os.path.join(tempfile.mkdtemp(), "hazard.db")
    clock = {"t": T0}
    svc = HazardService(db, now=lambda: clock["t"])

    show("1. 信号断续中，三条居民反映陆续到达（同一井盖，定位有漂移）")
    r1 = svc.submit_report(
        report_id="RPT-0901", reporter_id="网格员-老周",
        latitude=LAT, longitude=LNG, accuracy_m=8.0, reported_at=clock["t"],
        description="井盖周边积水约10厘米", image_digest="sha256:img-a",
        severity=Severity.MAJOR)
    clock["t"] += timedelta(minutes=9)
    r2 = svc.submit_report(
        report_id="RPT-0902", reporter_id="网格员-老周",
        latitude=LAT + 0.00015, longitude=LNG, accuracy_m=12.0, reported_at=clock["t"],
        description="水漫上人行道，行人绕行", image_digest="sha256:img-b",
        severity=Severity.SEVERE)
    clock["t"] += timedelta(minutes=12)
    r3 = svc.submit_report(
        report_id="RPT-0903", reporter_id="网格员-小王",
        latitude=LAT, longitude=LNG - 0.0001, accuracy_m=20.0, reported_at=clock["t"],
        description="", image_digest="sha256:img-c",  # 描述在弱网中丢失
        severity=Severity.MAJOR)
    eid = r1.event.event_id
    print(f"三条上报聚合同一事件 {eid}（RPT-0901/0902/0903），"
          f"事件级别取最高：{r3.event.severity.name}")

    show("2. 另有一条室内上报精度太差（200米），单独成事件并进入待补充")
    bad = svc.submit_report(
        report_id="RPT-0904", reporter_id="网格员-老周",
        latitude=LAT + 0.0002, longitude=LNG, accuracy_m=200.0,
        reported_at=clock["t"], description="居民电话反映，定位漂移",
        image_digest="sha256:img-d", severity=Severity.MODERATE)
    print(f"{bad.event.event_id} 状态={bad.event.status.value}，"
          f"可派发={bad.event.dispatchable}")
    try:
        svc.reassign(bad.event.event_id, department="市政排水所",
                     operator="调度员-李", reason="尝试直接派发")
    except Exception as exc:
        print(f"直接派发被拦截：{exc}")

    show("3. 重复提交（弱网重试）按上报编号幂等")
    again = svc.submit_report(
        report_id="RPT-0901", reporter_id="网格员-老周",
        latitude=LAT, longitude=LNG, accuracy_m=8.0, reported_at=T0,
        description="重复内容", image_digest="sha256:img-a",
        severity=Severity.MAJOR)
    print(f"RPT-0901 重复提交：deduplicated={again.deduplicated}，"
          f"事件证据条数={len(svc.get_event_detail(eid).reports)}")

    show("4. 核实 -> 转派市政排水所（操作编号幂等）")
    svc.verify(eid, operator="调度员-李", reason="与老周视频连线确认积水",
               op_id="OP-V-001")
    svc.verify(eid, operator="调度员-李", reason="弱网重试", op_id="OP-V-001")
    svc.reassign(eid, department="市政排水所", operator="调度员-李",
                 reason="属排水设施辖区，2小时紧急时限", op_id="OP-R-001")
    print("核实与转派完成（OP-V-001 重复提交仅生效一次）")

    show("5. 调度端查看未处理隐患（责任部门 / 处理时限 / 是否超时）")
    for item in svc.list_unhandled():
        print(f"  {item.event_id} [{item.status.value}] "
              f"级别={item.severity.name} 部门={item.department or '未派'} "
              f"时限={item.deadline:%H:%M} 超时={item.is_overdue} "
              f"可派发={item.dispatchable} 证据={item.report_count}条")

    show("6. 精度不足的 RPT-0904 实为同一井盖 -> 合并")
    svc.merge(bad.event.event_id, eid, operator="调度员-李",
              reason="老周现场确认电话反映的正是该井盖")
    print(f"{bad.event.event_id} 已并入 {eid}")

    show("7. 处置完成关闭，居民再次反映后重新打开")
    svc.close(eid, operator="市政排水所-张工", reason="清淤完成，积水消退")
    svc.reopen(eid, operator="调度员-李", reason="晚高峰居民再次反映积水")
    print(f"{eid} 当前状态={svc.get_event_detail(eid).event.status.value}")

    show("8. 模拟服务重启：聚合关系与完整时间线仍可追溯")
    svc.shutdown()
    svc2 = HazardService(db, now=lambda: clock["t"])
    detail = svc2.get_event_detail(eid)
    print(f"事件 {eid} 责任部门={detail.event.department}，"
          f"证据 {len(detail.reports)} 条，时间线 {len(detail.timeline)} 步：")
    for log in detail.timeline:
        print(f"  {log.created_at:%H:%M} {log.action.value:<6} "
              f"操作者={log.operator:<8} 依据={log.reason}")
    svc2.shutdown()
    print(f"\n数据文件：{db}")


if __name__ == "__main__":
    main()
