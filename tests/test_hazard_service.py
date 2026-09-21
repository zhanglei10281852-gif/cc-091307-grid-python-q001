"""隐患巡查服务测试：聚合、待补充、状态机、幂等、调度查询与重启追溯。"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import (  # noqa: E402
    DomainError,
    EventStatus,
    IncompleteEventError,
    InvalidStateTransitionError,
    LogAction,
    Severity,
)
from src.service import HazardService  # noqa: E402

T0 = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
# 事发井盖位置（人民广场附近）
LAT, LNG = 31.2304, 121.4737


class FakeClock:
    def __init__(self, start=T0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, **kwargs):
        self.t += timedelta(**kwargs)


class HazardServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "hazard.db")
        self.clock = FakeClock()
        self.svc = HazardService(self.db_path, now=self.clock)

    def tearDown(self):
        self.svc.shutdown()
        self.tmp.cleanup()

    # -- 工具 ------------------------------------------------------------

    def submit(self, report_id, **overrides):
        params = dict(
            report_id=report_id,
            reporter_id="网格员-老周",
            latitude=LAT,
            longitude=LNG,
            accuracy_m=8.0,
            reported_at=self.clock.t,
            description="井盖周边积水约10厘米",
            image_digest="sha256:" + report_id,
            severity=Severity.MAJOR,
        )
        params.update(overrides)
        return self.svc.submit_report(**params)

    # -- 聚合 ------------------------------------------------------------

    def test_same_location_short_window_aggregates_and_keeps_evidence(self):
        r1 = self.submit("RPT-001")
        self.clock.advance(minutes=10)
        # 同一井盖，定位漂移 20 米，不同居民补充描述与照片
        r2 = self.submit(
            "RPT-002", reporter_id="网格员-小王",
            latitude=LAT + 0.00018, longitude=LNG,  # 约 20 米
            description="水开始漫上人行道", severity=Severity.SEVERE,
        )
        self.clock.advance(minutes=15)
        r3 = self.submit("RPT-003", description="积水散发异味")

        self.assertEqual(r1.event.event_id, r2.event.event_id)
        self.assertEqual(r2.event.event_id, r3.event.event_id)
        event = r3.event
        self.assertEqual(event.severity, Severity.SEVERE)  # 取最高级别

        detail = self.svc.get_event_detail(event.event_id)
        self.assertEqual([r.report_id for r in detail.reports],
                         ["RPT-001", "RPT-002", "RPT-003"])
        # 每条原始证据（描述、图片摘要、上报人）完整保留
        by_id = {r.report_id: r for r in detail.reports}
        self.assertEqual(by_id["RPT-002"].image_digest, "sha256:RPT-002")
        self.assertEqual(by_id["RPT-003"].description, "积水散发异味")
        self.assertEqual(by_id["RPT-002"].reporter_id, "网格员-小王")

    def test_far_or_late_reports_create_separate_events(self):
        r1 = self.submit("RPT-001")
        # 500 米外：不同事件
        r2 = self.submit("RPT-002", latitude=LAT + 0.005)
        # 同一地点但超出 30 分钟时间窗（距最近一次上报 31 分钟）
        self.clock.advance(minutes=31)
        r3 = self.submit("RPT-003")

        ids = {r1.event.event_id, r2.event.event_id, r3.event.event_id}
        self.assertEqual(len(ids), 3)

    # -- 待补充 ----------------------------------------------------------

    def test_imprecise_coordinates_go_pending_supplement(self):
        # 室内信号差，精度 200 米
        result = self.submit("RPT-BAD", accuracy_m=200.0)
        self.assertEqual(result.event.status, EventStatus.PENDING_SUPPLEMENT)
        self.assertFalse(result.event.dispatchable)
        self.assertIsNone(result.event.latitude)

        with self.assertRaises(IncompleteEventError):
            self.svc.verify(result.event.event_id, operator="调度员", reason="电话核实")
        with self.assertRaises(IncompleteEventError):
            self.svc.reassign(result.event.event_id, department="市政排水所",
                              operator="调度员", reason="按辖区分派")

    def test_imprecise_report_does_not_aggregate(self):
        precise = self.submit("RPT-001")
        self.clock.advance(minutes=5)
        imprecise = self.submit("RPT-BAD", accuracy_m=300.0)
        self.assertNotEqual(precise.event.event_id, imprecise.event.event_id)

    def test_missing_description_then_supplement_promotes(self):
        r1 = self.submit("RPT-001", description="")
        self.assertEqual(r1.event.status, EventStatus.PENDING_SUPPLEMENT)

        self.clock.advance(minutes=8)
        r2 = self.submit("RPT-002", description="补充：积水没过脚踝")
        event = r2.event
        self.assertEqual(event.event_id, r1.event.event_id)
        self.assertEqual(event.status, EventStatus.PENDING)  # 补齐后自动转待核实

        actions = [l.action for l in self.svc.get_event_detail(event.event_id).timeline]
        self.assertIn(LogAction.SUPPLEMENTED, actions)
        # 现在可以核实了
        self.svc.verify(event.event_id, operator="调度员-李", reason="与网格员电话确认")

    # -- 幂等 ------------------------------------------------------------

    def test_duplicate_report_id_is_idempotent(self):
        first = self.submit("RPT-001")
        second = self.submit("RPT-001", description="重复提交，内容被忽略")
        self.assertTrue(second.deduplicated)
        self.assertEqual(second.event.event_id, first.event.event_id)
        detail = self.svc.get_event_detail(first.event.event_id)
        self.assertEqual(len(detail.reports), 1)
        self.assertEqual(detail.reports[0].description, "井盖周边积水约10厘米")

    def test_duplicate_operation_id_is_idempotent(self):
        event = self.submit("RPT-001").event
        r1 = self.svc.verify(event.event_id, operator="调度员-李",
                             reason="现场视频确认", op_id="OP-VERIFY-1")
        r2 = self.svc.verify(event.event_id, operator="调度员-李",
                             reason="现场视频确认", op_id="OP-VERIFY-1")
        self.assertFalse(r1.deduplicated)
        self.assertTrue(r2.deduplicated)
        self.assertEqual(r2.event.status, EventStatus.VERIFIED)
        logs = [l for l in self.svc.get_event_detail(event.event_id).timeline
                if l.action == LogAction.VERIFY]
        self.assertEqual(len(logs), 1)  # 只留痕一次

    # -- 状态机与留痕 ------------------------------------------------------

    def test_full_lifecycle_records_operator_and_reason(self):
        event = self.submit("RPT-001").event
        eid = event.event_id

        self.svc.verify(eid, operator="调度员-李", reason="与网格员视频核实")
        self.svc.reassign(eid, department="市政排水所",
                          operator="调度员-李", reason="按排水设施辖区分派")
        self.svc.close(eid, operator="市政排水所-张工", reason="清淤完成，积水消退")
        self.svc.reopen(eid, operator="调度员-李", reason="居民再次反映积水")

        detail = self.svc.get_event_detail(eid)
        self.assertEqual(detail.event.status, EventStatus.PENDING)
        actions = [l.action for l in detail.timeline]
        self.assertEqual(actions, [LogAction.CREATE, LogAction.VERIFY,
                                   LogAction.REASSIGN, LogAction.CLOSE,
                                   LogAction.REOPEN])
        for log in detail.timeline[1:]:
            self.assertTrue(log.operator)
            self.assertTrue(log.reason)
        self.assertEqual(detail.timeline[2].details["department"], "市政排水所")
        self.assertEqual(detail.event.department, "市政排水所")

    def test_invalid_transitions_and_missing_reason_rejected(self):
        event = self.submit("RPT-001").event
        eid = event.event_id
        with self.assertRaises(InvalidStateTransitionError):
            self.svc.reopen(eid, operator="调度员", reason="未关闭不能重开")
        with self.assertRaises(DomainError):
            self.svc.reassign(eid, department="", operator="调度员", reason="x")
        with self.assertRaises(DomainError):
            self.svc.verify(eid, operator="调度员", reason="")
        with self.assertRaises(DomainError):
            self.svc.verify(eid, operator="", reason="依据")

        self.svc.close(eid, operator="调度员", reason="误报")
        with self.assertRaises(InvalidStateTransitionError):
            self.svc.verify(eid, operator="调度员", reason="已关闭")

    # -- 合并 ------------------------------------------------------------

    def test_merge_keeps_evidence_and_marks_both_timelines(self):
        main = self.submit("RPT-001", severity=Severity.MAJOR).event
        dup = self.submit("RPT-002", latitude=LAT + 0.005,
                          severity=Severity.CRITICAL).event

        result = self.svc.merge(dup.event_id, main.event_id,
                                operator="调度员-李", reason="实为同一处井盖")
        self.assertEqual(result.event.status, EventStatus.MERGED)
        self.assertEqual(result.event.merged_into, main.event_id)
        # 目标事件吸收最高严重级别
        self.assertEqual(result.target_event.severity, Severity.CRITICAL)

        src = self.svc.get_event_detail(dup.event_id)
        tgt = self.svc.get_event_detail(main.event_id)
        self.assertEqual(len(src.reports), 1)  # 原始证据仍保留在源事件
        self.assertEqual(src.timeline[-1].details["merged_into"], main.event_id)
        self.assertEqual(tgt.timeline[-1].details["absorbed_from"], dup.event_id)

        # 已合并不再出现在未处理列表
        open_ids = {i.event_id for i in self.svc.list_unhandled()}
        self.assertNotIn(dup.event_id, open_ids)
        self.assertIn(main.event_id, open_ids)

    # -- 调度查询 ----------------------------------------------------------

    def test_dispatch_view_department_deadline_overdue(self):
        urgent = self.submit("RPT-001", severity=Severity.CRITICAL).event
        self.clock.advance(minutes=5)
        normal = self.submit("RPT-002", latitude=LAT + 0.01,
                             severity=Severity.MODERATE).event
        bad = self.submit("RPT-003", latitude=LAT + 0.02,
                          accuracy_m=500.0).event  # 待补充

        self.svc.verify(urgent.event_id, operator="调度员", reason="核实")
        self.svc.reassign(urgent.event_id, department="市政排水所",
                          operator="调度员", reason="紧急派单")

        items = {i.event_id: i for i in self.svc.list_unhandled()}
        self.assertEqual(set(items), {urgent.event_id, normal.event_id, bad.event_id})

        u = items[urgent.event_id]
        self.assertEqual(u.department, "市政排水所")
        self.assertEqual(u.deadline, u.created_at + timedelta(hours=2))  # 紧急 2 小时
        self.assertTrue(u.dispatchable)
        self.assertEqual(u.report_count, 1)

        n = items[normal.event_id]
        self.assertIsNone(n.department)
        self.assertEqual(n.deadline, n.created_at + timedelta(hours=24))

        b = items[bad.event_id]
        self.assertFalse(b.dispatchable)  # 待补充不能派发
        dispatchable_only = self.svc.list_unhandled(include_pending_supplement=False)
        self.assertNotIn(bad.event_id, {i.event_id for i in dispatchable_only})

        # 严重级别高的排在前面
        ordered = self.svc.list_unhandled()
        self.assertEqual(ordered[0].event_id, urgent.event_id)

        # 时钟越过紧急时限 -> 超时标记
        self.clock.advance(hours=3)
        overdue = {i.event_id: i.is_overdue for i in self.svc.list_unhandled()}
        self.assertTrue(overdue[urgent.event_id])
        self.assertFalse(overdue[normal.event_id])

        # 关闭后不再出现在未处理列表
        self.svc.close(normal.event_id, operator="调度员", reason="已处置")
        remaining = {i.event_id for i in self.svc.list_unhandled()}
        self.assertNotIn(normal.event_id, remaining)

    # -- 重启追溯 ----------------------------------------------------------

    def test_restart_preserves_aggregation_and_timeline(self):
        r1 = self.submit("RPT-001")
        self.clock.advance(minutes=6)
        r2 = self.submit("RPT-002", description="另一位居民补充")
        eid = r1.event.event_id
        self.svc.verify(eid, operator="调度员-李", reason="电话核实")
        self.svc.reassign(eid, department="市政排水所",
                          operator="调度员-李", reason="辖区分派")
        self.svc.shutdown()

        # 模拟服务重启：同一数据文件新建实例
        svc2 = HazardService(self.db_path, now=self.clock)
        try:
            detail = svc2.get_event_detail(eid)
            self.assertEqual([r.report_id for r in detail.reports],
                             ["RPT-001", "RPT-002"])  # 聚合关系保留
            self.assertEqual(detail.event.department, "市政排水所")
            self.assertEqual(detail.event.status, EventStatus.DISPATCHED)
            actions = [l.action for l in detail.timeline]
            self.assertEqual(actions, [LogAction.CREATE, LogAction.ATTACH,
                                       LogAction.VERIFY, LogAction.REASSIGN])
            # 重启后新上报仍能聚合到既有事件
            self.clock.advance(minutes=5)
            r3 = svc2.submit_report(
                report_id="RPT-003", reporter_id="网格员-老周",
                latitude=LAT, longitude=LNG, accuracy_m=5.0,
                reported_at=self.clock.t, description="雨后复查",
                image_digest="sha256:RPT-003", severity=Severity.MAJOR,
            )
            self.assertEqual(r3.event.event_id, eid)
        finally:
            svc2.shutdown()


if __name__ == "__main__":
    unittest.main()
