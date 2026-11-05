#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
وسيط طباعة جرد الذهب (Jard Print Bridge) - الإصدار 1.0
يستقبل أوامر الطابعة الخام (TSPL / ZPL / ESC-POS) من منصة جرد الذهب في المتصفح
ويمررها إلى الطابعة، لأن المتصفح لا يستطيع الاتصال بعنوان IP مباشرة.

الاستخدام (لا يحتاج تثبيت مكتبات، Python 3.8 فأحدث):
    python jard-print-bridge.py                      تشغيل على المنفذ 9101
    python jard-print-bridge.py --token سري123       اشتراط رمز في كل طلب
    python jard-print-bridge.py --origin https://موقعك.com   السماح لموقعك فقط

وضع الشبكة:  المنصة ترسل عنوان IP والمنفذ (غالبًا 9100) والوسيط يمرر البيانات.
وضع الطابور: يمرر البيانات لطابعة مثبّتة على الجهاز بتعريفها (ويندوز: يجب مشاركة الطابعة
              باسم مشاركة، لينكس/ماك: أمر lp -o raw).
الأمان: يستمع على 127.0.0.1 فقط، ويقبل عناوين الشبكة المحلية فقط، ولا يحفظ أي بيانات.
"""
import argparse
import ipaddress
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0"
MAX_BYTES = 20 * 1024 * 1024


def check_host(host):
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except Exception:
        raise ValueError("تعذّر تحديد عنوان الطابعة: " + host)
    if not (ip.is_private or ip.is_loopback or ip.is_link_local):
        raise ValueError("يُسمح بعناوين الشبكة المحلية فقط")
    return str(ip)


def send_tcp(host, port, data):
    ip = check_host(host)
    if not (1 <= port <= 65535):
        raise ValueError("منفذ غير صالح")
    with socket.create_connection((ip, port), timeout=6) as s:
        s.settimeout(15)
        s.sendall(data)
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def send_queue(name, data):
    if not re.fullmatch(r"[\w .\-()]{1,80}", name, re.UNICODE):
        raise ValueError("اسم الطابعة يحتوي رموزًا غير مسموحة")
    fd, path = tempfile.mkstemp(prefix="jard-", suffix=".prn")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if platform.system() == "Windows":
            target = "\\\\localhost\\" + name
            r = subprocess.run(["cmd", "/c", "copy", "/b", path, target], capture_output=True, timeout=30)
        else:
            r = subprocess.run(["lp", "-d", name, "-o", "raw", path], capture_output=True, timeout=30)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or b"").decode(errors="ignore").strip()
            raise RuntimeError("فشل الإرسال إلى طابور الطباعة: " + (msg or str(r.returncode)))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "JardBridge/" + VERSION

    def log_message(self, fmt, *args):
        sys.stderr.write("[bridge] " + (fmt % args) + "\n")

    def _cors(self):
        origin = self.headers.get("Origin")
        allowed = self.server.origins
        if allowed:
            allow = origin if origin in allowed else allowed[0]
        else:
            allow = origin or "*"
        self.send_header("Access-Control-Allow-Origin", allow)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Printer-Host, X-Printer-Port, X-Printer-Name, X-Bridge-Token")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] == "/ping":
            return self._json(200, {"ok": True, "bridge": VERSION, "os": platform.system()})
        self._json(404, {"error": "غير موجود"})

    def do_POST(self):
        if self.path.split("?")[0] != "/print":
            return self._json(404, {"error": "غير موجود"})
        if self.server.token and self.headers.get("X-Bridge-Token") != self.server.token:
            return self._json(401, {"error": "رمز الوسيط غير صحيح"})
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BYTES:
            return self._json(413, {"error": "حجم البيانات غير صالح"})
        data = self.rfile.read(n)
        host = (self.headers.get("X-Printer-Host") or "").strip()
        name = (self.headers.get("X-Printer-Name") or "").strip()
        try:
            if host:
                send_tcp(host, int(self.headers.get("X-Printer-Port", "9100") or 9100), data)
            elif name:
                send_queue(name, data)
            else:
                return self._json(400, {"error": "حدّد عنوان الطابعة أو اسمها"})
        except Exception as e:  # noqa
            return self._json(502, {"error": str(e)})
        self._json(200, {"ok": True, "bytes": len(data)})


def main():
    ap = argparse.ArgumentParser(description="وسيط طباعة جرد الذهب")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9101)
    ap.add_argument("--token", default="")
    ap.add_argument("--origin", action="append", default=[], help="السماح لموقع محدد فقط (يمكن تكراره)")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.token = a.token
    srv.origins = a.origin
    print("وسيط الطباعة يعمل على http://%s:%d  (اضغط Ctrl+C للإيقاف)" % (a.host, a.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nتم الإيقاف")


if __name__ == "__main__":
    main()
