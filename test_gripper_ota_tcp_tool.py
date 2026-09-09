import json
import queue
import socket
import struct
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import gripper_ota_tcp_tool as tool


IMAGE_PATH = Path(__file__).parents[2] / "gripper-program" / "OTA_PC_Tool" / "gripper1_app.bin"
IMAGE2_PATH = IMAGE_PATH.with_name("gripper2_app.bin")


class RobotReplies:
    """In-memory robot peer with delayed/duplicate responses and cancel support."""

    def __init__(self, boot_ready=True, drop_first_ack=False):
        self.pending = []
        self.requests = []
        self.image = bytearray()
        self.boot_ready = boot_ready
        self.drop_first_ack = drop_first_ack
        self.dropped = False
        self.last_ack = None
        self.start = None

    def send_json(self, obj):
        self.requests.append(obj.copy())
        topic = obj["topic"]
        if topic == "gripper_ota_query":
            self.pending.append({"topic": "gripper_ota_status", "state": "IDLE", "recovery_required": False,
                                 "control_restore_supported": True})
        elif topic == "gripper_ota_start":
            self.start = obj
            self.pending.append({"topic": "gripper_ota_ready", "session_id": obj["session_id"],
                                 "gripper_id": obj["gripper_id"], "boot_version": "1.0",
                                 "block_size": tool.BLOCK_SIZE, "next_offset": 0})
        elif topic == "gripper_ota_data":
            data = tool.base64.b64decode(obj["data"])
            offset = obj["seq"] * tool.BLOCK_SIZE
            if offset == len(self.image):
                self.image.extend(data)
            else:
                assert self.image[offset:offset + len(data)] == data
            if self.drop_first_ack and not self.dropped:
                self.dropped = True
                return
            # A retry can produce a second ACK that arrives during the next block.
            if self.last_ack is not None:
                self.pending.append(self.last_ack)
            self.last_ack = {"topic": "gripper_ota_ack", "session_id": obj["session_id"],
                             "gripper_id": self.start["gripper_id"], "seq": obj["seq"],
                             "committed_offset": offset + len(data), "ok": True}
            self.pending.append(self.last_ack)
        elif topic == "gripper_ota_end":
            assert len(self.image) == self.start["size"]
            assert tool.zlib.crc32(self.image) == int(self.start["crc32"], 0)
            self.pending.extend([
                {"topic": "gripper_ota_result", "session_id": obj["session_id"],
                 "gripper_id": self.start["gripper_id"], "ok": True},
                {"topic": "gripper_ota_boot_ok", "session_id": obj["session_id"],
                 "gripper_id": self.start["gripper_id"], "version": self.start["version"],
                 "app_ready": self.boot_ready, "control_ready": True,
                 "previous_unit_cfg": 15, "unit_cfg": 11, "mode": 0, "motor_enabled": False},
            ])
        elif topic == "gripper_ota_cancel":
            self.pending.append({"topic": "gripper_ota_cancelled", "ok": True,
                                 "session_id": obj["session_id"], "recovery_required": True})

    def recv_json(self, timeout):
        if self.pending:
            return self.pending.pop(0)
        time.sleep(min(timeout, 0.001))
        raise TimeoutError("模拟无响应")


def update_args():
    args = tool.build_parser().parse_args([])
    args.bin = IMAGE_PATH
    args.version = "1.15.0"
    args.ack_timeout = 0.01
    return args


class ToolTests(unittest.TestCase):
    def test_current_gripper_bin(self):
        for path in (IMAGE_PATH, IMAGE2_PATH):
            with self.subTest(path=path.name):
                image = tool.load_and_validate_image(path)
                self.assertLessEqual(image.size, tool.APP_SIZE)
                self.assertEqual(image.crc32, tool.zlib.crc32(image.data) & 0xFFFFFFFF)

    def test_max_data_json_fits_robot_rx_pool(self):
        payload = {
            "topic": "gripper_ota_data",
            "session_id": 0xFFFFFFFF,
            "seq": 999999,
            "data": tool.base64.b64encode(bytes(tool.BLOCK_SIZE)).decode("ascii"),
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        self.assertLess(len(raw), 1024)

    def test_json_framer_handles_braces_in_string(self):
        left, right = socket.socketpair()
        try:
            framed = tool.JsonFrameSocket(left)
            right.sendall(b'{"topic":"a","value":"{}"}{"topic":"b"}')
            self.assertEqual(framed.recv_json(0.2)["topic"], "a")
            self.assertEqual(framed.recv_json(0.2)["topic"], "b")
        finally:
            left.close()
            right.close()

    def test_identity_layout(self):
        self.assertEqual(len(tool.APP_IDENTITY), 20)
        self.assertEqual(struct.unpack_from("<I", tool.APP_IDENTITY, 12)[0], tool.APP_BASE)

    def test_update_progress_with_lost_and_stale_ack(self):
        robot = RobotReplies(drop_first_ack=True)
        progress = []
        phases = []
        args = update_args()
        args.bin = IMAGE2_PATH
        args.gripper = 2  # Selection must reach START/role and BOOT_OK validation.
        result = tool.update(robot, args, log=lambda _: None,
                             progress=lambda done, total: progress.append((done, total)),
                             phase=phases.append)
        self.assertEqual(robot.image, IMAGE2_PATH.read_bytes())
        self.assertEqual(robot.start["gripper_id"], 2)
        self.assertEqual(robot.start["role"], 2)
        self.assertEqual(result["gripper_id"], 2)
        self.assertEqual(phases, ["checking", "preparing", "streaming", "verifying", "rebooting"])
        self.assertEqual(progress[0], (0, len(robot.image)))
        self.assertEqual(progress[-1], (len(robot.image), len(robot.image)))
        done = [p[0] for p in progress]
        self.assertEqual(done, sorted(set(done)))
        data_requests = [r for r in robot.requests if r["topic"] == "gripper_ota_data"]
        self.assertEqual(data_requests[0], data_requests[1])

    def test_100_percent_is_not_success_without_app_ready(self):
        robot = RobotReplies(boot_ready=False)
        with self.assertRaisesRegex(RuntimeError, "BOOT_OK"):
            tool.update(robot, update_args(), log=lambda _: None)
        self.assertEqual(robot.requests[-1]["topic"], "gripper_ota_cancel")

    def test_legacy_main_firmware_is_rejected_before_start(self):
        class LegacyRobot(RobotReplies):
            def send_json(self, obj):
                super().send_json(obj)
                if obj["topic"] == "gripper_ota_query":
                    self.pending[-1].pop("control_restore_supported")
        robot = LegacyRobot()
        with self.assertRaisesRegex(RuntimeError, "主控未确认支持OTA后单位恢复"):
            tool.update(robot, update_args(), log=lambda _: None)
        self.assertEqual([r["topic"] for r in robot.requests], ["gripper_ota_query"])

    def test_wrong_unit_enabled_or_unconfirmed_control_never_reports_success(self):
        for changed in ({"unit_cfg": 15}, {"control_ready": False},
                        {"motor_enabled": True}, {"mode": 1}, {"control_ready": None}):
            with self.subTest(changed=changed):
                class BadControlRobot(RobotReplies):
                    def send_json(self, obj):
                        super().send_json(obj)
                        if obj["topic"] == "gripper_ota_end":
                            self.pending[-1].update(changed)
                robot = BadControlRobot()
                with self.assertRaisesRegex(RuntimeError, "BOOT_OK"):
                    tool.update(robot, update_args(), log=lambda _: None)

    def test_pending_control_recovery_does_not_erase_again(self):
        class PendingRobot(RobotReplies):
            def send_json(self, obj):
                super().send_json(obj)
                if obj["topic"] == "gripper_ota_query":
                    self.pending[-1].update(state="RECOVERY_REQUIRED", gripper_id=1,
                                            control_restore_pending=True)
        robot = PendingRobot()
        with self.assertRaisesRegex(RuntimeError, "不必重新擦写固件"):
            tool.update(robot, update_args(), log=lambda _: None)
        self.assertEqual(len(robot.requests), 1)

    def test_control_restore_result_is_logged(self):
        logs = []
        tool.update(RobotReplies(), update_args(), log=logs.append)
        self.assertTrue(any("0x000F → 0x000B" in line for line in logs))
        self.assertTrue(any("电机保持失能" in line for line in logs))

    def test_cancel_stops_next_data_and_waits_for_cancel_ack(self):
        robot = RobotReplies()
        stopped = threading.Event()
        logs = []
        def progress(done, _total):
            if done:
                stopped.set()
        with self.assertRaises(tool.OtaCancelled):
            tool.update(robot, update_args(), log=logs.append, progress=progress,
                        should_cancel=stopped.is_set)
        self.assertEqual(len(robot.image), tool.BLOCK_SIZE)
        self.assertEqual(robot.requests[-1]["topic"], "gripper_ota_cancel")
        self.assertFalse(any(r["topic"] == "gripper_ota_end" for r in robot.requests))
        self.assertTrue(any("recovery_required=True" in log for log in logs))

    def test_cancel_before_start_never_requests_erase(self):
        robot = RobotReplies()
        with self.assertRaises(tool.OtaCancelled):
            tool.update(robot, update_args(), log=lambda _: None, should_cancel=lambda: True)
        self.assertEqual(robot.requests, [])

    def test_no_query_reply_prevents_start_and_cancel(self):
        class SilentRobot(RobotReplies):
            def send_json(self, obj):
                self.requests.append(obj.copy())
        robot = SilentRobot()
        original_query = tool.query_status
        def short_query(*args, **kwargs):
            return original_query(*args, **kwargs, timeout=0.002)
        with patch.object(tool, "query_status", side_effect=short_query):
            with self.assertRaisesRegex(TimeoutError, "主控未回复 gripper_ota_query"):
                tool.update(robot, update_args(), log=lambda _: None)
        self.assertEqual([r["topic"] for r in robot.requests],
                         ["gripper_ota_query", "gripper_ota_query"])

    def test_timeout_distinguishes_no_tcp_data(self):
        left, right = socket.socketpair()
        try:
            conn = tool.JsonFrameSocket(left)
            with self.assertRaisesRegex(TimeoutError, "未收到主控TCP数据"):
                tool.wait_topic(conn, {"gripper_ota_status"}, 0.02)
        finally:
            left.close()
            right.close()

    def test_timeout_distinguishes_telemetry_and_does_not_flood_log(self):
        left, right = socket.socketpair()
        logs = []
        try:
            conn = tool.JsonFrameSocket(left, log=logs.append)
            raw = b''.join(json.dumps({"topic": f"telemetry_{i}"}).encode() for i in range(20))
            right.sendall(raw * 2)
            with self.assertRaisesRegex(TimeoutError, "收到40条JSON"):
                tool.wait_topic(conn, {"gripper_ota_status"}, 0.05)
            self.assertEqual(conn.rx_frames, 40)
            self.assertEqual(len(logs), 20)
        finally:
            left.close()
            right.close()

    def test_network_worker_updates_over_loopback_and_stays_connected(self):
        from gripper_ota_tcp_gui import NetworkWorker
        events = queue.Queue()
        worker = NetworkWorker("127.0.0.1", 0, events)
        worker.start()
        peer = None
        peer_error = []
        robot = RobotReplies()
        def wait_event(kind):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                event, value = events.get(timeout=5)
                if event == kind:
                    return value
            self.fail(f"未收到事件 {kind}")
        try:
            address = wait_event("listening")
            # A click left over from a disconnected session must not start
            # erasing as soon as a new robot connects.
            worker.commands.put(("update", update_args()))
            peer = tool.JsonFrameSocket(socket.create_connection(address, timeout=2))
            self.assertIn("error", wait_event("finished"))
            wait_event("connected")
            def serve_robot():
                try:
                    # Exercise ongoing non-OTA telemetry before and during update.
                    peer.send_json({"topic": "robot_status", "value": "idle"})
                    while True:
                        request = peer.recv_json(5)
                        if request["topic"] == "gripper_ota_query" and robot.image:
                            peer.send_json({"topic": "gripper_ota_status", "state": "IDLE"})
                            return
                        robot.send_json(request)
                        while robot.pending:
                            peer.send_json(robot.pending.pop(0))
                except Exception as exc:
                    peer_error.append(exc)
            simulator = threading.Thread(target=serve_robot, daemon=True)
            simulator.start()
            args = update_args()
            args.ack_timeout = 2
            worker.commands.put(("update", args))
            result = wait_event("finished")
            self.assertNotIn("error", result)
            self.assertTrue(result["result"]["app_ready"])
            worker.commands.put(("query", None))
            self.assertEqual(wait_event("finished")["result"]["state"], "IDLE")
            simulator.join(2)
            self.assertFalse(simulator.is_alive())
            self.assertEqual(peer_error, [])
            self.assertEqual(robot.image, IMAGE_PATH.read_bytes())
        finally:
            worker.stop_event.set()
            if peer is not None:
                peer.close()
            worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_query_survives_6366_telemetry_frames_with_delivery_receipt(self):
        left, right = socket.socketpair()
        logs, errors = [], []
        def robot():
            try:
                peer = tool.JsonFrameSocket(right)
                self.assertEqual(peer.recv_json(2)["topic"], "gripper_ota_query")
                peer.send_json({"topic": "robot_ota_link", "running_slot": "B",
                                "relay_rev": "20260908-net2", "netcmd_created": True})
                right.sendall(b'{"topic":"gripper_tactile"}' * 6366)
                peer.send_json({"topic": "gripper_ota_rx", "command": "gripper_ota_query",
                                "accepted": True, "cmd_age_ms": 1})
                peer.send_json({"topic": "gripper_ota_status", "state": "IDLE"})
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=robot, daemon=True)
        thread.start()
        try:
            conn = tool.JsonFrameSocket(left, log=logs.append)
            self.assertEqual(tool.query_status(conn, log=logs.append, timeout=3)["state"], "IDLE")
            self.assertEqual(conn.rx_frames, 6369)
            self.assertTrue(conn.query_receipt["accepted"])
            self.assertEqual(conn.peer_info["running_slot"], "B")
            self.assertEqual(sum("topic=gripper_tactile" in log for log in logs), 1)
            self.assertTrue(any("Slot B" in log and "20260908-net2" in log for log in logs))
        finally:
            left.close()
            right.close()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_rejected_command_reports_rx_queue_not_rs485(self):
        left, right = socket.socketpair()
        try:
            right.sendall(json.dumps({"topic": "gripper_ota_rx", "command": "gripper_ota_query",
                                     "accepted": False, "reason": "rx_queue_full",
                                     "netcmd_created": True}).encode())
            with self.assertRaisesRegex(RuntimeError, "未交给命令任务：rx_queue_full"):
                tool.wait_topic(tool.JsonFrameSocket(left), {"gripper_ota_status"}, 0.1)
        finally:
            left.close()
            right.close()

    def test_query_diagnostics_distinguish_link_rx_and_netcmd(self):
        conn = tool.JsonFrameSocket(None)
        self.assertIn("未收到新固件", tool.query_failure_hint(conn))
        conn._trace("RX", {"topic": "robot_ota_link", "running_slot": "A",
                           "relay_rev": "20260908-net2", "netcmd_created": True})
        self.assertIn("JSON分帧", tool.query_failure_hint(conn))
        conn._trace("RX", {"topic": "gripper_ota_rx", "command": "gripper_ota_query",
                           "accepted": True, "cmd_age_ms": 5000, "cmd_seen": 0, "cmd_done": 0})
        self.assertIn("成功入队", tool.query_failure_hint(conn))
        self.assertIn("5000ms", tool.query_failure_hint(conn))
        conn.query_receipt["netcmd_created"] = False
        self.assertIn("NetCmd任务未创建", tool.query_failure_hint(conn))

    def test_new_query_does_not_reuse_previous_receipt(self):
        left, right = socket.socketpair()
        try:
            conn = tool.JsonFrameSocket(left)
            conn.query_receipt = {"accepted": True}
            conn.send_json({"topic": "gripper_ota_query"})
            self.assertEqual(conn.query_receipt, {})
        finally:
            left.close()
            right.close()

    def test_gui_selection_and_busy_controls(self):
        import tkinter as tk
        from gripper_ota_tcp_gui import GripperEthernetOtaTool, NetworkWorker
        root = tk.Tk()
        root.withdraw()  # Smoke-test real widgets without opening a user window.
        app = GripperEthernetOtaTool(root)
        try:
            root.update_idletasks()
            self.assertTrue(app.update_button.instate(["disabled"]))
            app.worker = NetworkWorker("127.0.0.1", 0, app.events)
            app.connected = True
            app.file_var.set(str(IMAGE_PATH))
            app.gripper_var.set("2")
            app.on_update()
            action, args = app.worker.commands.get_nowait()
            self.assertEqual(action, "update")
            self.assertEqual(args.gripper, 2)
            self.assertEqual(args.bin, IMAGE_PATH)
            self.assertTrue(app.gripper_combo.instate(["disabled"]))
            self.assertTrue(app.listen_button.instate(["disabled"]))
            app.on_cancel()
            self.assertTrue(app.worker.cancel_event.is_set())
            app.worker.cancel_event.clear()
            app.phase = "verifying"
            app._refresh_controls()
            app.on_cancel()
            self.assertTrue(app.cancel_button.instate(["disabled"]))
            self.assertFalse(app.worker.cancel_event.is_set())
            with patch("gripper_ota_tcp_gui.messagebox.showerror") as dialog:
                def check_modal(*args, **kwargs):
                    self.assertFalse(app.update_button.instate(["disabled"]))
                    self.assertTrue(app.gripper_combo.instate(["readonly"]))
                    self.assertLess(len(args[1]), 300)
                    self.assertNotIn("telemetry_topic", args[1])
                dialog.side_effect = check_modal
                app._finish({"action": "update", "error": "模拟失败。详情：" + "telemetry_topic" * 200})
                dialog.assert_called_once()
            app._refresh_controls()
            self.assertTrue(app.gripper_combo.instate(["readonly"]))
            self.assertFalse(app.update_button.instate(["disabled"]))
        finally:
            root.after_cancel(app.poll_id)
            root.destroy()


if __name__ == "__main__":
    unittest.main()
