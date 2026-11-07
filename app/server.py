#!/usr/bin/env python3
"""OES 系统盘管家 —— Web 服务（Unix Socket + fnOS 统一网关）。

只依赖 Python 标准库。所有请求路径自动兼容网关前缀 /app/oesboot。
"""
import json
import mimetypes
import os
import posixpath
import socketserver
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import guard, ops, sysinfo  # noqa: E402

APP_NAME = "oesboot"
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
PREFIXES = ("/app/%s" % APP_NAME, "/%s" % APP_NAME)
MAX_BODY = 256 * 1024


def strip_prefix(path):
    for pfx in PREFIXES:
        if path == pfx:
            return "/"
        if path.startswith(pfx + "/"):
            return path[len(pfx):]
    return path


class Handler(BaseHTTPRequestHandler):
    server_version = "OESBoot/1.0"

    # ---- 基础响应 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": msg}, code)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---- 鉴权 ----
    def _is_admin(self):
        admin = self.headers.get("X-Trim-Isadmin")
        if admin is None:
            # 允许本机直接访问（socket 权限已限制），但网关请求必须带头
            return True
        return admin.lower() in ("true", "1", "yes")

    # ---- 路由 ----
    def do_GET(self):
        try:
            path = strip_prefix(urllib.parse.urlparse(self.path).path)
            # keep_blank_values：前端用的是 ?download（不带值），
            # 默认解析会把这个键直接丢掉，导致下载接口返回成 JSON。
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query,
                                          keep_blank_values=True)
            if path.startswith("/api/"):
                return self._api_get(path[5:], query)
            return self._static(path)
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._err("服务器内部错误", 500)

    def do_POST(self):
        try:
            path = strip_prefix(urllib.parse.urlparse(self.path).path)
            if not self._is_admin():
                return self._err("需要管理员权限", 403)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._err("请求体过大", 413)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                return self._err("请求体不是合法 JSON")
            return self._api_post(path[5:] if path.startswith("/api/") else path, body)
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._err("服务器内部错误", 500)

    # ---- 静态文件 ----
    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        safe = posixpath.normpath(path).lstrip("/")
        if safe.startswith("..") or "/../" in safe:
            return self._err("非法路径", 403)
        full = os.path.join(UI_DIR, safe)
        if not os.path.isfile(full):
            return self._err("Not Found", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype)

    # ---- API: GET ----
    def _api_get(self, name, query):
        if name == "status":
            info = sysinfo.collect()
            job = ops.running_job()
            info["job"] = job.to_dict() if job else None
            info["removable"] = None
            return self._json({"ok": True, "data": info})
        if name == "upgrade":
            info = sysinfo.collect()
            return self._json({"ok": True, "data": info.get("upgrade")})
        if name == "diagnose":
            info = sysinfo.collect()
            text = sysinfo.diagnose_text(info)
            if "download" in query:
                return self._send(200, text, "text/plain; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="oesboot-diagnose.txt"'})
            return self._json({"ok": True, "data": text})
        if name == "ota-report":
            path = sysinfo.OTA_REPORT
            # build=1：先按当前状态抓一份现场再给。窗口进行中也能抓，
            # 不需要等结算——「我想现在就看现场」是最常见的使用场景。
            if "build" in query:
                try:
                    ops.build_ota_report()
                except Exception as exc:  # noqa: BLE001
                    return self._err("生成升级现场失败：%s" % exc, 500)
            if not os.path.isfile(path):
                return self._err("还没有升级现场。先点一次系统升级（或点「立即抓取现场」再试）。", 404)
            text = sysinfo.read_file(path)
            # save=1：同时往存储空间（文件管理器能看到的目录）存一份，
            # 手机浏览器下载到哪儿不好找，存到共享文件夹里更靠谱。
            saved = None
            if "save" in query:
                saved = ops.save_report_copy()
            if "download" in query:
                fname = ops.report_filename()
                return self._send(200, text, "text/plain; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="%s"' % fname})
            return self._json({"ok": True, "data": text, "saved": saved})
        if name == "log":
            n = int((query.get("n") or ["200"])[0])
            lines = (sysinfo.read_file("/var/log/oesboot-guard.log").splitlines())[-n:]
            return self._json({"ok": True, "data": lines})
        if name.startswith("job/"):
            job = ops.get_job(name[4:])
            if not job:
                return self._err("任务不存在或已过期", 404)
            return self._json({"ok": True, "data": job.to_dict()})
        if name == "disks":
            info = sysinfo.collect()
            return self._json({"ok": True, "data": info["disks"]})
        return self._err("未知接口", 404)

    # ---- API: POST ----
    def _api_post(self, name, body):
        if name == "sync":
            # 窗口内让路是这套机制的核心。手动同步是唯一能绕开它的口子，
            # 在 OTA 写入途中点一下，等于把刚赶走的争用又请回来。
            if (sysinfo._ota_window() or {}).get("active"):
                return self._err("升级守护正在让路：升级窗口内不碰引导分区，以免和升级程序抢。"
                                 "等升级完成（或点「结束监视」）后再手动同步。")
            job = ops.start_job("sync", "同步引导分区", lambda j: ops.action_sync(j))
            return self._json({"ok": True, "data": job.to_dict()})
        if name == "repair":
            job = ops.start_job("repair", "检查并修复启动链", lambda j: ops.action_repair(j))
            return self._json({"ok": True, "data": job.to_dict()})
        if name == "upgrade/fix":
            job = ops.start_job("fix", "修复升级前置条件", lambda j: ops.action_upgrade_fix(j))
            return self._json({"ok": True, "data": job.to_dict()})
        if name == "dpkgfix":
            job = ops.start_job("dpkgfix", "修复 dpkg 配置", lambda j: ops.action_dpkgfix(j))
            return self._json({"ok": True, "data": job.to_dict()})
        if name == "hybrid":
            if body.get("confirm") != "HYBRID":
                return self._err("确认字符串不匹配")
            job = ops.start_job("hybrid", "修正引导布局（/boot 挂到 eMMC）",
                                lambda j: ops.action_hybrid(j))
            return self._json({"ok": True, "data": job.to_dict()})
        if name == "hybrid-rollback":
            if body.get("confirm") != "HYBRID":
                return self._err("确认字符串不匹配")
            job = ops.start_job("hybrid-rollback", "回滚引导布局",
                                lambda j: ops.action_hybrid_rollback(j))
            return self._json({"ok": True, "data": job.to_dict()})
        if name == "upgrade/watch":
            action = (body.get("action") or "start").lower()
            if action == "stop":
                return self._json({"ok": True, "data": guard.disable_watch()})
            info = sysinfo.collect()
            up = info.get("upgrade") or {}
            # 只拦"真会让装包失败"的 error 项（dpkg 配置坏掉这类）。
            # 布局不规范只是提醒——它不影响装包，挡住反而让人卡在
            # "启动不了→去点一键修复→修复又不处理布局"的死循环里。
            blockers = [c["label"] for c in up.get("checks", [])
                        if not c["ok"] and c.get("level") == "error"]
            if blockers:
                return self._err("还有 %d 项必须先处理：%s" % (len(blockers), "、".join(blockers)))
            return self._json({"ok": True, "data": guard.enable_watch()})
        if name == "switch":
            target = (body.get("target") or "").lower()
            if body.get("confirm") != target:
                return self._err("确认字符串不匹配")
            job = ops.start_job("switch", "切换启动盘到 %s" % target.upper(),
                                lambda j: ops.action_switch(j, target))
            return self._json({"ok": True, "data": job.to_dict()})
        if name == "migrate":
            disk = body.get("disk") or ""
            if not disk.startswith("/dev/"):
                return self._err("请提供目标磁盘路径")
            if body.get("confirm") != "MIGRATE":
                return self._err("确认字符串不匹配")
            size_gb = body.get("size_gb")
            job = ops.start_job("migrate", "迁移系统到 %s" % disk,
                                lambda j: ops.action_migrate(j, disk, size_gb))
            return self._json({"ok": True, "data": job.to_dict()})
        if name == "policy":
            conf = ops.set_policy(
                lock=body.get("lock"),
                auto_repair=body.get("auto_repair"),
                auto_reboot=body.get("auto_reboot"),
                enabled=body.get("enabled"),
            )
            return self._json({"ok": True, "data": conf})
        if name == "guard/install":
            report = ops.install_guard()
            return self._json({"ok": True, "data": report})
        if name == "reboot":
            return self._json({"ok": True, "data": ops.reboot(int(body.get("delay") or 0))})
        return self._err("未知接口", 404)


class ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class ThreadedTCPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    default_sock = os.path.join(os.environ.get("TRIM_APPDEST", "/var/apps/%s/target" % APP_NAME), "app.sock")
    sock = os.environ.get("OESBOOT_SOCKET", default_sock)
    port = os.environ.get("OESBOOT_PORT")

    # 启动即自愈：1.1.13 及以前的 dpkg 钩子可能把整台机器的包管理卡死，
    # 表现是系统升级 4 秒内报 failed to install、内核压根没解包。
    # 这里静默修一次，用户不用知道原理，升级就能过。
    def _heal():
        try:
            broken, line, _ = guard.dpkg_cfg_problem()
            if broken:
                ok, note = guard.repair_dpkg_cfg()
                print("[selfheal] dpkg 配置异常：%s → %s（%s）" % (line, "已恢复" if ok else "未恢复", note),
                      flush=True)
        except Exception:  # noqa: BLE001 - 自愈失败不能影响服务启动
            pass
    threading.Thread(target=_heal, daemon=True).start()

    if port:
        srv = ThreadedTCPServer(("127.0.0.1", int(port)), Handler)
        print("listening on 127.0.0.1:%s" % port, flush=True)
    else:
        try:
            if os.path.exists(sock):
                os.unlink(sock)
        except OSError:
            pass
        os.makedirs(os.path.dirname(sock), exist_ok=True)
        srv = ThreadedUnixServer(sock, Handler)
        os.chmod(sock, 0o660)
        print("listening on unix:%s" % sock, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
