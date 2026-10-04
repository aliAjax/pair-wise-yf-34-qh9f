import sys, tempfile, threading, unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import ApiError, DroneAirspaceService, iso, utcnow
from recon import adjudication


class ReconCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.svc = DroneAirspaceService(self.db)
        self.start = utcnow() + timedelta(hours=2)
        self.end = self.start + timedelta(hours=1)

    def tearDown(self): self.tmp.cleanup()

    def make_plan(self, callsign="D100", operator="OP1", user="op-user", route=None):
        plan = self.svc.create_plan(user, "operator", operator, {
            "callsign": callsign, "drone_model": "M400", "payload_kg": 5,
            "route": route or [[116.1, 39.8], [116.3, 39.9]],
            "starts_at": iso(self.start), "ends_at": iso(self.end),
            "max_altitude": 100, "population_risk": 1, "emergency_plan": "返回起降点", "region": "BJ"})
        self.svc.submit(plan["id"], user, "operator", operator, {})
        self.svc.approve(plan["id"], "reviewer", "airspace_reviewer",
                         {"expected_revision": 1, "offline_id": f"appr-{callsign}-1", "reason": "合规"})
        return plan["id"]

    def make_batch(self, plan_ids, code="B-001", **extra):
        body = {"code": code, "title": "十月监管抽查", "plan_ids": plan_ids}
        body.update(extra)
        return self.svc.recon.create_batch("aud-1", "auditor", body)

    def item_of(self, batch, plan_id):
        for result in batch["results"]:
            if result["plan_id"] == plan_id: return result["item_id"]
        raise AssertionError(f"批次里没有计划 {plan_id} 的条目: {batch['results']}")

    def receipt(self, offline_id, plan_id, takeoff=None, landing=None, **extra):
        entry = {"offline_id": offline_id, "plan_id": plan_id,
                 "actual_takeoff": iso(takeoff or self.start), "actual_landing": iso(landing or self.end)}
        entry.update(extra)
        return entry

    def ingest(self, batch_id, entries, operator="OP1", user="op-user"):
        return self.svc.recon.ingest_receipts(user, "operator", operator, batch_id, {"entries": entries})

    def detail(self, item_id):
        return self.svc.recon.item_detail("auditor", "", item_id)

    def notices(self, kind=None):
        rows = self.svc.notifications("aud-1", "auditor", "")["notifications"]
        return [n for n in rows if kind is None or n["kind"] == kind]


class ReceiptFlowTest(ReconCase):
    def test_matched_receipt_auto_reconciles_and_replay_records_once(self):
        pid = self.make_plan()
        batch = self.make_batch([pid])
        item_id = self.item_of(batch, pid)
        self.assertEqual(batch["results"][0]["result"], "created")
        first = self.ingest(batch["batch"]["id"], [self.receipt("rc-1", pid, takeoff=self.start + timedelta(minutes=10),
                                                                landing=self.end - timedelta(minutes=5))])
        self.assertEqual(first["results"][0]["result"], "recorded")
        item = self.detail(item_id)["item"]
        self.assertEqual(item["status"], "reconciled")
        self.assertEqual(item["conclusion_kind"], "auto_matched")
        replay = self.ingest(batch["batch"]["id"], [self.receipt("rc-1", pid)])
        self.assertEqual(replay["results"][0]["result"], "duplicate")
        self.assertEqual(len(self.detail(item_id)["receipts"]), 1)  # 重放只记一次

    def test_deviation_waits_for_auditor_and_deducts_once(self):
        pid = self.make_plan()
        batch = self.make_batch([pid])
        item_id = self.item_of(batch, pid)
        self.ingest(batch["batch"]["id"], [self.receipt("rc-dev", pid, takeoff=self.start + timedelta(minutes=10),
                                                        landing=self.end + timedelta(minutes=30))])
        self.assertEqual(self.detail(item_id)["item"]["status"], "deviation")
        console = self.svc.recon.console("auditor", "")
        self.assertEqual([i["id"] for i in console["pending_review"]], [item_id])
        sheet = self.svc.recon.adjudicate("aud-1", "auditor", batch["batch"]["id"],
                                          {"sheet_offline_id": "sheet-1",
                                           "entries": [{"item_id": item_id, "verdict": "violation", "note": "超时飞行"}]})
        entry = sheet["results"][0]
        self.assertEqual(entry["result"], "concluded")
        self.assertEqual(entry["deduction"], adjudication.POINTS["deviation"])
        replay = self.svc.recon.adjudicate("aud-1", "auditor", batch["batch"]["id"],
                                           {"sheet_offline_id": "sheet-1",
                                            "entries": [{"item_id": item_id, "verdict": "violation"}]})
        self.assertTrue(replay["idempotent"])
        self.assertEqual(len(self.detail(item_id)["deductions"]), 1)  # 裁决单重放不重复扣分
        self.assertEqual(len(self.notices("deduction")), 1)

    def test_conflicting_receipts_kept_for_auditor_decision(self):
        pid = self.make_plan()
        batch = self.make_batch([pid])
        item_id = self.item_of(batch, pid)
        ra = self.ingest(batch["batch"]["id"], [self.receipt("rc-a", pid, takeoff=self.start + timedelta(minutes=10),
                                                             landing=self.end - timedelta(minutes=10))])["results"][0]
        self.assertEqual(self.detail(item_id)["item"]["status"], "reconciled")
        rb = self.ingest(batch["batch"]["id"], [self.receipt("rc-b", pid, takeoff=self.start + timedelta(minutes=40),
                                                             landing=self.end - timedelta(minutes=5))])["results"][0]
        item = self.detail(item_id)
        self.assertEqual(item["item"]["status"], "contested")  # 冲突的实际起降两版都保留
        self.assertEqual(len(item["receipts"]), 2)
        self.assertTrue(all(r["standing"] == "candidate" for r in item["receipts"]))
        sheet = self.svc.recon.adjudicate("aud-1", "auditor", batch["batch"]["id"],
                                          {"sheet_offline_id": "sheet-c1",
                                           "entries": [{"item_id": item_id, "verdict": "violation",
                                                        "accept_receipt_id": rb["receipt_id"], "note": "以后到遥测为准"}]})
        self.assertEqual(sheet["results"][0]["deduction"], adjudication.POINTS["contested"])
        settled = self.detail(item_id)
        self.assertEqual(settled["item"]["status"], "reconciled")
        self.assertEqual(settled["item"]["accepted_receipt_id"], rb["receipt_id"])
        standing = {r["id"]: r["standing"] for r in settled["receipts"]}
        self.assertEqual(standing[rb["receipt_id"]], "accepted")
        self.assertEqual(standing[ra["receipt_id"]], "rejected")

    def test_failed_entries_retry_without_reprocessing(self):
        pid = self.make_plan()
        batch = self.make_batch([pid])
        entries = [self.receipt("rc-1", pid), {"plan_id": pid}, self.receipt("rc-2", pid)]
        first = self.ingest(batch["batch"]["id"], entries)
        self.assertEqual([r["result"] for r in first["results"]], ["recorded", "error", "recorded"])
        self.assertEqual(first["results"][1]["error"], "offline_id_required")
        retry = self.ingest(batch["batch"]["id"], entries)  # 写入失败后只重试没有处理的条目
        self.assertEqual([r["result"] for r in retry["results"]], ["duplicate", "error", "duplicate"])
        item_id = self.item_of(batch, pid)
        self.assertEqual(len(self.detail(item_id)["receipts"]), 2)
        other = self.ingest(batch["batch"]["id"], [self.receipt("rc-9", pid)], operator="OP2", user="op2-user")
        self.assertEqual(other["results"][0]["error"], "forbidden_operator")

    def test_receipt_against_changed_revision_lands_on_batch_approval_version(self):
        pid = self.make_plan()
        batch = self.make_batch([pid])
        item_id = self.item_of(batch, pid)
        self.svc.change(pid, "op-user", "operator", "OP1", {"expected_revision": 1, "route": [[116.12, 39.82], [116.32, 39.92]]})
        self.svc.submit(pid, "op-user", "operator", "OP1", {})
        self.svc.approve(pid, "reviewer", "airspace_reviewer",
                         {"expected_revision": 2, "offline_id": "appr-d100-2", "reason": "变更后合规"})
        result = self.ingest(batch["batch"]["id"], [self.receipt("rc-old", pid, plan_revision=2)])["results"][0]
        self.assertEqual(result["result"], "recorded")
        self.assertEqual(result["item_id"], item_id)  # 回执落到批次锁定的批准版本
        self.assertTrue(result["revision_mismatch"])  # 但标记它对应的是改过的计划版本
        detail = self.detail(item_id)
        self.assertEqual(detail["item"]["plan_revision"], 1)
        self.assertEqual(detail["receipts"][0]["plan_revision_reported"], 2)


class AdjudicationRaceTest(ReconCase):
    def contested_item(self):
        pid = self.make_plan()
        batch = self.make_batch([pid])
        item_id = self.item_of(batch, pid)
        ra = self.ingest(batch["batch"]["id"], [self.receipt("rc-a", pid, takeoff=self.start + timedelta(minutes=10))])["results"][0]
        rb = self.ingest(batch["batch"]["id"], [self.receipt("rc-b", pid, takeoff=self.start + timedelta(minutes=40))])["results"][0]
        self.assertEqual(self.detail(item_id)["item"]["status"], "contested")
        return batch["batch"]["id"], item_id, ra["receipt_id"], rb["receipt_id"]

    def test_first_write_wins_and_loser_sees_current_conclusion(self):
        batch_id, item_id, _, rb = self.contested_item()
        first = self.svc.recon.adjudicate("aud-1", "auditor", batch_id,
                                          {"sheet_offline_id": "sheet-w1",
                                           "entries": [{"item_id": item_id, "verdict": "violation", "accept_receipt_id": rb}]})
        self.assertEqual(first["results"][0]["result"], "concluded")
        second = self.svc.recon.adjudicate("aud-2", "auditor", batch_id,
                                           {"sheet_offline_id": "sheet-w2",
                                            "entries": [{"item_id": item_id, "verdict": "cleared", "accept_receipt_id": rb}]})
        loser = second["results"][0]
        self.assertEqual(loser["result"], "conflict")
        self.assertEqual(loser["current"]["concluded_by"], "aud-1")  # 后到的人看到当前结论
        self.assertEqual(loser["current"]["conclusion_kind"], "violation")
        self.assertEqual(len(self.detail(item_id)["deductions"]), 1)

    def test_concurrent_adjudication_only_one_sheet_wins(self):
        batch_id, item_id, ra, rb = self.contested_item()
        barrier, outcomes = threading.Barrier(2), []

        def conclude(name, sheet, accept):
            barrier.wait(timeout=5)
            try:
                outcomes.append(self.svc.recon.adjudicate(name, "auditor", batch_id,
                                                          {"sheet_offline_id": sheet,
                                                           "entries": [{"item_id": item_id, "verdict": "violation",
                                                                        "accept_receipt_id": accept, "note": name}]}))
            except Exception as exc:  # noqa: BLE001 - 测试里直接失败
                outcomes.append({"error": exc})

        threads = [threading.Thread(target=conclude, args=("aud-1", "sheet-t1", rb)),
                   threading.Thread(target=conclude, args=("aud-2", "sheet-t2", ra))]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(outcomes), 2)
        results = [o["results"][0] for o in outcomes]
        concluded = [r for r in results if r["result"] == "concluded"]
        conflicts = [r for r in results if r["result"] == "conflict"]
        self.assertEqual(len(concluded), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertIn(conflicts[0]["current"]["concluded_by"], {"aud-1", "aud-2"})
        self.assertEqual(len(self.detail(item_id)["deductions"]), 1)  # 并发下也只扣一次


class InvalidationTest(ReconCase):
    def test_plan_change_invalidates_item_and_evidence_flow(self):
        pid = self.make_plan()
        batch = self.make_batch([pid])
        item_id = self.item_of(batch, pid)
        self.svc.change(pid, "op-user", "operator", "OP1", {"expected_revision": 1, "route": [[116.12, 39.82], [116.32, 39.92]]})
        item = self.detail(item_id)["item"]
        self.assertEqual(item["status"], "invalidated")
        self.assertIsNotNone(item["evidence_deadline"])  # 补证期限
        kinds = [n["kind"] for n in self.notices()]
        self.assertIn("approval_invalidated", kinds)
        self.assertIn("evidence_required", kinds)
        console = self.svc.recon.console("operator", "OP1")
        self.assertEqual([i["id"] for i in console["evidence_deadlines"]], [item_id])
        self.assertFalse(console["evidence_deadlines"][0]["overdue"])
        self.ingest(batch["batch"]["id"], [{"offline_id": "sup-1", "plan_id": pid, "kind": "supplement", "note": "补充空域协调单"}])
        self.assertEqual(self.detail(item_id)["item"]["status"], "evidence_submitted")
        console = self.svc.recon.console("auditor", "")
        self.assertEqual([i["id"] for i in console["pending_review"]], [item_id])
        sheet = self.svc.recon.adjudicate("aud-1", "auditor", batch["batch"]["id"],
                                          {"sheet_offline_id": "sheet-e1", "entries": [{"item_id": item_id, "verdict": "cleared"}]})
        self.assertEqual(sheet["results"][0]["deduction"], 0)
        self.assertEqual(self.detail(item_id)["item"]["status"], "reconciled")

    def test_restriction_create_and_change_invalidate_approval(self):
        pid = self.make_plan()
        batch = self.make_batch([pid])
        item_id = self.item_of(batch, pid)
        far = self.svc.create_restriction("reviewer", "airspace_reviewer", {
            "name": "远端限制", "kind": "temporary_limit", "min_lon": 117.0, "min_lat": 40.0, "max_lon": 117.2, "max_lat": 40.2,
            "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start), "ends_at": iso(self.end), "reason": "无关区域"})
        self.assertEqual(self.svc.get_plan(pid, "auditor")["status"], "approved")  # 不重叠的限制不影响批准
        self.svc.change_restriction(far["id"], "reviewer", "airspace_reviewer", {
            "min_lon": 116.15, "min_lat": 39.85, "max_lon": 116.25, "max_lat": 39.95,
            "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.end + timedelta(minutes=30))})
        self.assertEqual(self.svc.get_plan(pid, "auditor")["status"], "draft")  # 限制变化让原批准失效
        self.assertEqual(self.detail(item_id)["item"]["status"], "invalidated")
        self.assertEqual(len(self.notices("approval_invalidated")), 1)
        self.svc.change_restriction(far["id"], "reviewer", "airspace_reviewer", {"reason": "调整边界"})
        self.assertEqual(len(self.notices("approval_invalidated")), 1)  # 已失效不重复通知
        self.svc.submit(pid, "op-user", "operator", "OP1", {})
        self.svc.approve(pid, "commander", "commander", {"expected_revision": 1, "offline_id": "appr-d100-1r",
                                                         "reason": "紧急任务", "override_reason": "应急授权"})
        self.svc.create_restriction("reviewer", "airspace_reviewer", {
            "name": "新增禁飞", "kind": "no_fly", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.4, "max_lat": 40.0,
            "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start), "ends_at": iso(self.end), "reason": "重大活动"})
        self.assertEqual(self.svc.get_plan(pid, "auditor")["status"], "draft")  # 新增限制同样让批准失效
        self.assertEqual(len(self.notices("approval_invalidated")), 2)


class SweepAndRestartTest(ReconCase):
    def test_sweep_restart_and_replay_never_duplicate_effects(self):
        pid = self.make_plan()
        batch = self.make_batch([pid], code="B-901", receipt_deadline=iso(utcnow() - timedelta(minutes=1)))
        item_id = self.item_of(batch, pid)
        first = self.svc.recon.sweep("aud-1", "auditor")
        self.assertEqual(first["no_show"], 1)
        self.assertEqual(self.svc.recon.sweep("aud-1", "auditor")["no_show"], 0)  # 已处理的不重复处理
        self.assertEqual(len(self.notices("no_show")), 1)
        sheet = self.svc.recon.adjudicate("aud-1", "auditor", batch["batch"]["id"],
                                          {"sheet_offline_id": "sheet-9", "entries": [{"item_id": item_id, "verdict": "violation"}]})
        self.assertEqual(sheet["results"][0]["deduction"], adjudication.POINTS["no_show"])
        replay = self.svc.recon.adjudicate("aud-1", "auditor", batch["batch"]["id"],
                                           {"sheet_offline_id": "sheet-9", "entries": [{"item_id": item_id, "verdict": "violation"}]})
        self.assertTrue(replay["idempotent"])

        pid2 = self.make_plan("D300", route=[[117.1, 40.0], [117.3, 40.1]])
        batch2 = self.make_batch([pid2], code="B-902")
        item2 = self.item_of(batch2, pid2)
        self.svc.change(pid2, "op-user", "operator", "OP1", {"expected_revision": 1, "route": [[116.12, 39.82], [116.32, 39.92]]})
        self.svc.repo.conn.execute("UPDATE recon_items SET evidence_deadline=? WHERE id=?",
                                   (iso(utcnow() - timedelta(hours=1)), item2))  # 白盒：把补证期限拨到过去
        self.assertEqual(self.svc.recon.sweep("aud-1", "auditor")["evidence_reminders"], 1)
        self.assertEqual(len(self.notices("evidence_overdue")), 1)

        restarted = DroneAirspaceService(self.db)  # 服务中断再启动：同一数据库重新拉起
        self.assertEqual(restarted.recon.sweep("aud-1", "auditor"), {"no_show": 0, "evidence_reminders": 1})
        again = restarted.recon.adjudicate("aud-1", "auditor", batch["batch"]["id"],
                                           {"sheet_offline_id": "sheet-9", "entries": [{"item_id": item_id, "verdict": "violation"}]})
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(restarted.recon.item_detail("auditor", "", item_id)["deductions"]), 1)  # 不重复扣分
        self.assertEqual(len(self.notices("no_show")), 1)  # 不重复发通知
        self.assertEqual(len(self.notices("deduction")), 1)
        self.assertEqual(len(self.notices("evidence_overdue")), 1)


class ConsoleViewTest(ReconCase):
    def test_console_buckets_and_role_visibility(self):
        p1 = self.make_plan("D100", operator="OP1", user="op-user")
        p2 = self.make_plan("D200", operator="OP2", user="op2-user", route=[[117.1, 40.0], [117.3, 40.1]])
        batch = self.make_batch([p1, p2])
        item1, item2 = self.item_of(batch, p1), self.item_of(batch, p2)
        self.ingest(batch["batch"]["id"], [self.receipt("rc-1", p1, takeoff=self.start + timedelta(minutes=5),
                                                        landing=self.end - timedelta(minutes=5))])
        console = self.svc.recon.console("auditor", "")
        self.assertEqual([i["id"] for i in console["reconciled"]], [item1])
        self.assertEqual([i["id"] for i in console["awaiting_receipts"]], [item2])
        self.assertEqual(console["summary"], {"pending_review": 0, "reconciled": 1, "evidence_deadlines": 0, "awaiting_receipts": 1})
        op1 = self.svc.recon.console("operator", "OP1")
        self.assertEqual({i["id"] for i in op1["reconciled"]}, {item1})
        self.assertEqual(op1["awaiting_receipts"], [])  # 运营方只看自己的条目
        op2 = self.svc.recon.console("operator", "OP2")
        self.assertEqual({i["id"] for i in op2["awaiting_receipts"]}, {item2})
        with self.assertRaises(ApiError) as ctx:
            self.svc.recon.console("viewer", "")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError):
            self.svc.recon.item_detail("operator", "OP2", item1)  # 跨运营方不可见
        batches = self.svc.recon.list_batches("operator", "OP2")["batches"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["item_count"], 1)


if __name__ == "__main__": unittest.main()
