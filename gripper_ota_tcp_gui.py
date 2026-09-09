"""Tkinter UI for PC -> Ethernet -> robot -> RS485 gripper OTA."""

from __future__ import annotations

import argparse
import queue
import socket
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

import gripper_ota_tcp_tool as ota


class NetworkWorker(threading.Thread):
    """One thread owns accept/read/write, including idle telemetry draining."""

    def __init__(self, host: str, port: int, events: queue.Queue):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.events = events
        self.commands: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()

    def emit(self, kind: str, value: Any = None) -> None:
        self.events.put((kind, value))

    def log(self, text: str) -> None:
        self.emit("log", text)

    def _reject_pending_commands(self) -> None:
        # A click can race with disconnect detection; never carry an old
        # operation into a newly accepted robot connection.
        while True:
            try:
                action, _ = self.commands.get_nowait()
            except queue.Empty:
                return
            self.emit("finished", {"action": action,
                                    "error": "机器人连接已改变，请重连后重试"})

    def run(self) -> None:
        server = None
        conn = None
        try:
            server = ota.create_server(self.host, self.port)
            server.settimeout(0.2)
            self.emit("listening", server.getsockname())
            while not self.stop_event.is_set():
                if conn is None:
                    try:
                        sock, address = server.accept()
                    except socket.timeout:
                        continue
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    conn = ota.JsonFrameSocket(sock, log=self.log)
                    self._reject_pending_commands()
                    self.emit("connected", address)
                try:
                    action, args = self.commands.get_nowait()
                except queue.Empty:
                    try:
                        # Only this thread reads the socket. Drain the robot's
                        # periodic telemetry even when no OTA button is pressed.
                        conn.recv_json(0.2)
                    except TimeoutError:
                        pass
                    except (OSError, ConnectionError, ValueError) as exc:
                        conn.close()
                        conn = None
                        self.emit("disconnected", str(exc))
                        self._reject_pending_commands()
                    continue

                outcome: dict[str, Any] = {"action": action}
                try:
                    if action == "update":
                        outcome["result"] = ota.update(
                            conn, args, log=self.log,
                            progress=lambda done, total: self.emit("progress", (done, total)),
                            phase=lambda phase: self.emit("phase", phase),
                            should_cancel=self.cancel_event.is_set,
                        )
                    elif action == "query":
                        outcome["result"] = ota.query(conn, log=self.log)
                    elif action == "cancel":
                        outcome["result"] = ota.cancel(conn, log=self.log)
                except ota.OtaCancelled as exc:
                    outcome["cancelled"] = str(exc)
                except Exception as exc:
                    outcome["error"] = str(exc)
                    self.log(f"操作失败：{exc}")
                    if isinstance(exc, (OSError, ConnectionError)) and not isinstance(exc, TimeoutError):
                        conn.close()
                        conn = None
                        self.emit("disconnected", str(exc))
                finally:
                    self.emit("finished", outcome)
        except Exception as exc:
            self.emit("listen_error", str(exc))
        finally:
            if conn is not None:
                conn.close()
            if server is not None:
                server.close()
            self.emit("stopped")


class GripperEthernetOtaTool:
    PHASES = {
        "checking": "0/4 检查主控夹爪 OTA 接口（尚未请求升级）",
        "preparing": "1/4 进入维护态、准备夹爪 Bootloader",
        "streaming": "2/4 正在传输，等待夹爪确认写入",
        "verifying": "3/4 数据已发送，等待整包校验",
        "rebooting": "4/4 等待新 Application 启动、单位恢复与失能读回确认",
        "cancelling": "正在取消，等待主控确认恢复状态",
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("夹爪 OTA 工具（网线 → 机器人主控 → RS485）")
        root.geometry("1000x710")
        root.minsize(900, 640)
        self.events: queue.Queue = queue.Queue()
        self.worker: NetworkWorker | None = None
        self.connected = False
        self.busy = False
        self.action = ""
        self.phase = ""
        self.close_pending = False
        self.host_var = tk.StringVar(value=ota.DEFAULT_HOST)
        self.port_var = tk.StringVar(value=str(ota.DEFAULT_PORT))
        self.connection_var = tk.StringVar(value="未监听")
        self.file_var = tk.StringVar()
        self.gripper_var = tk.StringVar(value="1")
        self.major_var = tk.StringVar(value="1")
        self.minor_var = tk.StringVar(value="15")
        self.patch_var = tk.StringVar(value="0")
        self.gripper_hint_var = tk.StringVar()
        self.image_info_var = tk.StringVar(value="请选择待升级的夹爪 Application BIN")
        self.stage_var = tk.StringVar(value="等待连接机器人")
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text_var = tk.StringVar(value="0 / 0 bytes")
        self.firmware_widgets: list[ttk.Widget] = []
        self._build_ui()
        self._gripper_changed()
        self._refresh_controls()
        self.log("电脑监听 TCP 5001，机器人主动连接。电脑有线网卡应设为 192.168.0.20。")
        self.log("选择 BIN 和夹爪编号后开始升级；只有新 Application 启动确认后才提示成功。")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll_id = root.after(80, self._poll_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        connection = ttk.LabelFrame(outer, text="以太网连接", padding=10)
        connection.pack(fill="x")
        ttk.Label(connection, text="本机监听地址").grid(row=0, column=0, padx=4, pady=4)
        self.host_entry = ttk.Entry(connection, textvariable=self.host_var, width=16)
        self.host_entry.grid(row=0, column=1, padx=4)
        ttk.Label(connection, text="端口").grid(row=0, column=2, padx=4)
        self.port_entry = ttk.Entry(connection, textvariable=self.port_var, width=8)
        self.port_entry.grid(row=0, column=3, padx=4)
        self.listen_button = ttk.Button(connection, text="开始监听", command=self.toggle_listener)
        self.listen_button.grid(row=0, column=4, padx=12)
        ttk.Label(connection, textvariable=self.connection_var).grid(row=0, column=5, sticky="w")
        ttk.Label(connection, text="电脑网卡：192.168.0.20    默认监听 0.0.0.0:5001（不要填机器人 IP）").grid(
            row=1, column=0, columnspan=6, sticky="w", padx=4, pady=(6, 0))

        firmware = ttk.LabelFrame(outer, text="夹爪 Application 固件", padding=10)
        firmware.pack(fill="x", pady=(10, 0))
        firmware.columnconfigure(0, weight=1)
        file_entry = ttk.Entry(firmware, textvariable=self.file_var)
        file_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=4)
        choose_button = ttk.Button(firmware, text="选择 BIN", command=self.choose_file)
        choose_button.grid(row=0, column=1, pady=4)
        self.firmware_widgets.extend((file_entry, choose_button))
        options = ttk.Frame(firmware)
        options.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 4))
        ttk.Label(options, text="夹爪编号").pack(side="left", padx=(0, 8))
        self.gripper_combo = ttk.Combobox(options, textvariable=self.gripper_var,
                                          values=("1", "2"), width=6, state="readonly")
        self.gripper_combo.pack(side="left", padx=(0, 24))
        self.gripper_combo.bind("<<ComboboxSelected>>", self._gripper_changed)
        for label, variable in (("版本 Major", self.major_var), ("Minor", self.minor_var),
                                ("Patch", self.patch_var)):
            ttk.Label(options, text=label).pack(side="left", padx=(0, 8))
            entry = ttk.Entry(options, textvariable=variable, width=7)
            entry.pack(side="left", padx=(0, 16))
            self.firmware_widgets.append(entry)
        ttk.Label(firmware, textvariable=self.gripper_hint_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(firmware, textvariable=self.image_info_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(firmware, text="版本号须与 BIN 内版本一致；当前夹爪 1/2 均为 1.15.0，请分别选择对应 BIN。").grid(
            row=4, column=0, columnspan=2, sticky="w")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=12)
        self.update_button = ttk.Button(actions, text="开始 OTA 升级", command=self.on_update)
        self.update_button.pack(side="left", padx=(0, 8))
        self.query_button = ttk.Button(actions, text="查询升级状态", command=lambda: self._submit("query"))
        self.query_button.pack(side="left", padx=(0, 8))
        self.cancel_button = ttk.Button(actions, text="取消当前会话", command=self.on_cancel)
        self.cancel_button.pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="清空日志", command=self.clear_log).pack(side="right")
        ttk.Label(outer, textvariable=self.stage_var).pack(anchor="w", pady=(0, 6))
        progress = ttk.Frame(outer)
        progress.pack(fill="x")
        ttk.Progressbar(progress, variable=self.progress_var, maximum=100).pack(
            side="left", fill="x", expand=True)
        ttk.Label(progress, textvariable=self.progress_text_var, width=29, anchor="e").pack(
            side="left", padx=(10, 0))
        log_frame = ttk.LabelFrame(outer, text="日志", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = tk.Text(log_frame, wrap="word", height=14, state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _gripper_changed(self, _event=None) -> None:
        self.gripper_hint_var.set(
            "夹爪 1：机器人 UART7 / 从站 1" if self.gripper_var.get() == "1" else
            "夹爪 2：机器人 UART8 / 从站 2")

    def log(self, message: str) -> None:
        # Called only by the Tk thread; worker messages arrive via events.
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 2500:
            self.log_text.delete("1.0", "501.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def choose_file(self) -> None:
        default_dir = Path(__file__).resolve().parents[2] / "gripper-program" / "OTA_PC_Tool"
        path = filedialog.askopenfilename(parent=self.root, title="选择夹爪 Application BIN",
                                          initialdir=str(default_dir),
                                          filetypes=(("BIN 固件", "*.bin"), ("所有文件", "*.*")))
        if path:
            self.file_var.set(path)
            try:
                image = ota.load_and_validate_image(Path(path))
                self.image_info_var.set(f"{image.size} bytes    CRC32: 0x{image.crc32:08X}")
            except (OSError, ValueError) as exc:
                self.image_info_var.set("固件检查未通过")
                messagebox.showerror("固件无效", str(exc), parent=self.root)

    def _refresh_controls(self) -> None:
        ready = self.connected and not self.busy and not self.close_pending
        for button in (self.update_button, self.query_button):
            button.configure(state="normal" if ready else "disabled")
        cancellable = self.busy and self.action == "update" and self.phase in ("", "checking", "preparing", "streaming")
        pending_cancel = self.worker is not None and self.worker.cancel_event.is_set()
        self.cancel_button.configure(text="取消升级" if self.busy else "取消当前会话",
                                     state="normal" if (ready or (cancellable and not pending_cancel)) else "disabled")
        self.listen_button.configure(text="停止监听 / 断开" if self.worker else "开始监听",
                                     state="disabled" if self.busy or self.close_pending else "normal")
        for entry in (self.host_entry, self.port_entry):
            entry.configure(state="disabled" if self.worker else "normal")
        for widget in self.firmware_widgets:
            widget.configure(state="disabled" if self.busy else "normal")
        self.gripper_combo.configure(state="disabled" if self.busy else "readonly")

    def toggle_listener(self) -> None:
        if self.worker is not None:
            self.worker.stop_event.set()
            self.listen_button.configure(state="disabled")
            return
        try:
            port = int(self.port_var.get())
            if not 1 <= port <= 65535 or not self.host_var.get().strip():
                raise ValueError("请填写本机监听地址和 1～65535 的端口")
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc), parent=self.root)
            return
        self.worker = NetworkWorker(self.host_var.get().strip(), port, self.events)
        self.connection_var.set("正在启动监听…")
        self.worker.start()
        self._refresh_controls()

    def _submit(self, action: str, args: argparse.Namespace | None = None) -> None:
        if self.worker is None or not self.connected or self.busy:
            return
        self.busy = True
        self.action = action
        self.phase = ""
        self.worker.cancel_event.clear()
        self.stage_var.set({"update": "正在检查固件", "query": "正在查询主控升级状态",
                            "cancel": "正在取消主控当前会话"}[action])
        self.worker.commands.put((action, args))
        self._refresh_controls()

    def on_update(self) -> None:
        try:
            if not self.file_var.get().strip():
                raise ValueError("请先选择夹爪 Application BIN 文件")
            args = ota.build_parser().parse_args([])
            args.bin = Path(self.file_var.get().strip())
            args.gripper = int(self.gripper_var.get())
            version = ".".join(v.get().strip() for v in (self.major_var, self.minor_var, self.patch_var))
            args.version = ".".join(str(v) for v in ota.parse_version(version))
            if args.gripper not in (1, 2):
                raise ValueError("夹爪编号必须为 1 或 2")
            image = ota.load_and_validate_image(args.bin)
            self.image_info_var.set(f"{image.size} bytes    CRC32: 0x{image.crc32:08X}")
        except (OSError, ValueError) as exc:
            messagebox.showerror("参数错误", str(exc), parent=self.root)
            return
        self.progress_var.set(0)
        self.progress_text_var.set(f"0 / {image.size} bytes")
        self._submit("update", args)

    def on_cancel(self) -> None:
        if self.worker is not None and self.busy:
            if self.action == "update" and self.phase in ("", "checking", "preparing", "streaming"):
                self.worker.cancel_event.set()
                self.stage_var.set("已请求取消，等待主控确认…")
                self.log("已请求取消；擦除开始后需要重新发送完整 BIN 才能恢复。")
                self._refresh_controls()
        else:
            self._submit("cancel")

    def _finish(self, outcome: dict[str, Any]) -> None:
        self.busy = False
        # A modal dialog starts a nested event loop: restore buttons BEFORE it.
        self._refresh_controls()
        if "error" in outcome:
            self.stage_var.set("操作失败，请查看日志中的失败阶段和恢复状态")
            if not self.close_pending:
                summary = str(outcome["error"]).split("详情：", 1)[0]
                if len(summary) > 260:
                    summary = summary[:260] + "…"
                messagebox.showerror("操作失败", summary + "\n\n完整诊断信息见下方日志。", parent=self.root)
        elif "cancelled" in outcome:
            self.stage_var.set("升级已停止；请查看取消确认结果，必要时重新发送完整 BIN")
        else:
            result = outcome.get("result", {})
            if outcome["action"] == "update":
                self.stage_var.set(f"升级成功：夹爪 {result['gripper_id']} / {result['version']}，毫度单位已确认，等待新命令")
                if not self.close_pending:
                    messagebox.showinfo("升级成功", self.stage_var.get(), parent=self.root)
            elif result.get("recovery_required"):
                self.stage_var.set("需要恢复：主控保持维护态，请重新发送完整 BIN")
            elif outcome["action"] == "cancel":
                self.stage_var.set("取消完成 / 当前无活动会话")
            else:
                self.stage_var.set(f"主控会话状态：{result.get('state', '未知')}（详情见日志）")
        if self.close_pending and self.worker is not None:
            self.worker.stop_event.set()

    def _poll_events(self) -> None:
        try:
            for _ in range(200):
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.log(value)
                elif kind == "listening":
                    self.connection_var.set("等待机器人连接…")
                    self.log(f"开始监听 {value[0]}:{value[1]}")
                elif kind == "connected":
                    self.connected = True
                    self.connection_var.set(f"TCP已连接 {value[0]}:{value[1]}")
                    self.stage_var.set("TCP已连接；可先查询升级状态，确认主控OTA接口响应")
                    self.log(self.connection_var.get())
                elif kind == "disconnected":
                    self.connected = False
                    self.connection_var.set("连接已断开，等待重连…")
                    self.log(f"连接断开：{value}")
                elif kind == "phase":
                    self.phase = value
                    self.stage_var.set(self.PHASES[value])
                elif kind == "progress":
                    done, total = value
                    percent = done * 100 / total if total else 0
                    self.progress_var.set(percent)
                    self.progress_text_var.set(f"{done} / {total} bytes  ({percent:.0f}%)")
                elif kind == "finished":
                    self._finish(value)
                elif kind == "listen_error":
                    self.log(f"监听失败：{value}")
                    if not self.close_pending:
                        messagebox.showerror("监听失败", f"{value}\n请检查本机地址、端口占用和防火墙。", parent=self.root)
                elif kind == "stopped":
                    self.worker = None
                    self.connected = False
                    self.busy = False
                    self.connection_var.set("未监听")
                    self.log("监听已停止")
        except queue.Empty:
            pass
        if self.close_pending and self.worker is None:
            self.root.destroy()
            return
        self._refresh_controls()
        self.poll_id = self.root.after(80, self._poll_events)

    def on_close(self) -> None:
        self.close_pending = True
        if self.worker is not None:
            if self.busy:
                self.on_cancel()
                self.log("正在等待当前操作结束后关闭；校验及启动确认期间会等待结果。")
            else:
                self.worker.stop_event.set()
        self._refresh_controls()


def main() -> int:
    root = tk.Tk()
    GripperEthernetOtaTool(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
