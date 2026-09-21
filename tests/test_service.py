"""积水隐患巡查服务的端到端测试（标准库 unittest，无外部依赖）。"""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.service import (  # noqa: E402
    CLOSED,
    CRITICAL,
    DISPATCHED,
    HIGH,
    InvalidTransition,
    LOW,
    MEDIUM,
    MERGED,
    PENDING,
    PENDING_SUPPLEMENT,
    VERIFIED,
    Service,
    ValidationError,
)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.svc = Service(":memory:")

    def tearDown(self):
        self.svc.shutdown()

    # -- 立案与待补充 --------------------------------------------------------

    def test_full_report_creates_pending_event(self):
        r = self.svc.report(
            location_name="幸福路与建设街交叉口",
            description="井盖周边积水约20厘米",
            image_summary="照片：积水淹没井盖边缘",
            severity=HIGH,
            lat=31.2304,
            lng=121.4737,
            coord_accuracy=10,
            reporter="网格员老周",
            report_id="rep-001",
        )
        self.assertTrue(r["created"])
        self.assertEqual(r["status"], PENDING)
        ev = self.svc.get_event(r["event_id"])
        self.assertEqual(ev["evidence_count"], 1)
        self.assertIsNone(ev["responsible_dept"])

    def test_missing_description_enters_supplement_and_cannot_dispatch(self):
        r = self.svc.report(
            location_name="幸福路井盖",
            description="   ",  # 描述缺失
            lat=31.2304,
            lng=121.4737,
            coord_accuracy=10,
            reporter="老周",
        )
        self.assertEqual(r["status"], PENDING_SUPPLEMENT)
        ev = self.svc.get_event(r["event_id"])
        self.assertIn("description", ev["missing_fields"])
        with self.assertRaises(InvalidTransition):
            self.svc.verify(r["event_id"], "调度员", "电话核实")
        with self.assertRaises(InvalidTransition):
            self.svc.dispatch(r["event_id"], "调度员", "立即派单", "市政排水所")
        # 出现在未处理清单，但不可派发
        board = {e["event_id"]: e for e in self.svc.list_unhandled()}
        self.assertIn(r["event_id"], board)
        self.assertFalse(board[r["event_id"]]["dispatchable"])

    def test_imprecise_coordinates_enter_supplement(self):
        r = self.svc.report(
            location_name="幸福路井盖",
            description="井盖周边积水",
            lat=31.2304,
            lng=121.4737,
            coord_accuracy=500,  # 精度不足（超过 100m 上限）
            reporter="老周",
        )
        self.assertEqual(r["status"], PENDING_SUPPLEMENT)
        ev = self.svc.get_event(r["event_id"])
        self.assertIn("coordinates", ev["missing_fields"])
        self.assertIsNone(ev["lat"])  # 不可信坐标不写入事件定位

    def test_no_coordinates_at_all_also_supplement(self):
        r = self.svc.report(
            location_name="幸福路井盖",
            description="井盖周边积水",
            reporter="老周",
        )
        self.assertEqual(r["status"], PENDING_SUPPLEMENT)

    def test_supplement_unblocks_event(self):
        r = self.svc.report(
            location_name="幸福路井盖",
            description="",
            lat=31.2304,
            lng=121.4737,
            coord_accuracy=10,
            reporter="老周",
        )
        out = self.svc.supplement(
            r["event_id"], "老周", "居民微信群补充了现场描述",
            description="井盖周边积水约20厘米，有异味",
        )
        self.assertEqual(out["status"], PENDING)
        # 原始证据保留且描述已更新
        evidence = self.svc.list_evidence(r["event_id"])
        self.assertEqual(len(evidence), 1)
        self.assertIn("异味", evidence[0]["description"])
        # 补充必须给依据
        with self.assertRaises(ValidationError):
            self.svc.supplement(r["event_id"], "老周", " ", description="x")

    def test_supplement_new_report_completes_event(self):
        # 第一条缺坐标，第二条同地点上报带着精确坐标 -> 自动补齐
        r1 = self.svc.report(
            location_name="幸福路井盖",
            description="井盖周边积水",
            reporter="老周",
        )
        self.assertEqual(r1["status"], PENDING_SUPPLEMENT)
        r2 = self.svc.report(
            location_name="幸福路井盖",
            description="积水仍未退",
            lat=31.2304,
            lng=121.4737,
            coord_accuracy=8,
            reporter="网格员小李",
        )
        self.assertTrue(r2["aggregated"])
        self.assertEqual(r2["event_id"], r1["event_id"])
        self.assertEqual(r2["status"], PENDING)
        ev = self.svc.get_event(r1["event_id"])
        self.assertEqual(ev["evidence_count"], 2)
        self.assertEqual(ev["lat"], 31.2304)

    # -- 聚合与幂等 ----------------------------------------------------------

    def test_same_location_short_window_aggregates(self):
        base = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
        r1 = self.svc.report(
            location_name="幸福路与建设街交叉口井盖",
            description="井盖周边积水",
            image_summary="远景照片",
            severity=MEDIUM,
            lat=31.2304, lng=121.4737, coord_accuracy=10,
            reported_at=base, reporter="老周", report_id="rep-A",
        )
        r2 = self.svc.report(
            location_name="幸福路与建设街交叉口，井盖",  # 标点归一化后同名
            description="水更深了",
            image_summary="近景照片",
            severity=HIGH,
            lat=31.2305, lng=121.4738, coord_accuracy=10,
            reported_at=base + timedelta(minutes=40),
            reporter="小李", report_id="rep-B",
        )
        r3 = self.svc.report(
            location_name="幸福路与建设街交叉口井盖",
            description="第三位居民反映",
            severity=LOW,
            lat=31.2304, lng=121.4737, coord_accuracy=10,
            reported_at=base + timedelta(minutes=70),
            reporter="居民转述", report_id="rep-C",
        )
        self.assertEqual(r2["event_id"], r1["event_id"])
        self.assertEqual(r3["event_id"], r1["event_id"])
        ev = self.svc.get_event(r1["event_id"])
        self.assertEqual(ev["evidence_count"], 3)
        self.assertEqual(ev["severity"], HIGH)  # 取最高级别
        reporters = {e["reporter"] for e in ev["evidence"]}
        self.assertEqual(reporters, {"老周", "小李", "居民转述"})

    def test_outside_window_not_aggregated(self):
        base = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
        r1 = self.svc.report(
            location_name="幸福路井盖", description="积水",
            lat=31.23, lng=121.47, coord_accuracy=10,
            reported_at=base, reporter="老周",
        )
        r2 = self.svc.report(
            location_name="幸福路井盖", description="积水",
            lat=31.23, lng=121.47, coord_accuracy=10,
            reported_at=base + timedelta(hours=3), reporter="老周",
        )
        self.assertNotEqual(r1["event_id"], r2["event_id"])

    def test_far_location_not_aggregated(self):
        r1 = self.svc.report(
            location_name="一号井盖", description="积水",
            lat=31.2304, lng=121.4737, coord_accuracy=10, reporter="老周",
        )
        r2 = self.svc.report(
            location_name="二号井盖", description="积水",
            lat=31.2400, lng=121.4900, coord_accuracy=10, reporter="老周",
        )
        self.assertNotEqual(r1["event_id"], r2["event_id"])

    def test_coordinate_proximity_aggregates_without_name(self):
        r1 = self.svc.report(
            description="积水", lat=31.2304, lng=121.4737,
            coord_accuracy=10, reporter="老周",
        )
        r2 = self.svc.report(
            description="还在积水", lat=31.23045, lng=121.47375,  # 约 7m
            coord_accuracy=10, reporter="小李",
        )
        self.assertEqual(r1["event_id"], r2["event_id"])

    def test_report_id_idempotent(self):
        kw = dict(
            location_name="幸福路井盖", description="积水",
            lat=31.23, lng=121.47, coord_accuracy=10, reporter="老周",
        )
        r1 = self.svc.report(report_id="dup-1", **kw)
        r2 = self.svc.report(report_id="dup-1", **kw)
        self.assertFalse(r2["created"])
        self.assertEqual(r1["event_id"], r2["event_id"])
        ev = self.svc.get_event(r1["event_id"])
        self.assertEqual(ev["evidence_count"], 1)  # 没有重复证据

    def test_explicit_event_id_appends_evidence(self):
        r1 = self.svc.report(
            location_name="幸福路井盖", description="积水",
            lat=31.23, lng=121.47, coord_accuracy=10, reporter="老周",
        )
        r2 = self.svc.report(
            event_id=r1["event_id"], description="积水加重",
            reporter="小李", severity=CRITICAL,
        )
        self.assertEqual(r2["event_id"], r1["event_id"])
        self.assertEqual(self.svc.get_event(r1["event_id"])["severity"], CRITICAL)

    # -- 状态流转与时间线 -----------------------------------------------------

    def test_verify_dispatch_transfer_close_reopen_lifecycle(self):
        r = self.svc.report(
            location_name="幸福路井盖", description="井盖周边积水约20cm",
            image_summary="照片", severity=CRITICAL,
            lat=31.2304, lng=121.4737, coord_accuracy=10, reporter="老周",
        )
        eid = r["event_id"]

        v = self.svc.verify(eid, "值班长王敏", "现场视频确认井盖冒水")
        self.assertEqual(v["status"], VERIFIED)

        d = self.svc.dispatch(eid, "调度员张强", "紧急工单 W20260921-1", "市政排水所")
        self.assertEqual(d["status"], DISPATCHED)
        self.assertEqual(d["responsible_dept"], "市政排水所")
        # 紧急级别时限 2 小时
        self.assertAlmostEqual(d["seconds_left"], 2 * 3600, delta=5)
        self.assertFalse(d["overdue"])

        t = self.svc.transfer(
            eid, "调度员张强", "现场判定为供水井盖，非排水设施", "自来水公司抢修队"
        )
        self.assertEqual(t["responsible_dept"], "自来水公司抢修队")
        self.assertEqual(t["deadline"], d["deadline"])  # 转派不重新计时

        c = self.svc.close_event(
            eid, "抢修队赵磊", "抽水完毕并更换防沉降垫圈，现场无积水",
            outcome="已修复",
        )
        self.assertEqual(c["status"], CLOSED)
        with self.assertRaises(InvalidTransition):
            self.svc.verify(eid, "x", "y")

        ro = self.svc.reopen(eid, "网格员老周", "居民反映雨后再次积水")
        self.assertEqual(ro["status"], PENDING)
        self.assertIsNone(ro["responsible_dept"])
        self.assertIsNone(ro["deadline"])

        actions = [x["action"] for x in self.svc.event_timeline(eid)]
        self.assertEqual(
            actions,
            ["CREATED", "VERIFIED", "DISPATCHED", "TRANSFERRED",
             "CLOSED", "REOPENED"],
        )
        for item in self.svc.event_timeline(eid):
            self.assertTrue(item["operator"])
            self.assertTrue(item["basis"])

    def test_dispatch_requires_operator_and_basis(self):
        r = self.svc.report(
            location_name="幸福路井盖", description="积水",
            lat=31.23, lng=121.47, coord_accuracy=10, reporter="老周",
        )
        with self.assertRaises(ValidationError):
            self.svc.dispatch(r["event_id"], "", "依据", "排水所")
        with self.assertRaises(ValidationError):
            self.svc.dispatch(r["event_id"], "调度员", "", "排水所")
        with self.assertRaises(ValidationError):
            self.svc.dispatch(r["event_id"], "调度员", "依据", "")

    def test_overdue_flag(self):
        r = self.svc.report(
            location_name="幸福路井盖", description="积水", severity=LOW,
            lat=31.23, lng=121.47, coord_accuracy=10, reporter="老周",
            reported_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        self.svc.verify(r["event_id"], "王敏", "核实")
        d = self.svc.dispatch(r["event_id"], "张强", "工单", "排水所")
        self.assertTrue(d["overdue"])
        self.assertLess(d["seconds_left"], 0)

    # -- 合并 ----------------------------------------------------------------

    def test_merge_keeps_all_evidence_and_traceable_relation(self):
        a = self.svc.report(
            location_name="幸福路东段井盖", description="积水",
            lat=31.2304, lng=121.4737, coord_accuracy=10,
            reporter="老周", report_id="ra",
        )
        b = self.svc.report(
            location_name="幸福路88号门口井盖", description="积水",
            lat=31.2600, lng=121.5000, coord_accuracy=10,
            reporter="小李", report_id="rb",
        )
        out = self.svc.merge(
            b["event_id"], a["event_id"],
            operator="值班长王敏", basis="现场核对为同一处井盖，定位偏差",
        )
        self.assertEqual(out["event_id"], a["event_id"])
        self.assertEqual(out["evidence_count"], 2)
        self.assertIn(b["event_id"], out["merged_from"])

        src = self.svc.get_event(b["event_id"])
        self.assertEqual(src["status"], MERGED)
        self.assertEqual(src["merged_into"], a["event_id"])

        # 目标事件时间线包含合并记录；被合并事件自身时间线也留痕
        target_actions = [x["action"] for x in self.svc.event_timeline(a["event_id"])]
        self.assertIn("MERGE_RECEIVED", target_actions)
        source_actions = [
            x["action"]
            for x in self.svc.event_timeline(b["event_id"], include_merged=False)
        ]
        self.assertEqual(source_actions, ["CREATED", "MERGED"])

        # 原始证据仍可按被合并事件编号溯源
        self.assertEqual(len(self.svc.list_evidence(b["event_id"])), 1)
        with self.assertRaises(InvalidTransition):
            self.svc.verify(b["event_id"], "王敏", "尝试核实已合并事件")

    # -- 调度查询 -------------------------------------------------------------

    def test_dispatch_queries(self):
        # 紧急（待核实后派发）
        r1 = self.svc.report(
            location_name="A点", description="积水", severity=CRITICAL,
            lat=31.23, lng=121.47, coord_accuracy=10, reporter="老周",
        )
        # 低级（不派发，留在待处理）
        self.svc.report(
            location_name="B点", description="积水", severity=LOW,
            lat=31.24, lng=121.48, coord_accuracy=10, reporter="小李",
        )
        # 待补充
        self.svc.report(location_name="C点", description="积水", reporter="小王")

        unhandled = self.svc.list_unhandled()
        self.assertEqual(len(unhandled), 3)
        self.assertEqual(unhandled[0]["event_id"], r1["event_id"])  # 紧急排最前
        self.assertEqual(unhandled[0]["severity"], CRITICAL)

        self.svc.verify(r1["event_id"], "王敏", "核实")
        self.svc.dispatch(r1["event_id"], "张强", "工单-1", "排水所")
        board = self.svc.dispatch_board()
        self.assertEqual(len(board), 1)
        self.assertEqual(board[0]["responsible_dept"], "排水所")
        self.assertIsNotNone(board[0]["deadline"])

        unhandled = self.svc.list_unhandled()
        self.assertNotIn(r1["event_id"], [e["event_id"] for e in unhandled])

    # -- 持久化 ---------------------------------------------------------------

    def test_restart_preserves_aggregation_and_full_timeline(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "patrol.db")
            svc = Service(path)
            r1 = svc.report(
                location_name="幸福路井盖", description="积水",
                image_summary="图1", severity=MEDIUM,
                lat=31.2304, lng=121.4737, coord_accuracy=10,
                reporter="老周", report_id="persist-1",
            )
            r2 = svc.report(
                location_name="幸福路井盖", description="积水加重",
                severity=HIGH,
                lat=31.2305, lng=121.4738, coord_accuracy=10,
                reporter="小李", report_id="persist-2",
            )
            svc.verify(r1["event_id"], "王敏", "视频核实")
            svc.dispatch(r1["event_id"], "张强", "工单-9", "排水所")
            svc.transfer(r1["event_id"], "张强", "属供水设施", "自来水公司")
            timeline_before = svc.event_timeline(r1["event_id"])
            svc.shutdown()

            # 模拟服务重启
            svc2 = Service(path)
            ev = svc2.get_event(r1["event_id"])
            self.assertEqual(ev["status"], DISPATCHED)
            self.assertEqual(ev["responsible_dept"], "自来水公司")
            self.assertEqual(ev["evidence_count"], 2)
            self.assertEqual(ev["severity"], HIGH)
            self.assertEqual(
                [x["report_id"] for x in ev["evidence"]],
                ["persist-1", "persist-2"],
            )
            self.assertEqual(
                [x["action"] for x in timeline_before],
                [x["action"] for x in svc2.event_timeline(r1["event_id"])],
            )
            # 重启后幂等键仍然生效
            again = svc2.report(
                report_id="persist-1", location_name="幸福路井盖",
                description="积水", lat=31.2304, lng=121.4737,
                coord_accuracy=10, reporter="老周",
            )
            self.assertEqual(again["event_id"], r1["event_id"])
            self.assertEqual(
                svc2.get_event(r1["event_id"])["evidence_count"], 2
            )
            # 调度看板可恢复
            self.assertEqual(len(svc2.dispatch_board()), 1)
            svc2.shutdown()
            self.assertEqual(r2["event_id"], r1["event_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
