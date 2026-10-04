import sys, tempfile, threading, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DroneAirspaceService, iso, utcnow
from recon import ReconciliationService
from recon.judge import CONFLICT, INVALIDATED, MISMATCH, PENDING, VERIFIED


class ReconTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "recon.db"
        self.svc = DroneAirspaceService(self.db)
        self.recon = ReconciliationService(self.db)
        self.start = utcnow() + timedelta(hours=2)

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers --------------------------------------------------------------
    def plan(self, callsign="D200", route=None):
        return self.svc.create_plan("op-user", "operator", "OP1", {
            "callsign": callsign, "drone_model": "M400", "payload_kg": 5,
            "route": route or [[116.1, 39.8], [116.3, 39.9]],
            "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)),
            "max_altitude": 100, "population_risk": 1, "emergency_plan": "返回起降点", "region": "BJ"})

    def approve(self, plan, offline_id="off-1", revision=None):
        submitted = self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})["plan"]
        return self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {
            "expected_revision": submitted["revision"] if revision is None else revision,
            "offline_id": offline_id, "reason": "满足要求"})

    def batch(self, code=None):
        return self.recon.create_batch("auditor", "auditor", {"batch_code": code})

    def receipt(self, batch_id, plan, offline_id, revision=None, result="completed",
                 takeoff=None, landing=None, operator="OP1"):
        takeoff = iso(self.start + timedelta(minutes=10)) if takeoff is None else takeoff
        landing = iso(self.start + timedelta(minutes=50)) if landing is None else landing
        return self.recon.submit_receipt(batch_id, "op-user", "operator", operator, {
            "offline_id": offline_id, "plan_id": plan["id"],
            "plan_revision": revision if revision is not None else plan["revision"],
            "actual_takeoff_at": takeoff, "actual_landing_at": landing, "result": result})

    def conclude(self, batch_id, actor="auditor", role="auditor"):
        return self.recon.conclude_batch(batch_id, actor, role, {})

    def conclusions(self, batch_id):
        return self.recon.get_batch(batch_id, "auditor")["conclusions"]

    def deductions(self, plan_id):
        return [dict(r) for r in self.recon.store.conn.execute(
            "SELECT * FROM deductions WHERE plan_id=?", (plan_id,))]

    def notif_count(self, plan_id):
        return self.recon.store.conn.execute(
            "SELECT COUNT(*) c FROM notifications WHERE plan_id=? AND dedup_key LIKE 'recon:%'", (plan_id,)).fetchone()["c"]

    # -- tests ----------------------------------------------------------------
    def test_verified_receipt_lands_on_approved_version(self):
        plan = self.plan(); self.approve(plan)
        b = self.batch()
        self.receipt(b["id"], plan, "rcpt-1")
        out = self.conclude(b["id"])
        self.assertTrue(out["concluded"])
        statuses = {c["status"] for c in self.conclusions(b["id"])}
        self.assertEqual(statuses, {VERIFIED})
        # Receipt landed on the approved revision.
        rcpt = self.recon.store.conn.execute("SELECT * FROM receipts WHERE offline_id='rcpt-1'").fetchone()
        self.assertEqual(rcpt["plan_revision"], 1)
        self.assertEqual(rcpt["processed"], 1)

    def test_replay_recorded_once(self):
        plan = self.plan(); self.approve(plan)
        b = self.batch()
        first = self.receipt(b["id"], plan, "rcpt-1")
        replay = self.receipt(b["id"], plan, "rcpt-1")
        self.assertFalse(first["idempotent"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["receipt"]["id"], first["receipt"]["id"])
        count = self.recon.store.conn.execute("SELECT COUNT(*) c FROM receipts WHERE offline_id='rcpt-1'").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_plan_change_invalidates_approval(self):
        plan = self.plan(); self.approve(plan)
        # Operator changes the plan after approval -> revision bumps to 2.
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {
            "expected_revision": 1, "route": [[116.12, 39.82], [116.32, 39.92]]})
        self.assertEqual(changed["revision"], 2)
        b = self.batch()
        # Receipt still names the approved revision v1.
        self.receipt(b["id"], plan, "rcpt-1", revision=1)
        self.conclude(b["id"])
        conc = self.conclusions(b["id"])
        self.assertEqual(len(conc), 1)
        self.assertEqual(conc[0]["status"], INVALIDATED)
        # A supplement deadline was opened for the operator.
        deadlines = [dict(r) for r in self.recon.store.conn.execute(
            "SELECT * FROM supplement_deadlines WHERE plan_id=? AND status='open'", (plan["id"],))]
        self.assertEqual(len(deadlines), 1)

    def test_restriction_change_invalidates_approval(self):
        plan = self.plan(); self.approve(plan)
        # A new restriction appears after approval overlapping the plan.
        self.svc.create_restriction("reviewer", "airspace_reviewer", {
            "name": "临时禁飞", "kind": "no_fly",
            "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.2, "max_lat": 40.0,
            "min_altitude": 0, "max_altitude": 150,
            "starts_at": iso(self.start - timedelta(minutes=30)),
            "ends_at": iso(self.start + timedelta(hours=2)), "reason": "活动"})
        b = self.batch()
        self.receipt(b["id"], plan, "rcpt-1")
        self.conclude(b["id"])
        self.assertEqual(self.conclusions(b["id"])[0]["status"], INVALIDATED)

    def test_restriction_revision_bump_invalidates_approval(self):
        # Restriction already present and acknowledged at approval time.
        self.svc.create_restriction("reviewer", "airspace_reviewer", {
            "name": "临时限飞", "kind": "temporary_limit",
            "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.2, "max_lat": 40.0,
            "min_altitude": 0, "max_altitude": 150,
            "starts_at": iso(self.start - timedelta(minutes=30)),
            "ends_at": iso(self.start + timedelta(hours=2)), "reason": "活动"})
        plan = self.plan()
        # Approve via commander override so the pre-existing restriction is acknowledged.
        submitted = self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})["plan"]
        self.svc.approve(plan["id"], "commander", "commander", {
            "expected_revision": submitted["revision"], "offline_id": "off-ov",
            "reason": "紧急任务", "override_reason": "应急救援授权"})
        # Restriction is changed (revision 1 -> 2) after approval.
        row = self.recon.store.conn.execute("SELECT id FROM restrictions WHERE name='临时限飞'").fetchone()
        self.svc.change_restriction(row["id"], "reviewer", "airspace_reviewer", {
            "expected_revision": 1, "max_altitude": 120, "reason": "收紧"})
        b = self.batch()
        self.receipt(b["id"], plan, "rcpt-1")
        self.conclude(b["id"])
        self.assertEqual(self.conclusions(b["id"])[0]["status"], INVALIDATED)

    def test_unapproved_revision_pending(self):
        plan = self.plan()  # never approved
        b = self.batch()
        self.receipt(b["id"], plan, "rcpt-1", revision=1)
        self.conclude(b["id"])
        self.assertEqual(self.conclusions(b["id"])[0]["status"], PENDING)

    def test_conflict_two_versions_then_adjudicate(self):
        plan = self.plan(); self.approve(plan)
        b = self.batch()
        self.receipt(b["id"], plan, "rcpt-1", landing=iso(self.start + timedelta(minutes=50)))
        self.receipt(b["id"], plan, "rcpt-2", landing=iso(self.start + timedelta(minutes=55)))
        self.conclude(b["id"])
        conc = self.conclusions(b["id"])
        self.assertTrue(all(c["status"] == CONFLICT for c in conc))
        import json
        versions = json.loads(conc[0]["versions_json"])
        self.assertEqual(len(versions), 2)
        # Auditor adjudicates: accept the current version -> verified.
        out = self.recon.adjudicate(conc[0]["id"], "auditor", "auditor", {"decision": "accept", "note": "核实"})
        self.assertEqual(out["conclusion"]["status"], VERIFIED)

    def test_mismatch_deduction_no_duplicate_on_restart(self):
        plan = self.plan(); self.approve(plan)
        b = self.batch()
        # Landing 30 minutes after the approved window -> mismatch.
        self.receipt(b["id"], plan, "rcpt-1", landing=iso(self.start + timedelta(hours=1, minutes=30)))
        self.conclude(b["id"])
        self.assertEqual(self.conclusions(b["id"])[0]["status"], MISMATCH)
        self.assertEqual(len(self.deductions(plan["id"])), 1)
        self.assertEqual(self.notif_count(plan["id"]), 1)
        # Simulate service restart: a brand new service on the same database.
        restarted = ReconciliationService(self.db)
        restarted.conclude_batch(b["id"], "auditor", "auditor", {})
        self.assertEqual(len(self.deductions(plan["id"])), 1, "restart must not deduct twice")
        self.assertEqual(self.notif_count(plan["id"]), 1, "restart must not notify twice")

    def test_retry_only_unprocessed_entries(self):
        plan = self.plan(); self.approve(plan)
        b = self.batch()
        self.receipt(b["id"], plan, "rcpt-1")
        self.conclude(b["id"])
        # After a successful conclude the receipt is processed; re-concluding skips it.
        rcpt = self.recon.store.conn.execute("SELECT * FROM receipts WHERE offline_id='rcpt-1'").fetchone()
        self.assertEqual(rcpt["processed"], 1)
        before = self.conclusions(b["id"])[0]["id"]
        self.conclude(b["id"])
        after = self.conclusions(b["id"])[0]["id"]
        self.assertEqual(before, after, "processed entries must not be re-concluded")

    def test_concurrent_conclude_first_write_wins(self):
        plan = self.plan(); self.approve(plan)
        b = self.batch()
        self.receipt(b["id"], plan, "rcpt-1")
        # Two separate services (two connections) like two auditor requests.
        svc_a = ReconciliationService(self.db)
        svc_b = ReconciliationService(self.db)
        results = {}
        barrier = threading.Barrier(2)

        def conclude(svc, name):
            barrier.wait()
            results[name] = svc.conclude_batch(b["id"], "auditor", "auditor", {})

        t1 = threading.Thread(target=conclude, args=(svc_a, "a"))
        t2 = threading.Thread(target=conclude, args=(svc_b, "b"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertTrue(results["a"]["concluded"])
        self.assertTrue(results["b"]["concluded"])
        self.assertTrue(results["b"].get("idempotent") or results["a"].get("idempotent"))
        # Batch concluded exactly once, receipt processed once.
        batch = self.recon.get_batch(b["id"], "auditor")
        self.assertEqual(batch["status"], "concluded")
        rcpt = self.recon.store.conn.execute("SELECT * FROM receipts WHERE offline_id='rcpt-1'").fetchone()
        self.assertEqual(rcpt["processed"], 1)

    def test_desk_shows_sections_by_role(self):
        plan = self.plan(); self.approve(plan)
        b = self.batch()
        self.receipt(b["id"], plan, "rcpt-1")
        self.conclude(b["id"])
        # Operator sees only their own verified entry.
        op_desk = self.recon.desk("operator", "OP1")
        self.assertEqual(op_desk["summary"]["verified"], 1)
        self.assertEqual(op_desk["pending_review"], [])
        # Auditor sees the same verified entry (all plans).
        au_desk = self.recon.desk("auditor", "")
        self.assertEqual(au_desk["summary"]["verified"], 1)
        # Viewer sees only verified (public) section.
        vi_desk = self.recon.desk("viewer", "")
        self.assertEqual(len(vi_desk["verified"]), 1)
        self.assertEqual(vi_desk["pending_review"], [])

    def test_late_receipt_processed_after_conclude(self):
        plan = self.plan(); self.approve(plan)
        b = self.batch()
        self.conclude(b["id"])  # conclude with no receipts
        # Receipt arrives late (offline): it is processed immediately.
        late = self.receipt(b["id"], plan, "rcpt-late")
        self.assertTrue(late["processed"])
        self.assertEqual(self.conclusions(b["id"])[0]["status"], VERIFIED)


if __name__ == "__main__":
    unittest.main()
