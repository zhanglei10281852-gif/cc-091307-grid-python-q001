"""暴雨场景端到端演示。

运行：python3 demo_storm.py

场景：暴雨过后网格员老周等多人上报同一处井盖积水，照片与定位分散在不同
记录里。演示自动聚合、待补充拦截、核实派发、转派、重复事件合并与重启追溯。
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

from src.service import Service, CRITICAL, HIGH

CN_TZ = timezone(timedelta(hours=8))


def show(title, payload):
    print(f"\n=== {title} ===")
    if isinstance(payload, dict):
        for key in ("status_label", "responsible_dept", "deadline",
                    "evidence_count", "severity_label", "overdue",
                    "missing_fields", "merged_from"):
            if key in payload:
                print(f"  {key}: {payload[key]}")
    else:
        for item in payload:
            print(
                f"  [{item['severity_label']}] {item['event_id']} "
                f"{item['location_name']} | {item['status_label']} | "
                f"责任部门: {item['responsible_dept'] or '-'} | "
                f"时限: {item['deadline'] or '-'} | 证据 {item['evidence_count']} 条"
                + (f" | 缺: {','.join(item['missing_fields'])}"
                   if item.get('missing_fields') else "")
            )


def main() -> None:
    db_dir = tempfile.mkdtemp(prefix="patrol-demo-")
    db_path = os.path.join(db_dir, "patrol.db")
    print(f"数据库文件: {db_path}")

    svc = Service(db_path)
    t0 = datetime.now(CN_TZ) - timedelta(minutes=30)  # 暴雨约半小时前开始

    # 1) 老周的第一条上报：信号差，只发来了描述，没有定位
    r1 = svc.report(
        reporter="网格员老周",
        location_name="幸福路与建设街交叉口北侧井盖",
        reported_at=t0,
        description="井盖周边积水，雨水箅子被落叶堵住",
        image_summary="远景照片：路面积水",
        severity=HIGH,
        report_id="zhou-0805",
    )
    print(f"老周 08:05 上报 -> 事件 {r1['event_id']}，状态: {r1['status']}（缺坐标，待补充）")

    # 2) 居民转述的第二条：信号断续，连描述都没填全，坐标还飘了（精度 300m）
    r2 = svc.report(
        reporter="居民转述/老周",
        location_name="幸福路与建设街交叉口北侧井盖",
        reported_at=t0 + timedelta(minutes=13),
        description="",
        image_summary="模糊视频",
        severity=CRITICAL,
        lat=31.2304, lng=121.4737, coord_accuracy=300,
        report_id="zhou-0818",
    )
    assert r2["event_id"] == r1["event_id"]
    print(f"老周 08:18 续报 -> 聚合到 {r2['event_id']}，"
          f"状态仍为 {r2['status']}（坐标精度不足+描述缺失，不能派发）")

    # 3) 小李在现场发来精确坐标和近照，信息补齐 -> 自动转为待处理
    r3 = svc.report(
        reporter="网格员小李",
        location_name="幸福路与建设街交叉口北侧井盖",
        reported_at=t0 + timedelta(minutes=21),
        description="积水最深处约25厘米，井盖有松动，车辆绕行",
        image_summary="近景照片：积水淹没井盖边缘，有漩涡",
        severity=CRITICAL,
        lat=31.23041, lng=121.47372, coord_accuracy=6,
        report_id="li-0826",
    )
    print(f"小李 08:26 上报精确坐标 -> 聚合到 {r3['event_id']}，状态: {r3['status']}")
    assert r3["event_id"] == r1["event_id"]

    # 4) 弱网重试导致老周第一条被重复提交 —— 幂等，不产生重复证据
    dup = svc.report(
        reporter="网格员老周",
        location_name="幸福路与建设街交叉口北侧井盖",
        reported_at=t0,
        description="井盖周边积水，雨水箅子被落叶堵住",
        image_summary="远景照片：路面积水",
        severity=HIGH,
        report_id="zhou-0805",  # 同一上报编号
    )
    print(f"弱网重试 zhou-0805 -> 幂等命中，返回 {dup['event_id']}，"
          f"证据仍为 {svc.get_event(r1['event_id'])['evidence_count']} 条")

    # 5) 调度端看未处理清单，按严重程度排序
    show("调度端：未处理隐患清单", svc.list_unhandled())

    eid = r1["event_id"]

    # 6) 核实 -> 派发排水所 -> 现场发现是供水井盖，转派自来水公司
    svc.verify(eid, operator="值班长王敏", basis="查看小李现场照片并电话连线确认")
    d = svc.dispatch(eid, operator="调度员张强",
                     basis="紧急工单 WX-20260921-001",
                     responsible_dept="市政排水所")
    print(f"\n已派发 -> {d['responsible_dept']}，处理时限截止: {d['deadline']}")
    t = svc.transfer(
        eid, operator="排水所值班室",
        basis="现场确认井盖属供水设施，排水管道无破损",
        new_dept="自来水公司抢修队",
    )
    print(f"转派 -> {t['responsible_dept']}（截止时间不变: {t['deadline']}）")

    # 7) 另一条被误建的重复事件（不同网格员各自立案），核实后合并
    other = svc.report(
        reporter="网格员小陈",
        location_name="建设街路口积水点",
        reported_at=t0 + timedelta(minutes=35),
        description="同一处路口积水，井盖冒水",
        image_summary="照片",
        severity=HIGH,
        lat=31.2400, lng=121.4900, coord_accuracy=12,
        report_id="chen-0840",
    )
    svc.verify(other["event_id"], "值班长王敏", "现场比对确认同一隐患点")
    merged = svc.merge(
        other["event_id"], eid,
        operator="值班长王敏",
        basis="两单现场位置与照片一致，定位设备偏差导致重复立案",
    )
    print(f"\n重复事件 {other['event_id']} 已合并进 {eid}，"
          f"合并后证据 {merged['evidence_count']} 条（每条原始证据保留）")

    # 8) 处置完成关闭
    svc.close_event(
        eid, operator="自来水公司赵磊",
        basis="关闭供水阀门、更换密封胶圈，抽排积水完毕，路面恢复正常",
        outcome="已修复",
    )

    # 9) 模拟服务重启：聚合关系、完整时间线仍可追溯
    svc.shutdown()
    svc = Service(db_path)
    ev = svc.get_event(eid)
    show("重启后事件详情", ev)
    print("\n完整时间线:")
    for item in svc.event_timeline(eid):
        print(f"  {item['created_at'][11:19]} {item['action']:<16} "
              f"{item['from_status'] or '-'} -> {item['to_status'] or '-'} | "
              f"{item['operator']} | 依据: {item['basis']}")

    print("\n全部原始证据（含合并事件带来的证据）:")
    for rep in svc.list_evidence(eid):
        print(f"  {rep['reported_at'][11:19]} {rep['reporter']} / "
              f"{rep['report_id']} / {rep['image_summary']}")

    # 10) 雨后复发，重新打开
    svc.reopen(eid, operator="网格员老周",
               basis="次日凌晨再次降雨，同一位置积水复发")
    print(f"\n复发重开 -> 状态: {svc.get_event(eid)['status_label']}，"
          "重新进入未处理清单，需再次核实派发")
    svc.shutdown()


if __name__ == "__main__":
    main()
