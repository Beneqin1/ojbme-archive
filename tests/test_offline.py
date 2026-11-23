#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线端到端测试：用本地 mock OJ 验证抓取、去重、增量、生成文件和 git 行为。

运行：
    python -m unittest discover -s tests -v
    python tests/test_offline.py
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 源码布局：bmeoj_archive.py 在仓库根目录；发行包布局：在 files/ 下。
for _base in (ROOT, os.path.join(ROOT, "files")):
    if os.path.isfile(os.path.join(_base, "bmeoj_archive.py")):
        sys.path.insert(0, _base)
        break
else:
    raise SystemExit("找不到 bmeoj_archive.py（根目录或 files/）")

import bmeoj_archive as archive  # noqa: E402

COOKIE = "PHPSESSID=test-cookie"


def row_html(sid, cid, pid, result_code, result_text, language, submitted_at, problem_no):
    return """
<tr class='evenrow'>
<td>%s</td>
<td><a href="userinfo.php?user=alice">alice</a></td>
<td><div class=center><a href="problem.php?cid=%s&amp;pid=%s">%s</div></a></td>
<td><span class='hidden' result='%s'>%s</span></td>
<td>1234</td>
<td>12</td>
<td>%s</td>
<td>456</td>
<td>%s</td>
<td>1</td>
</tr>""" % (sid, cid, pid, problem_no, result_code, result_text, language, submitted_at)


STATUS_HTML = """<!DOCTYPE html><html><body>
<table>
<tr class='toprow'><th>RunID</th><th>User</th><th>Problem</th><th>Result</th>
<th>Memory</th><th>Time</th><th>Language</th><th>Code Length</th><th>Submit Time</th><th>Judger</th></tr>
%s
%s
%s
%s
</table></body></html>""" % (
    row_html(30, 1000, 0, "4", "Accepted", "G++", "2026-11-21 20:19:28", 1000),
    row_html(25, 1000, 0, "5", "Wrong Answer", "G++", "2026-11-21 20:15:00", 1000),
    row_html(20, 1000, 0, "4", "Accepted", "G++", "2026-11-21 20:10:00", 1000),
    row_html(10, 1001, 1, "4", "Accepted", "G++", "2026-11-21 20:05:00", 1001),
)

SOURCE_CODE = """#include <iostream>
int main() {
    std::cout << "hello" << std::endl;
    return 0;
}
/*****
This footer is added by the OJ and must be stripped.
*****/
"""


class MockOJHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.server.requests.append(self.path)
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        cookie = self.headers.get("Cookie", "")

        if parsed.path == "/userinfo.php":
            user = (query.get("user") or [""])[0]
            if user == "ghost":
                return self._send("<html><body>No such User!</body></html>")
            return self._send(
                "<html><body><h2>%s--Alice</h2><p>Solved Problems List</p></body></html>" % user
            )

        if parsed.path == "/status.php":
            user = (query.get("user_id") or [""])[0]
            if user == "noac":
                body = """<!DOCTYPE html><html><body><table>
<tr class='toprow'><th>RunID</th><th>User</th><th>Problem</th><th>Result</th>
<th>Memory</th><th>Time</th><th>Language</th><th>Code Length</th><th>Submit Time</th><th>Judger</th></tr>
%s
</table></body></html>""" % row_html(
                    50, 1002, 0, "5", "Wrong Answer", "G++", "2026-11-22 09:00:00", 1002
                )
                return self._send(body)
            return self._send(STATUS_HTML)

        if parsed.path == "/problem.php":
            cid = (query.get("cid") or [""])[0]
            pid = (query.get("pid") or [""])[0]
            title = "A+B问题" if (cid, pid) == ("1000", "0") else "排序"
            letter = chr(ord("A") + int(pid))
            return self._send("<html><body><h2>Problem %s: %s</h2></body></html>" % (letter, title))

        if parsed.path == "/showsource.php":
            if cookie != COOKIE:
                return self._send("<html><body>I am sorry, You could not view this code!</body></html>")
            import html as html_lib

            return self._send("<html><body><pre>%s</pre></body></html>" % html_lib.escape(SOURCE_CODE))

        if parsed.path == "/modifypage.php":
            if cookie == COOKIE:
                return self._send("<html><body><a href='logout.php'>Logout</a></body></html>")
            return self._send("<html><body>Please login</body></html>")

        return self._send("<html><body>not found</body></html>", 404)


class OfflineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockOJHandler)
        cls.server.requests = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        # 必须放在工作区内：DSH 沙箱只允许写 workspace 和部分临时目录。
        # 注意：Python 3.15 在 Windows 上 os.mkdir(mode=0o700) 会产生子目录不可写的
        # ACL，因此不用 tempfile.mkdtemp（内部用 0700），改用默认权限的普通目录。
        self.tmp = os.path.join(ROOT, ".bmeoj-test-%s" % uuid.uuid4().hex[:10])
        os.makedirs(self.tmp)
        self.out = os.path.join(self.tmp, "output")
        self.server.requests[:] = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ #
    def run_main(self, extra, cookie=COOKIE, user="alice"):
        args = [
            "--user", user,
            "--dir", self.out,
            "--base-url", self.base,
            "--interval", "0",
            "--config", os.path.join(self.tmp, "none.toml"),
            "--local-config", os.path.join(self.tmp, "none.local.toml"),
        ] + extra
        old = os.environ.get("BMEOJ_COOKIE")
        if cookie:
            os.environ["BMEOJ_COOKIE"] = cookie
        else:
            os.environ.pop("BMEOJ_COOKIE", None)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = archive.main(args)
        finally:
            if old is None:
                os.environ.pop("BMEOJ_COOKIE", None)
            else:
                os.environ["BMEOJ_COOKIE"] = old
        return code, out.getvalue() + err.getvalue()

    def read(self, *parts):
        with open(os.path.join(self.out, *parts), encoding="utf-8") as fh:
            return fh.read()

    # ------------------------------------------------------------------ #
    def test_parse_status_page(self):
        rows = archive.parse_status_page(STATUS_HTML)
        self.assertEqual([r["sid"] for r in rows], ["30", "25", "20", "10"])
        self.assertEqual(rows[0]["cid"], "1000")
        self.assertEqual(rows[0]["pid"], 0)
        self.assertTrue(rows[0]["accepted"])
        self.assertFalse(rows[1]["accepted"])
        self.assertEqual(rows[3]["pid"], 1)
        self.assertEqual(rows[0]["language"], "G++")
        self.assertEqual(rows[0]["submitted_at"], "2026-11-21 20:19:28")

    def test_clean_code_and_filename(self):
        cleaned = archive.clean_code(SOURCE_CODE)
        self.assertIn("int main()", cleaned)
        self.assertNotIn("must be stripped", cleaned)
        self.assertEqual(
            archive.sanitize_filename('a<b>c:d/e\\f?.cpp'),
            "a_b_c_d_e_f_.cpp",
        )
        ctx = {"id": "1000", "part": "A", "title": "A+B", "slug": "a-b",
               "user": "alice", "submit_id": "30", "pid": 0, "letter": "A",
               "problem_id": "1000", "date": "2026-11-21"}
        self.assertEqual(archive.render_filename("{id} P{part}.cpp", ctx, "latest", "30"), "1000 PA.cpp")
        self.assertEqual(
            archive.render_filename("{id} P{part}.cpp", ctx, "all", "30"),
            "1000 PA_sid30.cpp",
        )
        with self.assertRaises(archive.ConfigError):
            archive.render_filename("{unknown}.cpp", ctx, "latest", "30")

    def test_end_to_end_latest(self):
        code, out = self.run_main([])
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "1000", "1000 PA.cpp")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "1001", "1001 PB.cpp")))
        body = self.read("1000", "1000 PA.cpp")
        self.assertIn("int main()", body)
        self.assertNotIn("must be stripped", body)

        manifest = json.loads(self.read("manifest.json"))
        entries = {e["submit_id"]: e for e in manifest["entries"]}
        self.assertEqual(set(entries), {"30", "10"})
        self.assertEqual(entries["30"]["title"], "A+B问题")
        self.assertEqual(entries["30"]["status"], "Accepted")
        self.assertEqual(entries["30"]["filename"], "1000/1000 PA.cpp")
        self.assertIn("problem.php?cid=1000", entries["30"]["problem_url"])

        self.assertIn("A+B问题", self.read("README.md"))
        self.assertIn("bmeoj-archive: generated", self.read("README.md"))
        self.assertIn("免责", self.read("README.md"))
        self.assertIn("cpp", self.read("topics.txt"))
        self.assertIn("config.local.toml", self.read(".gitignore"))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "repo-description.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "manifest.csv")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "config.example.toml")))
        self.assertIn("新增 2", out)

    def test_second_run_is_incremental(self):
        code, out = self.run_main([])
        self.assertEqual(code, 0, out)
        before = self.read("1000", "1000 PA.cpp")
        self.server.requests[:] = []
        code, out = self.run_main([])
        self.assertEqual(code, 0, out)
        self.assertIn("跳过 2", out)
        self.assertEqual(self.read("1000", "1000 PA.cpp"), before)
        self.assertFalse(any("showsource.php" in p for p in self.server.requests),
                         self.server.requests)
        self.assertFalse(any("problem.php" in p for p in self.server.requests),
                         self.server.requests)

    def test_all_mode_keeps_every_ac(self):
        code, out = self.run_main(["--all"])
        self.assertEqual(code, 0, out)
        manifest = json.loads(self.read("manifest.json"))
        self.assertEqual(len(manifest["entries"]), 3)
        names = sorted(os.listdir(os.path.join(self.out, "1000")))
        self.assertEqual(names, ["1000 PA_sid20.cpp", "1000 PA_sid30.cpp"])
        self.assertIn("新增 3", out)

    def test_all_mode_adds_new_ac_incrementally(self):
        """先 latest 再 --all：同题新增的 AC 应新增文件，而不是被当成覆盖。"""
        code, out = self.run_main([])
        self.assertEqual(code, 0, out)
        global STATUS_HTML
        old_html = STATUS_HTML
        try:
            extra = row_html(40, 1000, 0, "4", "Accepted", "G++", "2026-11-22 10:00:00", 1000)
            STATUS_HTML = STATUS_HTML.replace("</table>", extra + "</table>")
            code, out = self.run_main(["--all"])
        finally:
            STATUS_HTML = old_html
        self.assertEqual(code, 0, out)
        names = sorted(os.listdir(os.path.join(self.out, "1000")))
        self.assertEqual(
            names, ["1000 PA.cpp", "1000 PA_sid20.cpp", "1000 PA_sid40.cpp"]
        )
        manifest = json.loads(self.read("manifest.json"))
        self.assertEqual(len(manifest["entries"]), 4)

    def test_force_redownloads_and_backs_up(self):
        code, out = self.run_main([])
        self.assertEqual(code, 0, out)
        path = os.path.join(self.out, "1000", "1000 PA.cpp")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("// local edit\n")
        code, out = self.run_main(["--force"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("// local edit", self.read("1000", "1000 PA.cpp"))
        backups = []
        for root, _, files in os.walk(os.path.join(self.out, "_backup")):
            backups.extend(files)
        self.assertTrue(backups)
        self.assertIn("已更新", out)

    def test_no_ac_dry_run_writes_nothing(self):
        code, out = self.run_main(["--dry-run"], user="noac")
        self.assertEqual(code, 0, out)
        self.assertIn("没有 AC", out)
        self.assertFalse(os.path.exists(self.out))

    def test_public_config_rejects_cookie(self):
        cfg = os.path.join(self.tmp, "config.toml")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write('[oj]\ncookie = "PHPSESSID=oops"\n')
        code, out = self.run_main(["--config", cfg])
        self.assertEqual(code, 2, out)
        self.assertIn("config.toml", out)
        self.assertIn("cookie", out.lower())

    def test_no_cookie_gives_clear_error(self):
        code, out = self.run_main([], cookie=None)
        self.assertEqual(code, 4, out)
        self.assertIn("Cookie", out)
        self.assertIn("BMEOJ_COOKIE", out)
        cpp = []
        for root, _, files in os.walk(self.out):
            cpp.extend(name for name in files if name.endswith(".cpp"))
        self.assertEqual(cpp, [])

    def test_user_not_found(self):
        args = [
            "--user", "ghost", "--dir", self.out, "--base-url", self.base,
            "--interval", "0", "--config", os.path.join(self.tmp, "none.toml"),
            "--local-config", os.path.join(self.tmp, "none.local.toml"),
        ]
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = archive.main(args)
        self.assertEqual(code, 5)
        self.assertIn("用户不存在", out.getvalue())

    def test_dry_run_writes_nothing(self):
        code, out = self.run_main(["--dry-run"])
        self.assertEqual(code, 0, out)
        self.assertIn("dry-run", out)
        self.assertFalse(os.path.exists(self.out))

    def test_probe_without_dir(self):
        """--probe 不应要求 --dir，也不应创建目标目录。"""
        args = [
            "--probe", "--user", "alice", "--base-url", self.base,
            "--interval", "0", "--config", os.path.join(self.tmp, "none.toml"),
            "--local-config", os.path.join(self.tmp, "none.local.toml"),
        ]
        old = os.environ.get("BMEOJ_COOKIE")
        os.environ["BMEOJ_COOKIE"] = COOKIE
        out = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(out):
                code = archive.main(args)
        finally:
            if old is None:
                os.environ.pop("BMEOJ_COOKIE", None)
            else:
                os.environ["BMEOJ_COOKIE"] = old
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("探测完成", out.getvalue())
        self.assertFalse(os.path.exists(self.out))

    def test_init_git_only_local(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        code, out = self.run_main(["--init-git"])
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isdir(os.path.join(self.out, ".git")))
        remotes = subprocess.run(
            ["git", "remote", "-v"], cwd=self.out, capture_output=True, text=True
        )
        self.assertEqual(remotes.stdout.strip(), "")
        self.assertIn("git remote add origin", out)
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.out, capture_output=True, text=True
        )
        self.assertEqual(status.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
