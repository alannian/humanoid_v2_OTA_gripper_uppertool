#!/usr/bin/env python3
"""PC -> Ethernet -> H723 -> RS485 -> G431 gripper OTA tool."""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import socket
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5001
BLOCK_SIZE = 672
PRODUCT_ID = 0x0000D431
LAYOUT_VERSION = 1
APP_BASE = 0x08006000
APP_SIZE = 0x00019000
SRAM_BASE = 0x20000000
MAILBOX_BASE = 0x20007FE0
APP_IDENTITY = struct.pack(
    "<IIIII", 0x50415247, PRODUCT_ID, LAYOUT_VERSION, APP_BASE, 0x21444947
)


@dataclass(frozen=True)
class ImageInfo:
    data: bytes
    size: int
    crc32: int
    initial_msp: int
    reset_handler: int


class OtaCancelled(RuntimeError):
    """An operator stopped the transfer; update() still requests CANCEL."""


def check_cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise OtaCancelled("用户已取消升级")


class JsonFrameSocket:
    def __init__(self, sock: socket.socket, *, log: Callable[[str], None] | None = None):
        self.sock = sock
        self.buffer = bytearray()
        self.log = log
        self.rx_bytes = 0
        self.rx_frames = 0
        self.rx_topics: list[str] = []
        self._logged_topics: set[str] = set()
        self.peer_info: dict[str, Any] = {}
        self.query_receipt: dict[str, Any] = {}

    def _trace(self, direction: str, obj: dict[str, Any]) -> None:
        topic = str(obj.get("topic", "<无topic>"))
        if direction == "RX":
            self.rx_frames += 1
            if topic == "robot_ota_link":
                self.peer_info = obj.copy()
                if self.log is not None:
                    self.log(f"主控实际运行 Slot {obj.get('running_slot', '?')}，"
                             f"转发固件标识 {obj.get('relay_rev', '?')}，"
                             f"NetCmd已创建={obj.get('netcmd_created')}")
            elif topic == "gripper_ota_rx" and obj.get("command") == "gripper_ota_query":
                self.query_receipt = obj.copy()
            if topic not in self.rx_topics:
                self.rx_topics.append(topic)
                self.rx_topics = self.rx_topics[-16:]
            if topic not in self._logged_topics and len(self._logged_topics) < 64:
                self._logged_topics.add(topic)
                if (self.log is not None and not topic.startswith("gripper_ota_") and
                        topic not in ("ota_boot_ok", "robot_ota_link")):
                    self.log(f"[RX] 收到主控报文 topic={topic}")
        if (self.log is not None and
                (topic.startswith("gripper_ota_") or topic in ("ota_boot_ok", "ota_error", "robot_ota_link")) and
                topic not in ("gripper_ota_data", "gripper_ota_ack")):
            self.log(f"[{direction}] {json.dumps(obj, ensure_ascii=False, separators=(',', ':'))}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def send_json(self, obj: dict[str, Any]) -> None:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) >= 1024:
            raise ValueError(f"JSON帧{len(raw)}字节，超过机器人1 KiB接收池限制")
        if obj.get("topic") == "gripper_ota_query":
            self.query_receipt = {}
        self.sock.sendall(raw)
        self._trace("TX", obj)

    def recv_json(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            frame = self._extract_frame()
            if frame is not None:
                obj = json.loads(frame.decode("utf-8", errors="strict"))
                self._trace("RX", obj)
                return obj

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"等待JSON响应超时（{timeout:.1f}s）")
            self.sock.settimeout(min(remaining, 1.0))
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("机器人主控断开了TCP连接")
            self.buffer.extend(chunk)
            self.rx_bytes += len(chunk)

    def _extract_frame(self) -> bytes | None:
        try:
            start = self.buffer.index(ord("{"))
        except ValueError:
            self.buffer.clear()
            return None
        if start:
            del self.buffer[:start]

        depth = 0
        in_string = False
        escaped = False
        for index, value in enumerate(self.buffer):
            if in_string:
                if escaped:
                    escaped = False
                elif value == ord("\\"):
                    escaped = True
                elif value == ord('"'):
                    in_string = False
                continue
            if value == ord('"'):
                in_string = True
            elif value == ord("{"):
                depth += 1
            elif value == ord("}"):
                depth -= 1
                if depth == 0:
                    frame = bytes(self.buffer[: index + 1])
                    del self.buffer[: index + 1]
                    return frame
        return None


def load_and_validate_image(path: Path) -> ImageInfo:
    data = path.read_bytes()
    if len(data) < 8:
        raise ValueError("BIN不足8字节，缺少向量表")
    if len(data) > APP_SIZE:
        raise ValueError(f"BIN为{len(data)}字节，超过Application上限{APP_SIZE}字节")
    msp, reset_handler = struct.unpack_from("<II", data, 0)
    reset_address = reset_handler & ~1
    if not (SRAM_BASE <= msp <= MAILBOX_BASE) or msp & 7:
        raise ValueError(f"初始MSP 0x{msp:08X}不合法")
    if not reset_handler & 1:
        raise ValueError(f"Reset_Handler 0x{reset_handler:08X}缺少Thumb位")
    if not (APP_BASE <= reset_address < APP_BASE + len(data)):
        raise ValueError(
            f"Reset_Handler 0x{reset_handler:08X}不在当前BIN范围；"
            "请检查夹爪Application的IROM1/VTOR是否为0x08006000"
        )
    if APP_IDENTITY not in data:
        raise ValueError("BIN缺少夹爪OTA身份签名，请使用已经完成OTA适配的Application")
    return ImageInfo(data, len(data), zlib.crc32(data) & 0xFFFFFFFF, msp, reset_handler)


def parse_version(text: str) -> tuple[int, int, int]:
    parts = text.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError("--version必须是Major.Minor.Patch，例如1.15.0")
    version = tuple(int(part) for part in parts)
    if any(part < 0 or part > 255 for part in version):
        raise ValueError("当前Application版本寄存器要求三个版本分量均为0~255")
    return version  # type: ignore[return-value]


def create_server(host: str, port: int) -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(1)
    except BaseException:
        server.close()
        raise
    return server


def accept_robot(server: socket.socket, timeout: float) -> JsonFrameSocket:
    print(f"等待机器人主控连接 {server.getsockname()[0]}:{server.getsockname()[1]} ...")
    server.settimeout(timeout)
    sock, address = server.accept()
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print(f"已连接机器人主控：{address[0]}:{address[1]}")
    return JsonFrameSocket(sock, log=print)


def wait_topic(conn: JsonFrameSocket, expected: set[str], timeout: float, *,
               should_cancel: Callable[[], bool] | None = None,
               session_id: int | None = None, seq: int | None = None,
               ignore_errors: bool = False) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    start_bytes = getattr(conn, "rx_bytes", 0)
    start_frames = getattr(conn, "rx_frames", 0)
    ignored_matches = 0
    while True:
        check_cancel(should_cancel)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            detail = ""
            if hasattr(conn, "rx_bytes"):
                received = conn.rx_bytes - start_bytes
                frames = conn.rx_frames - start_frames
                if frames:
                    detail = (f"；期间收到{frames}条JSON，近期topic={conn.rx_topics}，"
                              f"会话/序号不匹配的目标回复={ignored_matches}条")
                elif received or conn.buffer:
                    detail = (f"；期间收到{received}字节，未解析出完整JSON，"
                              f"缓冲区剩余{len(conn.buffer)}字节")
                else:
                    detail = "；等待期间未收到主控TCP数据"
            raise TimeoutError(f"等待{sorted(expected)}超时{detail}")
        try:
            obj = conn.recv_json(min(remaining, 1.0))
        except TimeoutError:
            continue
        topic = str(obj.get("topic", ""))
        if topic == "gripper_ota_rx" and obj.get("accepted") is False and not ignore_errors:
            raise RuntimeError(
                f"主控已收到 {obj.get('command')}，但未交给命令任务："
                f"{obj.get('reason')}；NetCmd已创建={obj.get('netcmd_created')}。"
                "请保留日志并检查主控接收队列/缓冲池。"
            )
        if topic == "gripper_ota_error" and not ignore_errors:
            raise RuntimeError(
                "主控返回夹爪OTA错误："
                f"stage={obj.get('stage')} code={obj.get('code')} "
                f"downstream={obj.get('downstream_status_name')} "
                f"offset={obj.get('committed_offset')} msg={obj.get('msg')} "
                f"recovery_required={obj.get('recovery_required')}"
            )
        if topic in expected:
            if session_id is not None and obj.get("session_id") != session_id:
                ignored_matches += 1
                continue
            if seq is not None and obj.get("seq") != seq:
                ignored_matches += 1
                continue
            return obj


def query_failure_hint(conn: JsonFrameSocket) -> str:
    receipt = getattr(conn, "query_receipt", {})
    peer = getattr(conn, "peer_info", {})
    evidence = receipt or peer
    if evidence.get("netcmd_created") is False:
        return "主控报告NetCmd任务未创建，检查任务创建和FreeRTOS剩余堆空间。"
    if receipt.get("accepted") is True:
        return ("主控已接收查询并成功入队，但NetCmd未完成状态回复；"
                f"心跳间隔={receipt.get('cmd_age_ms')}ms，"
                f"命令取出/完成={receipt.get('cmd_seen')}/{receipt.get('cmd_done')}。")
    if peer:
        return (f"已确认运行Slot {peer.get('running_slot')} / {peer.get('relay_rev')}，"
                "但未收到查询的接收回执；重点检查主控TCP接收、JSON分帧及接收回调。")
    return ("未收到新固件robot_ota_link标识或查询回执；"
            "请核对实际运行槽位及是否烧录20260909-unit1版本，不能只凭TCP连接确认固件版本。")


def query_status(conn: JsonFrameSocket, *, log: Callable[[str], None] = print,
                 should_cancel: Callable[[], bool] | None = None,
                 timeout: float = 5.0) -> dict[str, Any]:
    """Check the application command/reply path before requesting any erase."""
    for attempt in range(1, 3):
        check_cancel(should_cancel)
        log(f"主控夹爪OTA接口检查 {attempt}/2：发送 gripper_ota_query")
        conn.send_json({"topic": "gripper_ota_query"})
        try:
            status = wait_topic(conn, {"gripper_ota_status"}, timeout,
                                should_cancel=should_cancel)
            if not isinstance(status.get("state"), str):
                raise RuntimeError(f"主控状态回复缺少state：{status}")
            log(f"主控夹爪OTA接口已响应：state={status['state']}")
            return status
        except TimeoutError as exc:
            log(str(exc))
            if attempt == 2:
                raise TimeoutError(
                    "TCP已连接，但主控未回复 gripper_ota_query。"
                    + query_failure_hint(conn)
                    + "尚不能判断夹爪RS485故障。详情：" + str(exc)
                ) from exc
    raise AssertionError("unreachable")


def send_start(conn: JsonFrameSocket, image: ImageInfo, gripper: int,
               version_text: str, hardware_revision: int, session_id: int) -> None:
    conn.send_json(
        {
            "topic": "gripper_ota_start",
            "session_id": session_id,
            "gripper_id": gripper,
            "role": gripper,
            "product_id": f"0x{PRODUCT_ID:08X}",
            "hardware_revision": hardware_revision,
            "layout_version": LAYOUT_VERSION,
            "version": version_text,
            "min_boot_version": "0x0100",
            "image_base": f"0x{APP_BASE:08X}",
            "size": image.size,
            "crc32": f"0x{image.crc32:08X}",
            "block_size": BLOCK_SIZE,
        }
    )


def send_data(conn: JsonFrameSocket, image: ImageInfo, session_id: int,
              gripper: int, ack_timeout: float, retries: int, *,
              log: Callable[[str], None] = print,
              progress: Callable[[int, int], None] | None = None,
              should_cancel: Callable[[], bool] | None = None) -> None:
    total = (image.size + BLOCK_SIZE - 1) // BLOCK_SIZE
    started = time.monotonic()
    for seq, offset in enumerate(range(0, image.size, BLOCK_SIZE)):
        check_cancel(should_cancel)
        chunk = image.data[offset : offset + BLOCK_SIZE]
        request = {
            "topic": "gripper_ota_data",
            "session_id": session_id,
            "seq": seq,
            "data": base64.b64encode(chunk).decode("ascii"),
        }
        ack: dict[str, Any] | None = None
        for attempt in range(1, retries + 1):
            check_cancel(should_cancel)
            conn.send_json(request)
            try:
                ack = wait_topic(conn, {"gripper_ota_ack"}, ack_timeout,
                                 should_cancel=should_cancel,
                                 session_id=session_id, seq=seq)
                break
            except TimeoutError:
                log(f"DATA seq={seq} 第{attempt}次等待ACK超时")
        if ack is None:
            raise TimeoutError(f"DATA seq={seq}重试{retries}次仍未收到ACK")
        if ack.get("session_id") != session_id or ack.get("gripper_id") != gripper:
            raise RuntimeError(f"DATA seq={seq}收到其他会话/夹爪的ACK")
        if ack.get("seq") != seq or not ack.get("ok"):
            raise RuntimeError(f"DATA ACK不匹配：expect={seq}, got={ack}")
        expected_offset = offset + len(chunk)
        if ack.get("committed_offset") != expected_offset:
            raise RuntimeError(
                f"持久化进度不匹配：expect={expected_offset}, "
                f"got={ack.get('committed_offset')}"
            )
        if progress is not None:
            progress(expected_offset, image.size)
        if (seq + 1) % 10 == 0 or seq + 1 == total:
            elapsed = max(time.monotonic() - started, 0.001)
            log(
                f"  {seq + 1:3d}/{total}块  {expected_offset * 100 // image.size:3d}%  "
                f"{expected_offset / 1024 / elapsed:5.1f} KiB/s"
            )


def update(conn: JsonFrameSocket, args: argparse.Namespace, *,
           log: Callable[[str], None] = print,
           progress: Callable[[int, int], None] | None = None,
           phase: Callable[[str], None] | None = None,
           should_cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    image = load_and_validate_image(args.bin)
    version_text = ".".join(str(part) for part in parse_version(args.version))
    session_id = secrets.randbits(32) or 1
    log(
        f"固件：{args.bin}\n夹爪：{args.gripper}\n版本：{version_text}\n"
        f"大小：{image.size} bytes\nCRC32/ISO-HDLC：0x{image.crc32:08X}\n"
        f"MSP：0x{image.initial_msp:08X}  Reset：0x{image.reset_handler:08X}\n"
        f"会话：0x{session_id:08X}"
    )

    start_sent = False
    if progress is not None:
        progress(0, image.size)
    try:
        check_cancel(should_cancel)
        if phase is not None:
            phase("checking")
        log("[0/4] 检查主控夹爪OTA命令/回复链路（此时不发送START）...")
        status = query_status(conn, log=log, should_cancel=should_cancel)
        if status.get("control_restore_supported") is not True:
            raise RuntimeError("主控未确认支持OTA后单位恢复；请先烧录20260909-unit1或兼容版本，再升级夹爪。")
        if status.get("control_restore_pending") is True:
            raise RuntimeError("固件已写入，但控制单位尚未恢复；请点击取消当前会话重试配置，不必重新擦写固件。")
        state = status["state"]
        if state != "IDLE" and not (
                state == "RECOVERY_REQUIRED" and status.get("gripper_id") == args.gripper):
            raise RuntimeError(
                f"主控仍有会话：state={state}，gripper_id={status.get('gripper_id')}，"
                "请先查询/取消原会话，再为同一夹爪发送完整BIN。"
            )
        check_cancel(should_cancel)
        if phase is not None:
            phase("preparing")
        log("[1/4] 请求主控进入维护态并让夹爪进入Bootloader ...")
        start_sent = True
        send_start(conn, image, args.gripper, version_text,
                   args.hardware_revision, session_id)
        ready = wait_topic(conn, {"gripper_ota_ready"}, args.ready_timeout,
                           should_cancel=should_cancel, session_id=session_id)
        if ready.get("session_id") != session_id or ready.get("gripper_id") != args.gripper:
            raise RuntimeError(f"READY会话不匹配：{ready}")
        if ready.get("block_size") != BLOCK_SIZE or ready.get("next_offset") != 0:
            raise RuntimeError(f"READY参数异常：{ready}")
        log(f"主控已就绪：Bootloader {ready.get('boot_version')}")

        if phase is not None:
            phase("streaming")
        log("[2/4] 流式发送固件；每块等待夹爪Flash提交 ...")
        send_data(conn, image, session_id, args.gripper, args.ack_timeout, args.retries,
                  log=log, progress=progress, should_cancel=should_cancel)

        check_cancel(should_cancel)
        if phase is not None:
            phase("verifying")
        log("[3/4] 请求夹爪校验整包并提交VALID ...")
        conn.send_json({"topic": "gripper_ota_end", "session_id": session_id})
        result = wait_topic(conn, {"gripper_ota_result"}, args.end_timeout,
                            session_id=session_id)
        if not result.get("ok") or result.get("gripper_id") != args.gripper:
            raise RuntimeError(f"END结果异常：{result}")
        log("夹爪Bootloader已确认向量表、CRC和VALID Metadata")

        if phase is not None:
            phase("rebooting")
        log("[4/4] 等待新Application启动、主控恢复毫度单位并读回确认失能状态 ...")
        boot = wait_topic(conn, {"gripper_ota_boot_ok"}, args.boot_timeout,
                          session_id=session_id)
        if (boot.get("gripper_id") != args.gripper or
                boot.get("version") != version_text or boot.get("app_ready") is not True or
                boot.get("control_ready") is not True or boot.get("unit_cfg") != 0x000B or
                boot.get("mode") != 0 or boot.get("motor_enabled") is not False):
            raise RuntimeError(f"BOOT_OK内容不匹配：{boot}")
        previous = boot.get("previous_unit_cfg")
        previous_text = f"0x{previous:04X}" if isinstance(previous, int) and previous != 0xFFFF else "未读到"
        log(f"控制单位恢复：UNIT_CFG {previous_text} → 0x000B；MIT模式，电机保持失能。")
        log(f"升级完成：夹爪{args.gripper} Application {boot.get('version')} 已运行；等待新的运动命令。")
        return boot
    except BaseException:
        if start_sent:
            if phase is not None:
                phase("cancelling")
            try:
                conn.send_json({"topic": "gripper_ota_cancel", "session_id": session_id})
                cancelled = wait_topic(conn, {"gripper_ota_cancelled"}, args.ready_timeout,
                                        ignore_errors=True)
                log(
                    "已请求取消；"
                    f"recovery_required={cancelled.get('recovery_required')}"
                )
                if cancelled.get("control_restore_pending"):
                    log("固件已校验；控制配置仍未恢复，保持维护态，可重试取消会话以恢复配置，无需立即重刷BIN。")
                elif cancelled.get("control_ready"):
                    log("控制配置重试已通过，电机保持失能；请先查询状态，再按正常流程发送新命令。")
                elif cancelled.get("recovery_required"):
                    log("Application可能已擦除：机器人必须保持维护态，请重新发送完整固件。")
            except Exception as cancel_error:
                log(f"取消未确认：{cancel_error}。请重连后查询会话状态，必要时重发完整BIN。")
        raise


def query(conn: JsonFrameSocket, *, log: Callable[[str], None] = print) -> dict[str, Any]:
    status = query_status(conn, log=log)
    log(json.dumps(status, ensure_ascii=False, indent=2))
    return status


def cancel(conn: JsonFrameSocket, session_id: int = 0, *,
           log: Callable[[str], None] = print) -> dict[str, Any]:
    if not session_id:
        conn.send_json({"topic": "gripper_ota_query"})
        status = wait_topic(conn, {"gripper_ota_status"}, 5.0)
        session_id = int(status.get("session_id", 0))
        if not session_id:
            log(json.dumps(status, ensure_ascii=False, indent=2))
            return status
    request: dict[str, Any] = {
        "topic": "gripper_ota_cancel",
        "session_id": session_id,
    }
    conn.send_json(request)
    status = wait_topic(conn, {"gripper_ota_cancelled"}, 12.0)
    log(json.dumps(status, ensure_ascii=False, indent=2))
    return status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="机器人主控流式转发夹爪OTA工具")
    parser.add_argument("--gui", action="store_true", help="打开图形界面（不带参数时默认打开）")
    parser.add_argument("--action", choices=("update", "query", "cancel"), default="update")
    parser.add_argument("--bin", type=Path, help="夹爪Application BIN（链接地址0x08006000）")
    parser.add_argument("--gripper", type=int, choices=(1, 2), default=1)
    parser.add_argument("--version", help="目标版本Major.Minor.Patch，例如1.15.1")
    parser.add_argument("--hardware-revision", type=int, default=0, choices=range(0, 256), metavar="0..255")
    parser.add_argument("--host", default=DEFAULT_HOST, help="本机监听地址，默认0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--accept-timeout", type=float, default=120.0)
    parser.add_argument("--ready-timeout", type=float, default=30.0)
    parser.add_argument("--ack-timeout", type=float, default=12.0)
    parser.add_argument("--end-timeout", type=float, default=20.0)
    parser.add_argument("--boot-timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--session-id", type=lambda value: int(value, 0), default=0,
                        help="cancel时可指定原会话ID")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if len(sys.argv) == 1 or args.gui:
        from gripper_ota_tcp_gui import main as gui_main
        return gui_main()
    if args.action == "update" and (args.bin is None or args.version is None):
        raise SystemExit("update操作必须同时提供--bin和--version")
    if not 1 <= args.port <= 65535 or args.retries < 1:
        raise SystemExit("端口或重试次数无效")

    server = create_server(args.host, args.port)
    conn: JsonFrameSocket | None = None
    try:
        conn = accept_robot(server, args.accept_timeout)
        if args.action == "update":
            update(conn, args)
        elif args.action == "query":
            query(conn)
        else:
            cancel(conn, args.session_id)
        return 0
    except (KeyboardInterrupt, Exception) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
