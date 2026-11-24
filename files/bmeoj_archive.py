#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""bmeoj_archive.py —— 把 oj.bmeonline.cn 上某个用户的 AC/正确提交导出成
可发布到 GitHub 的本地项目目录。

设计目标
========
* 只读 OJ：只抓取指定用户“正确/Accepted”的提交源码，不提交、不评分、不改站点。
* 只写本地：默认增量；已有文件不覆盖，除非显式 --overwrite / --force。
* 不自动 push、不自动创建远程仓库：--init-git 只做 git init/add/commit。
* 鉴权：优先使用 OJ 用户名 + 密码登录（标准库 http.cookiejar 维持会话）；
  也可配置 Cookie 作为备选。密码/Cookie 只从环境变量或 config.local.toml 读取，
  绝不写入 config.toml、README、manifest、日志或任何提交文件。
* 零必需第三方依赖：Python 3.8+ 标准库即可运行。

用法示例
========
    python bmeoj_archive.py
    python bmeoj_archive.py --user 11226128 --dir output
    python bmeoj_archive.py --user 11226128 --all
    python bmeoj_archive.py --user 11226128 --force --overwrite
    python bmeoj_archive.py --user 11226128 --init-git
    python bmeoj_archive.py --probe --user 11226128 --dump-html

密码输入（按优先级）
====================
1. 环境变量 BMEOJ_PASSWORD
2. config.local.toml 的 [oj] password（该文件已被 .gitignore 忽略）
3. 交互式 getpass 输入（不回显；仅当标准输入是终端时）
命令行不提供 --password，避免密码进入 shell 历史。

Cookie 备选方式
================
若配置了 BMEOJ_COOKIE 或 config.local.toml 的 [oj] cookie，则优先使用 Cookie
并跳过密码登录。Cookie 获取：在你自己已登录的浏览器中 F12 -> Network ->
复制 Request Headers 的 Cookie 整行。不要把密码/Cookie 写进 config.toml、
README、命令行历史或任何会提交的文件。
"""

import argparse
import csv
import getpass
import hashlib
import html as html_lib
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

VERSION = "1.1.0"
SCHEMA_VERSION = 1
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASE = "http://oj.bmeonline.cn"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)
ACCEPTED_CODE = "4"
ACCEPTED_WORDS = ("accepted", "correct", "正确", "通过", "答案正确")

# 文件末尾 OJ 自动追加的分隔注释（旧脚本规则：/***** 那一行及以下丢弃）
_FOOTER_RE = re.compile(r"^(?:/{4,}|\*{5,}|//\*{4,}|/\*{4,})\s*$")
_ILLEGAL_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

_ACTIVE_COOKIE = ""
_ACTIVE_SECRETS = []
PRIVATE_AUTH_KEYS = ("cookie", "password", "passwd")


# --------------------------------------------------------------------------- #
# 错误类型
# --------------------------------------------------------------------------- #
class ArchiveError(Exception):
    """可预期的错误：打印中文说明后退出，不抛 traceback。"""


class ConfigError(ArchiveError):
    pass


class NetworkError(ArchiveError):
    pass


class AuthError(ArchiveError):
    pass


class UserNotFound(ArchiveError):
    pass


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def remember_secret(value):
    """记住本次运行中的秘密，供日志脱敏使用；不写盘。"""
    value = str(value or "")
    if value and value not in _ACTIVE_SECRETS:
        _ACTIVE_SECRETS.append(value)


def redact(text):
    """日志脱敏：Cookie/Token/密码一律替换成 ***。"""
    text = str(text)
    for secret in _ACTIVE_SECRETS:
        text = text.replace(secret, "***")
    if _ACTIVE_COOKIE:
        text = text.replace(_ACTIVE_COOKIE, "***")
    text = re.sub(
        r"(?i)((?:cookie|authorization|token|password|passwd)\s*[:=]\s*)[^\s,;]+",
        r"\1***",
        text,
    )
    text = re.sub(
        r"(?i)\b(PHPSESSID|sessionid|session_id|token|auth)\s*=\s*[^;\s]+",
        r"\1=***",
        text,
    )
    return text


class Logger(object):
    def __init__(self, verbose=True):
        self.lines = []
        self.path = None
        self.verbose = verbose

    def _emit(self, prefix, message):
        line = "[%s] %s%s" % (time.strftime("%H:%M:%S"), prefix, redact(message))
        self.lines.append(line)
        if self.verbose:
            try:
                print(line)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode("ascii"))

    def info(self, message):
        self._emit("", message)

    def step(self, message):
        self._emit(">> ", message)

    def warn(self, message):
        self._emit("[警告] ", message)

    def error(self, message):
        self._emit("[错误] ", message)

    def attach(self, path):
        self.path = path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            self.path = None

    def flush(self):
        if not self.path:
            return None
        try:
            with open(self.path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(self.lines) + "\n")
        except OSError:
            return None
        return self.path


def atomic_write(path, content, newline="\n"):
    """原子写文本；返回 'created' / 'updated' / 'unchanged'。"""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if newline == "\r\n":
        content = content.replace("\n", "\r\n")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    existed = os.path.isfile(path)
    if existed:
        try:
            with open(path, "rb") as fh:
                old = fh.read()
        except OSError:
            old = None
        expected = content.encode("utf-8")
        if old == expected:
            return "unchanged"
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return "updated" if existed else "created"


def strip_tags(fragment):
    fragment = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", fragment or "")
    fragment = re.sub(r"(?is)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?is)</(?:p|div|tr|li|h[1-6])>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return html_lib.unescape(fragment)


def plain_text(fragment):
    return re.sub(r"[ \t\r\f\v]+", " ", strip_tags(fragment)).strip()


def norm_space(text):
    return re.sub(r"\s+", " ", text or "").strip()


def slugify(text):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "problem"


def part_letter(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "X"
    if 0 <= pid <= 25:
        return chr(ord("A") + pid)
    return "X%d" % pid


def sanitize_filename(name, default="unnamed.cpp"):
    name = _ILLEGAL_FILENAME_RE.sub("_", name or "")
    name = name.replace("\r", " ").replace("\n", " ").strip().rstrip(". ")
    if not name:
        name = default
    stem, ext = os.path.splitext(name)
    stem = stem.strip().rstrip(". ") or "unnamed"
    if stem.upper() in _RESERVED_NAMES:
        stem = "_" + stem
    if len(stem) > 120:
        stem = stem[:120].rstrip(". ")
    ext = ext if ext else ".cpp"
    return stem + ext


def render_filename(template, ctx, mode, sid):
    try:
        name = template.format(**ctx)
    except KeyError as exc:
        raise ConfigError(
            "filename_template 使用了未知变量 {%s}；可用变量：%s"
            % (exc.args[0], ", ".join(sorted(ctx)))
        )
    except (ValueError, IndexError) as exc:
        raise ConfigError("filename_template 格式错误：%s" % exc)
    name = (name or "").strip()
    if not os.path.splitext(name)[1]:
        name += ".cpp"
    if mode == "all" and "{submit_id}" not in template:
        stem, ext = os.path.splitext(name)
        name = "%s_sid%s%s" % (stem, sid, ext or ".cpp")
    return sanitize_filename(name)


def sha256_of(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rel_display(path, root):
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:
        return path.replace("\\", "/")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def default_config():
    return {
        "oj": {
            "base_url": DEFAULT_BASE,
            "request_interval": 1.5,
            "timeout": 30,
            "retries": 3,
            "user_agent": DEFAULT_UA,
        },
        "archive": {
            "directory": "output",
            "layout": "problem",
            "filename_template": "{id} P{part}.cpp",
            "mode": "latest",
            "overwrite": False,
            "strip_footer": True,
            "write_license": False,
        },
        "files": {
            "write_manifest_csv": True,
            "write_repo_files": True,
        },
        "git": {
            "noreply_domain": "users.noreply.github.com",
        },
    }


def deep_merge(base, extra):
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_toml_file(path):
    if not os.path.isfile(path):
        return {}
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            raise ConfigError(
                "读取 %s 需要 Python 3.11+ 的 tomllib，或安装 tomli：\n"
                "  pip install tomli\n"
                "也可以直接删掉该配置文件，改用命令行参数和环境变量。" % path
            )
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        raise ConfigError("配置文件 %s 解析失败：%s" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("配置文件 %s 顶层必须是表（table）。" % path)
    return data


def _find_private_key(node, prefix=""):
    """在公共配置里递归查找 cookie/password 字段，返回路径或空串。"""
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key).strip().lower()
            here = "%s.%s" % (prefix, key) if prefix else str(key)
            if name in PRIVATE_AUTH_KEYS:
                return here
            found = _find_private_key(value, here)
            if found:
                return found
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found = _find_private_key(value, "%s[%d]" % (prefix, index))
            if found:
                return found
    return ""


def _strip_private_fields(node):
    """从运行时配置里移除密码/Cookie，避免它们被误写进任何产物。"""
    if isinstance(node, dict):
        for key in list(node):
            if str(key).strip().lower() in PRIVATE_AUTH_KEYS:
                node.pop(key, None)
            else:
                _strip_private_fields(node[key])
    elif isinstance(node, list):
        for item in node:
            _strip_private_fields(item)


def resolve_config(args):
    cfg = default_config()
    public = load_toml_file(args.config)
    local = load_toml_file(args.local_config)
    deep_merge(cfg, public)
    deep_merge(cfg, local)

    # 安全提醒：Cookie/密码只允许放 local 或环境变量
    private_key = _find_private_key(public)
    if private_key:
        raise ConfigError(
            "config.toml 里出现了 %s。密码/Cookie 属于私有凭据，请放到 "
            "config.local.toml（已被 .gitignore 忽略）或环境变量"
            "（BMEOJ_PASSWORD / BMEOJ_COOKIE）。" % private_key
        )

    # 环境变量覆盖
    env_base = os.environ.get("BMEOJ_BASE_URL")
    if env_base:
        cfg["oj"]["base_url"] = env_base
    if args.base_url:
        cfg["oj"]["base_url"] = args.base_url
    if args.interval is not None:
        cfg["oj"]["request_interval"] = args.interval
    if args.all:
        cfg["archive"]["mode"] = "all"
    if args.force:
        cfg["archive"]["overwrite"] = True
    if args.overwrite:
        cfg["archive"]["overwrite"] = True
    if args.license:
        cfg["archive"]["write_license"] = True
    if args.keep_footer:
        cfg["archive"]["strip_footer"] = False

    oj = cfg["oj"]
    arc = cfg["archive"]
    oj["base_url"] = str(oj.get("base_url") or DEFAULT_BASE).rstrip("/")
    # 真实站点强制 >=1 秒；localhost 仅用于测试/自建镜像，允许调快。
    try:
        host = urllib.parse.urlparse(oj["base_url"]).hostname or ""
        minimum_interval = 0.0 if host in ("127.0.0.1", "localhost", "::1") else 1.0
        oj["request_interval"] = max(minimum_interval, float(oj.get("request_interval", 1.5)))
    except (TypeError, ValueError):
        raise ConfigError("request_interval 必须是数字。")
    try:
        oj["timeout"] = max(5.0, float(oj.get("timeout", 30)))
    except (TypeError, ValueError):
        raise ConfigError("timeout 必须是数字。")
    try:
        oj["retries"] = max(0, min(10, int(oj.get("retries", 3))))
    except (TypeError, ValueError):
        raise ConfigError("retries 必须是整数。")
    if arc.get("layout") not in ("problem", "submissions", "flat"):
        raise ConfigError("layout 只能是 problem / submissions / flat。")
    if arc.get("mode") not in ("latest", "all"):
        raise ConfigError("mode 只能是 latest / all。")
    if not str(arc.get("filename_template") or "").strip():
        raise ConfigError("filename_template 不能为空。")
    # 运行时配置不再携带密码/Cookie；鉴权代码只读取 local 原始表和进程环境变量。
    _strip_private_fields(cfg)
    return cfg, public, local


def resolve_cookie(local_cfg, log):
    global _ACTIVE_COOKIE
    raw = os.environ.get("BMEOJ_COOKIE")
    source = "环境变量 BMEOJ_COOKIE"
    if not raw:
        oj = local_cfg.get("oj") if isinstance(local_cfg.get("oj"), dict) else {}
        raw = oj.get("cookie")
        source = "config.local.toml"
    if not raw:
        return "", ""
    raw = str(raw).strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if "\n" in raw or "\r" in raw:
        raise ConfigError("Cookie 不能包含换行；请粘贴为一行。")
    if not raw:
        return "", ""
    _ACTIVE_COOKIE = raw
    return raw, source


def _can_prompt():
    """非交互环境不弹密码输入，避免 CI/计划任务挂起。"""
    if os.environ.get("BMEOJ_NO_PROMPT"):
        return False
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def resolve_password(local_cfg, log, required=True):
    """按环境变量 -> config.local.toml -> getpass 的顺序取密码。"""
    raw = os.environ.get("BMEOJ_PASSWORD")
    source = "环境变量 BMEOJ_PASSWORD"
    if raw is None or raw == "":
        oj = local_cfg.get("oj") if isinstance(local_cfg.get("oj"), dict) else {}
        raw = oj.get("password")
        source = "config.local.toml"
        if raw is None:
            raw = oj.get("passwd")
            source = "config.local.toml"
    if raw is not None:
        raw = str(raw)
        if raw != "":
            remember_secret(raw)
            return raw, source
    if _can_prompt():
        try:
            raw = getpass.getpass("OJ 密码（输入不回显）: ")
        except EOFError:
            raise ConfigError("密码输入结束（EOF），未获得密码。")
        if raw == "":
            raise ConfigError("密码不能为空。")
        remember_secret(raw)
        return raw, "交互输入"
    if required:
        raise AuthError(
            "未提供 OJ 密码，无法登录。请任选一种方式配置：\n"
            "  * 环境变量 BMEOJ_PASSWORD\n"
            "  * config.local.toml 的 [oj] password（已被 .gitignore 忽略）\n"
            "  * 在交互式终端运行，让脚本用 getpass 提示输入（不回显）\n"
            "也可以改用 Cookie 方式（BMEOJ_COOKIE / [oj] cookie）作为备选鉴权。"
        )
    return "", ""


def password_digest(password):
    """站点登录页用 hex_md5() 在浏览器端处理密码；这里做同样的 UTF-8 MD5。"""
    return hashlib.md5(password.encode("utf-8")).hexdigest()


# 登录表单解析：HUSTOJ 的 loginpage.php 是标准 POST 表单，字段名可能因部署而异。
_FORM_RE = re.compile(r"(?is)<form\b([^>]*)>(.*?)</form>")
_INPUT_RE = re.compile(r"(?is)<input\b([^>]*)>")
_BUTTON_RE = re.compile(r"(?is)<button\b([^>]*)>")
_SUBMIT_INPUT_RE = re.compile(r"(?is)<input\b[^>]*type\s*=\s*[\"']?submit[^>]*>")
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>/]+))""")


def _parse_attrs(text):
    attrs = {}
    for match in _ATTR_RE.finditer(text or ""):
        name = match.group(1).lower()
        value = next((item for item in match.groups()[1:] if item is not None), "")
        attrs[name] = html_lib.unescape(value or "")
    return attrs


def parse_login_form(html):
    """提取 action、用户名/密码字段、隐藏字段，并判断是否前端 MD5。"""
    form_attrs = ""
    form_body = html or ""
    for attrs, body in _FORM_RE.findall(html or ""):
        if re.search(r"type\s*=\s*[\"']?password", body, re.I) or "login" in attrs.lower():
            form_attrs, form_body = attrs, body
            break

    attrs = _parse_attrs(form_attrs)
    action = (attrs.get("action") or "login.php").strip() or "login.php"
    md5 = bool(re.search(r"md5", form_attrs, re.I)) or bool(
        re.search(r"jsMd5|hex_md5", form_body, re.I)
    )
    if not form_attrs:
        md5 = bool(re.search(r"jsMd5|hex_md5", html or "", re.I))

    username_field = "user_id"
    password_field = "password"
    hidden = {}
    captcha = False
    for raw in _INPUT_RE.findall(form_body):
        field = _parse_attrs(raw)
        name = field.get("name") or ""
        kind = (field.get("type") or "text").lower()
        if not name:
            continue
        if kind == "password":
            password_field = name
        elif kind == "hidden":
            hidden[name] = field.get("value", "")
        elif name.lower() in ("user_id", "userid", "username", "user", "name"):
            username_field = name
        if kind != "hidden" and re.search(r"captcha|vcode|verify", name, re.I):
            captcha = True

    for raw in _BUTTON_RE.findall(form_body) + _SUBMIT_INPUT_RE.findall(form_body):
        field = _parse_attrs(raw)
        name = field.get("name")
        if name and name not in hidden:
            hidden[name] = field.get("value") or "Login"

    return {
        "action": action,
        "username_field": username_field,
        "password_field": password_field,
        "hidden": hidden,
        "md5": md5,
        "captcha": captcha,
    }


_LOGIN_FAIL_RE = re.compile(
    r"user\s*name\s*or\s*password\s*wrong|username\s*or\s*password\s*wrong|"
    r"password\s*(?:is\s*)?wrong|invalid\s+(?:user|password)|incorrect\s+password|"
    r"用户名或密码错误|用户名/密码错误|密码错误|登录失败",
    re.I,
)
_NO_SUCH_USER_RE = re.compile(r"no\s+such\s+user|user\s+not\s+found|用户名不存在|用户不存在", re.I)


def login(fetcher, user, password, source, log, dump_html=False):
    """用 OJ 用户名/密码建立会话，并在成功后验证登录态。"""
    remember_secret(password)
    log.step("登录：%s（密码来源：%s，值已脱敏）" % (user, source))
    page = fetcher.get("loginpage.php", None, "登录页")
    if dump_html:
        fetcher.dump("loginpage.html", page)

    form = parse_login_form(page)
    if form["captcha"]:
        raise AuthError(
            "登录页要求验证码；本工具不绕过验证码，无法自动登录。"
            "请先在浏览器手动登录，然后改用 Cookie 方式（BMEOJ_COOKIE / [oj] cookie）。"
        )

    payload = dict(form["hidden"])
    payload[form["username_field"]] = user
    if form["md5"]:
        digest = password_digest(password)
        remember_secret(digest)
        payload[form["password_field"]] = digest
    else:
        payload[form["password_field"]] = password

    # 绝不把 payload 写进日志：里面含密码或密码摘要。
    response = fetcher.post(
        form["action"], payload, "登录提交", referer=fetcher.url("loginpage.php")
    )
    if dump_html:
        fetcher.dump("login_response.html", response)

    state = check_logged_in(fetcher, dump_html=dump_html)
    if state is True:
        log.info("登录成功：PHPSESSID 会话已建立，并通过 modifypage.php 验证（值已脱敏）。")
        return True

    blob = (response or "") + "\n" + plain_text(response)
    if _NO_SUCH_USER_RE.search(blob):
        raise UserNotFound("OJ 用户名不存在：%s（登录被拒绝）。" % user)

    exists = None
    try:
        exists, _ = check_user_exists(fetcher, user, log)
    except ArchiveError:
        exists = None
    if exists is False:
        raise UserNotFound("OJ 用户名不存在：%s（userinfo.php 返回 No such User）。" % user)

    if state is False or _LOGIN_FAIL_RE.search(blob):
        raise AuthError(
            "OJ 登录失败：用户名或密码错误（密码来源：%s，值已脱敏）。\n"
            "请确认 OJ 用户名和密码；连续失败可能导致账号被临时锁定。"
            "本工具不会猜测密码、不会绕过验证码。" % source
        )
    raise AuthError(
        "登录请求已提交，但无法确认登录状态（modifypage.php 页面结构未知）。\n"
        "请用 --probe --dump-html 查看登录页/会话页结构；或改用 Cookie 方式登录。"
    )


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Fetcher(object):
    def __init__(self, base_url, cookie, cfg, log):
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie
        self.interval = float(cfg["oj"]["request_interval"])
        self.timeout = float(cfg["oj"]["timeout"])
        self.retries = int(cfg["oj"]["retries"])
        self.user_agent = str(cfg["oj"]["user_agent"] or DEFAULT_UA)
        self.log = log
        self.calls = 0
        # 密码登录用 CookieJar 维持 PHPSESSID；静态 Cookie 模式保持原 header 行为。
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def _open(self, req):
        if self.cookie:
            return urllib.request.urlopen(req, timeout=self.timeout)
        return self.opener.open(req, timeout=self.timeout)

    def _url(self, path, params):
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            path = path.strip()
            while path.startswith("./"):
                path = path[2:]
            url = self.base_url + "/" + path.lstrip("/")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return url

    def url(self, path, params=None):
        return self._url(path, params)

    def session_cookie_names(self):
        return sorted({cookie.name for cookie in self.jar})

    def get(self, path, params=None, purpose=""):
        url = self._url(path, params)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Accept-Encoding": "identity",
            "Referer": self.base_url + "/",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        last_error = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with self._open(req) as resp:
                    raw = resp.read()
                    charset = resp.headers.get_content_charset() or "utf-8"
                    text = raw.decode(charset, "replace")
                self.calls += 1
                if purpose:
                    self.log.info("    已获取：%s" % purpose)
                self._sleep()
                return text
            except urllib.error.HTTPError as exc:
                self.calls += 1
                if exc.code in (401, 403):
                    raise AuthError(
                        "HTTP %s：没有权限访问 %s。登录状态可能无效/已过期，"
                        "或该提交不属于当前账号。" % (exc.code, purpose or path)
                    )
                last_error = "HTTP %s" % exc.code
            except urllib.error.URLError as exc:
                self.calls += 1
                last_error = "网络错误：%s" % getattr(exc, "reason", exc)
            except Exception as exc:  # socket timeout 等
                self.calls += 1
                last_error = "%s: %s" % (type(exc).__name__, exc)

            if attempt < self.retries:
                wait = self.interval * (attempt + 1)
                self.log.warn(
                    "%s 失败（%s），%.1f 秒后重试（%d/%d）"
                    % (purpose or path, last_error, wait, attempt + 1, self.retries)
                )
                time.sleep(wait)
        raise NetworkError("%s 失败：%s" % (purpose or path, last_error))

    def post(self, path, data, purpose="", referer=None):
        """提交表单；不重试，避免把同一份凭据重复发给站点。"""
        url = self._url(path, None)
        body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Accept-Encoding": "identity",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": referer or (self.base_url + "/"),
            "Origin": self.base_url,
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self._open(req) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                text = raw.decode(charset, "replace")
            self.calls += 1
            if purpose:
                self.log.info("    已提交：%s" % purpose)
            self._sleep()
            return text
        except urllib.error.HTTPError as exc:
            self.calls += 1
            if exc.code in (401, 403):
                raise AuthError("HTTP %s：登录请求被拒绝（%s）。" % (exc.code, purpose or path))
            raise NetworkError("登录请求失败：HTTP %s" % exc.code)
        except urllib.error.URLError as exc:
            self.calls += 1
            raise NetworkError("登录请求网络错误：%s" % getattr(exc, "reason", exc))
        except Exception as exc:
            self.calls += 1
            raise NetworkError("登录请求失败：%s: %s" % (type(exc).__name__, exc))

    def _sleep(self):
        # 间隔已在 resolve_config 里按站点钳制（真实站点 >=1 秒）。
        if self.interval > 0:
            time.sleep(self.interval)

    def dump(self, name, text):
        path = os.path.join(HERE, "_debug", name)
        try:
            os.makedirs(os.path.join(HERE, "_debug"), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(redact(text))
        except OSError as exc:
            self.log.warn(
                "调试页面保存失败（%s）：%s；不影响抓取，可检查目录权限。"
                % (rel_display(path, HERE), exc)
            )
            return None
        self.log.info("    调试页面已保存：%s" % rel_display(path, HERE))
        return path


# --------------------------------------------------------------------------- #
# 页面解析
# --------------------------------------------------------------------------- #
_ROW_RE = re.compile(r"(?is)<tr\b[^>]*class=[\"']?(?:even|odd)row[\"']?[^>]*>(.*?)</tr>")
_TD_RE = re.compile(r"(?is)<td\b[^>]*>(.*?)</td>")
_TH_RE = re.compile(r"(?is)<th\b[^>]*>(.*?)</th>")
_PROBLEM_LINK_RE = re.compile(r"problem\.php\?cid=(\d+)&(?:amp;)?pid=(\d+)", re.I)
_RESULT_CODE_RE = re.compile(r"result\s*=\s*[\"']?(\d+)", re.I)
_DATETIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")


def _header_index(html):
    block = re.search(r"(?is)<tr\b[^>]*class=[\"']?toprow[\"']?[^>]*>(.*?)</tr>", html)
    if not block:
        return {}
    names = [norm_space(strip_tags(x)).replace(" ", "").upper() for x in _TH_RE.findall(block.group(1))]
    return {name: i for i, name in enumerate(names)}


def parse_status_page(html):
    """解析 status.php；返回提交行列表。对残缺 HTML（缺少 </a>）也尽量容忍。"""
    idx = _header_index(html)

    def col(tds, *names, **kw):
        default = kw.get("default")
        for name in names:
            key = name.replace(" ", "").upper()
            if key in idx and idx[key] < len(tds):
                return norm_space(strip_tags(tds[idx[key]]))
        if default is not None and default < len(tds):
            return norm_space(strip_tags(tds[default]))
        return ""

    rows = []
    for match in _ROW_RE.finditer(html):
        body = match.group(1)
        tds = _TD_RE.findall(body)
        if len(tds) < 4:
            continue
        problem = _PROBLEM_LINK_RE.search(body)
        result_code = ""
        code_match = _RESULT_CODE_RE.search(body)
        if code_match:
            result_code = code_match.group(1)
        sid = col(tds, "RunID", "RunId", "ID", default=0)
        if not sid.isdigit():
            continue
        result_text = col(tds, "Result", default=3)
        submitted_at = col(tds, "SubmitTime", "Submit Time", default=8)
        if not submitted_at:
            dt = _DATETIME_RE.search(body)
            submitted_at = dt.group(1) if dt else ""
        language = col(tds, "Language", default=6)
        if language in ("----", "---", "-", "—"):
            language = ""
        accepted = result_code == ACCEPTED_CODE or any(
            word in result_text.lower() for word in ACCEPTED_WORDS
        )
        rows.append(
            {
                "sid": sid,
                "cid": problem.group(1) if problem else "",
                "pid": int(problem.group(2)) if problem else -1,
                "problem_number": col(tds, "Problem", default=2),
                "result_code": result_code,
                "result_text": result_text,
                "language": language,
                "submitted_at": submitted_at,
                "accepted": accepted,
            }
        )
    return rows


def fetch_all_submissions(fetcher, user, log, max_pages=200):
    seen = {}
    cursor = None
    for page in range(1, max_pages + 1):
        params = {"user_id": user}
        if cursor:
            params["top"] = cursor
        html = fetcher.get("status.php", params, "提交列表第 %d 页" % page)
        rows = parse_status_page(html)
        if not rows:
            break
        new_count = 0
        for row in rows:
            if row["sid"] not in seen:
                seen[row["sid"]] = row
                new_count += 1
        sids = [int(row["sid"]) for row in rows if row["sid"].isdigit()]
        if not sids:
            break
        smallest = min(sids)
        if new_count == 0:
            break
        if cursor is not None and smallest >= cursor:
            break
        cursor = smallest
    return list(seen.values())


def check_user_exists(fetcher, user, log):
    html = fetcher.get("userinfo.php", {"user": user}, "用户信息")
    text = plain_text(html)
    if re.search(r"no\s+such\s+user", text, re.I):
        return False, ""
    nickname = ""
    match = re.search(re.escape(user) + r"\s*-{1,2}\s*([^\s<]+)", html)
    if match:
        nickname = html_lib.unescape(match.group(1)).strip()
    return True, nickname


_SESSION_POS_RE = re.compile(
    r"logout\.php|name\s*=\s*[\"']?(?:oldpass|newpass)\b|change\s+password|"
    r"old\s+password|new\s+password|modify\s+user|修改密码|修改资料|旧密码|新密码",
    re.I,
)
_SESSION_NEG_RE = re.compile(
    r"please\s+(?:login|log\s+in)|loginpage\.php|请先登录|请登录|not\s+logged\s+in",
    re.I,
)
_PROFILE_SCRIPT_RE = re.compile(
    r"(?is)<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I
)


def classify_session(html, fetcher, dump_html=False):
    """判断受保护页面是登录态还是匿名态；无法判断返回 None。"""
    if _SESSION_POS_RE.search(html or ""):
        return True
    if _SESSION_NEG_RE.search(html or ""):
        return False

    # 兼容不同主题：登录入口由 template/bs3/profile.php 动态输出。
    for src in _PROFILE_SCRIPT_RE.findall(html or ""):
        if "profile.php" not in src.lower():
            continue
        try:
            script = fetcher.get(src, None, "会话状态脚本")
        except ArchiveError:
            continue
        if re.search(r"logout\.php|\bLogout\b|退出登录|注销", script, re.I):
            return True
        if re.search(r"loginpage\.php|\bLogin\b", script, re.I):
            return False
    return None


def check_logged_in(fetcher, dump_html=False):
    try:
        html = fetcher.get("modifypage.php", None, "登录状态")
    except (NetworkError, AuthError):
        return None
    if dump_html:
        fetcher.dump("modifypage_session.html", html)
    return classify_session(html, fetcher, dump_html=dump_html)


def fetch_problem_title(fetcher, cid, pid, log):
    html = fetcher.get("problem.php", {"cid": cid, "pid": pid}, "题目 %s P%s" % (cid, part_letter(pid)))
    first_h2 = re.search(r"(?is)<h2[^>]*>(.*?)</h2>", html)
    title = norm_space(strip_tags(first_h2.group(1))) if first_h2 else ""
    title = re.sub(r"^(?:问题|Problem)\s*[A-Za-z0-9]+\s*[:：]\s*", "", title, flags=re.I)
    if not title:
        page_title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = norm_space(strip_tags(page_title.group(1))) if page_title else ""
        title = re.sub(r"\s*[-|_]\s*生医在线.*$", "", title)
    if not title or title in ("Description", "Input", "Output"):
        title = "题 %s P%s" % (cid, part_letter(pid))
    return title


def fetch_source(fetcher, sid, log, dump_html=False):
    html = fetcher.get("showsource.php", {"id": sid}, "提交 %s 源码" % sid)
    text = plain_text(html)
    # 只有页面里没有代码块时才把“无法查看”当作权限错误；
    # 避免源码注释/字符串里恰好包含 not allowed 等词时误判。
    has_code_block = re.search(r"(?is)<(pre|textarea)\b", html) is not None
    if not has_code_block and re.search(r"could\s+not\s+view\s+this\s+code|no\s+such\s+code|not\s+allowed", text, re.I):
        if dump_html:
            fetcher.dump("showsource_%s_no_access.html" % sid, html)
        raise AuthError(
            "无法查看提交 %s 的源码：登录状态无效/已过期，或该提交不属于当前账号。" % sid
        )

    candidates = []
    for match in re.finditer(r"(?is)<pre\b[^>]*>(.*?)</pre>", html):
        candidates.append(match.group(1))
    for match in re.finditer(r"(?is)<textarea\b[^>]*>(.*?)</textarea>", html):
        candidates.append(match.group(1))
    if not candidates and dump_html:
        fetcher.dump("showsource_%s_no_pre.html" % sid, html)
    if not candidates:
        raise ArchiveError("提交 %s 的页面里没有找到可识别的代码块（<pre>/<textarea>）。" % sid)

    best = ""
    for candidate in candidates:
        candidate = re.sub(r"(?is)<br\s*/?>", "\n", candidate)
        candidate = re.sub(r"<[^>]+>", "", candidate)
        candidate = html_lib.unescape(candidate)
        candidate = candidate.replace("\r\n", "\n").replace("\r", "\n")
        if len(candidate) > len(best):
            best = candidate
    if not best.strip():
        raise ArchiveError("提交 %s 的源码块是空的。" % sid)
    return best


def clean_code(code, strip_footer=True):
    """只保留代码；按旧规则删除 OJ 追加的 /***** 分隔块。"""
    code = code.replace("\r\n", "\n").replace("\r", "\n")
    if strip_footer:
        lines = code.split("\n")
        for index, line in enumerate(lines):
            if _FOOTER_RE.match(line.strip()):
                code = "\n".join(lines[:index])
                break
    return code.rstrip() + "\n"


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def manifest_path(target):
    return os.path.join(target, "manifest.json")


def load_manifest(target, log):
    path = manifest_path(target)
    if not os.path.isfile(path):
        return {"schema_version": SCHEMA_VERSION, "entries": []}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        backup = path + ".corrupt-%s" % time.strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, backup)
        except OSError:
            backup = None
        log.warn("manifest.json 解析失败（%s），已备份到 %s，本次从空清单开始。" % (exc, backup))
        return {"schema_version": SCHEMA_VERSION, "entries": []}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        log.warn("manifest.json 结构不认识，本次从空清单开始。")
        return {"schema_version": SCHEMA_VERSION, "entries": []}
    data.setdefault("schema_version", SCHEMA_VERSION)
    return data


def sort_entries(entries):
    def key(entry):
        try:
            cid = int(entry.get("cid") or 0)
        except (TypeError, ValueError):
            cid = 0
        try:
            pid = int(entry.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        try:
            sid = int(entry.get("submit_id") or 0)
        except (TypeError, ValueError):
            sid = 0
        return (cid, pid, sid)

    return sorted(entries, key=key)


def write_manifest(target, manifest, cfg, user, log):
    entries = sort_entries(manifest.get("entries") or [])
    manifest["entries"] = entries
    problems = len(set((entry.get("cid"), entry.get("pid")) for entry in entries))
    data = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "bmeoj_archive.py", "version": VERSION},
        "generated_at": now_iso(),
        "source": {
            "base_url": cfg["oj"]["base_url"],
            "user": user,
            "mode": cfg["archive"]["mode"],
            "layout": cfg["archive"]["layout"],
            "filename_template": cfg["archive"]["filename_template"],
        },
        "stats": {
            "problems": problems,
            "submissions": len(entries),
            "bytes": sum(int(entry.get("bytes") or 0) for entry in entries),
        },
        "entries": entries,
    }
    os.makedirs(target, exist_ok=True)
    atomic_write(manifest_path(target), json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    if cfg["files"].get("write_manifest_csv", True):
        csv_path = os.path.join(target, "manifest.csv")
        fields = [
            "problem", "cid", "pid", "part", "problem_id", "title",
            "submit_id", "submitted_at", "language", "status", "filename",
            "problem_url", "source_url", "sha256", "bytes",
        ]
        # utf-8-sig 让 Excel 正确识别中文
        tmp = csv_path + ".tmp-%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for entry in entries:
                writer.writerow(entry)
        os.replace(tmp, csv_path)
        log.info("    manifest.csv 已更新（%d 条）" % len(entries))
    return data


# --------------------------------------------------------------------------- #
# 文件生成
# --------------------------------------------------------------------------- #
_GITIGNORE = """\
# 私有配置与凭据
config.local.toml
.cookie
*.cookie
*.log
logs/
_cache/
_backup/

# Python
__pycache__/
*.py[cod]
.venv/
.deps/

# 本目录可能同时存放的文档/临时文件
*.docx
*.tmp
*.bak

# 系统文件
.DS_Store
Thumbs.db
desktop.ini
"""

_TARGET_CONFIG = """\
# 本文件由 bmeoj_archive.py 生成，用于记录本次存档的规则。
# 它不包含密码/Cookie，也不能直接用来自动同步（脚本在工具目录，不在本仓库）。

[archive]
layout = "{layout}"
filename_template = "{template}"
mode = "{mode}"

[notes]
# 提交源码的版权归原作者/题目方；本仓库仅作个人 AC 记录。
# 请遵守 OJ 服务条款与学术诚信要求。
"""

_LICENSE = """\
MIT License (tooling and documentation only)

Copyright (c) {year} {user}

This license applies only to the scripts and documentation in this repository.
It does NOT apply to any submission source code, which remains the property of
its original author and/or the problem provider.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

README_MARKER = "<!-- bmeoj-archive: generated -->"


def build_readme(manifest, cfg, user, base_url):
    entries = sort_entries(manifest.get("entries") or [])
    problems = len(set((entry.get("cid"), entry.get("pid")) for entry in entries))
    mode = cfg["archive"]["mode"]
    lines = [
        README_MARKER,
        "# BME OJ 个人 AC 提交存档",
        "",
        "> 用户：`%s`　题目：%d　提交：%d　模式：%s" % (user, problems, len(entries), mode),
        "> 生成时间：%s（由 bmeoj_archive.py v%s 自动生成）" % (now_iso(), VERSION),
        "",
        "本仓库只保存 **本人账号或已获授权账号** 在 [oj.bmeonline.cn](%s) 上的正确（Accepted）提交源码，"
        "用于个人复习与备份。" % base_url,
        "",
        "## 目录与命名",
        "",
        "- 默认按题号分目录：`<题号>/<题号> P<小题字母>.cpp`，例如 `1011/1011 PA.cpp`。",
        "- 文件清单与元数据见 [`manifest.json`](manifest.json) 和 [`manifest.csv`](manifest.csv)。",
        "- 重复 AC 默认只保留最新一次；`--all` 保留全部，文件名会自动加 `_sid<提交ID>` 区分。",
        "",
        "## 题目清单",
        "",
    ]
    if entries:
        lines.append("| 题号 | 题名 | 提交 ID | 提交时间 | 语言 | 文件 |")
        lines.append("| --- | --- | ---: | --- | --- | --- |")
        for entry in entries:
            title = str(entry.get("title") or "").replace("|", "\\|")
            problem_url = entry.get("problem_url") or base_url
            filename = entry.get("filename") or ""
            file_url = urllib.parse.quote(filename, safe="/")
            lines.append(
                "| [%s](%s) | %s | %s | %s | %s | [%s](%s) |"
                % (
                    entry.get("problem", ""),
                    problem_url,
                    title,
                    entry.get("submit_id", ""),
                    entry.get("submitted_at", ""),
                    entry.get("language") or "unknown",
                    os.path.basename(filename),
                    file_url,
                )
            )
    else:
        lines.append("_暂无记录。_")

    lines += [
        "",
        "## 版权与免责声明",
        "",
        "- 提交源码的版权归 **原作者 / 题目提供方** 所有；本仓库不主张对这些代码的版权。",
    ]
    if cfg["archive"].get("write_license"):
        lines.append("- 本仓库的整理文档与清单采用 MIT 许可（见 `LICENSE`）；该许可 **不覆盖** 任何提交源码。")
    else:
        lines.append("- 本仓库仅保存提交源码与清单，未附加开源许可；提交源码版权归原作者/题目方。")
    lines += [
        "- 本仓库仅用于个人学习记录。请遵守 oj.bmeonline.cn 的服务条款、课程要求和学术诚信规范。",
        "- 请勿把他人的提交用于抄袭、代写、刷分或其他违反学术诚信的用途。",
        "- 如果你是权利人并希望删除某段代码，请提交 Issue 或联系仓库所有者。",
        "",
        "## 重新生成",
        "",
        "本仓库由 `bmeoj_archive.py` 生成。重新运行工具时默认增量更新：",
        "已存在且提交未变化的题目会跳过；`--force` 强制重下，`--overwrite` 允许覆盖。",
        "",
        "```text",
        "python bmeoj_archive.py --user %s --dir <本目录>" % user,
        "```",
        "",
    ]
    return "\n".join(lines)


def build_topics(user):
    safe_user = re.sub(r"[^a-z0-9-]+", "-", user.lower()).strip("-") or "user"
    topics = [
        "online-judge",
        "bme-oj",
        "bmeonline",
        "cpp",
        "competitive-programming",
        "acm",
        "algorithm",
        "bmeoj-archive",
        "oj-%s" % safe_user,
    ]
    seen = set()
    result = []
    for topic in topics:
        topic = re.sub(r"[^a-z0-9-]+", "-", topic.lower()).strip("-")
        topic = re.sub(r"-{2,}", "-", topic)
        if topic and topic not in seen:
            seen.add(topic)
            result.append(topic)
    return "\n".join(result) + "\n"


def write_repo_files(target, manifest, cfg, user, log):
    if not cfg["files"].get("write_repo_files", True):
        return
    base_url = cfg["oj"]["base_url"]
    generated = [
        ("README.md", build_readme(manifest, cfg, user, base_url)),
        (".gitignore", _GITIGNORE),
        ("topics.txt", build_topics(user)),
        ("repo-description.txt", "BME OJ 个人 AC 提交源码存档（用户 %s，%d 题）。\n" % (
            user, len(set((e.get("cid"), e.get("pid")) for e in manifest.get("entries") or [])))),
        ("config.example.toml", _TARGET_CONFIG.format(
            layout=cfg["archive"]["layout"],
            template=cfg["archive"]["filename_template"],
            mode=cfg["archive"]["mode"],
        )),
    ]
    if cfg["archive"].get("write_license"):
        generated.append(("LICENSE", _LICENSE.format(year=datetime.now().year, user=user)))

    for name, content in generated:
        path = os.path.join(target, name)
        if os.path.isfile(path) and name == "README.md":
            try:
                with open(path, encoding="utf-8") as fh:
                    old = fh.read()
                if README_MARKER not in old:
                    backup_dir = os.path.join(target, "_backup")
                    os.makedirs(backup_dir, exist_ok=True)
                    shutil.copy2(path, os.path.join(
                        backup_dir, "README.md.%s.bak" % time.strftime("%Y%m%d-%H%M%S")))
                    log.warn("目标目录已有手写 README.md，已备份到 _backup/ 后重新生成。")
            except OSError:
                pass
        status = atomic_write(path, content)
        if status != "unchanged":
            log.info("    已生成：%s" % name)


# --------------------------------------------------------------------------- #
# git（只本地初始化，绝不 push）
# --------------------------------------------------------------------------- #
def _run_git(args, cwd, timeout=120):
    env = dict(os.environ)
    # 子进程不需要、也不应该看到本次登录的凭据。
    env.pop("BMEOJ_PASSWORD", None)
    env.pop("BMEOJ_COOKIE", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return 127, "git 未安装或不在 PATH 中"
    except subprocess.TimeoutExpired:
        return 124, "git 命令超时"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def init_git(target, user, cfg, log):
    git = shutil.which("git")
    if not git:
        log.warn("未找到 git，跳过 --init-git。安装 Git 后可手动初始化。")
        return
    if not os.path.isdir(os.path.join(target, ".git")):
        code, out = _run_git(["init"], target)
        if code != 0:
            log.error("git init 失败：%s" % out.strip())
            return
        log.info("已初始化本地 git 仓库。")

    code, out = _run_git(["add", "-A"], target)
    if code != 0:
        log.error("git add 失败：%s" % out.strip())
        return

    code, staged = _run_git(["diff", "--cached", "--name-only"], target)
    if code == 0 and not staged.strip():
        log.info("没有新的改动需要 commit。")
    else:
        _, name = _run_git(["config", "user.name"], target)
        _, email = _run_git(["config", "user.email"], target)
        name = name.strip()
        email = email.strip()
        if not name:
            name = user
        if not email:
            domain = cfg["git"].get("noreply_domain") or "users.noreply.github.com"
            email = "%s@%s" % (re.sub(r"[^A-Za-z0-9._-]+", "-", user), domain)
        message = "Add BME OJ accepted submissions for %s (%s)" % (
            user, datetime.now().strftime("%Y-%m-%d"))
        code, out = _run_git(
            [
                "-c", "user.name=%s" % name,
                "-c", "user.email=%s" % email,
                "commit", "-m", message,
            ],
            target,
        )
        if code != 0:
            log.error("git commit 失败：%s" % out.strip())
            log.info("文件已 git add，可手动配置 user.name/user.email 后提交。")
            return
        log.info("已提交本地 commit：%s" % message)

    repo_name = "bmeoj-" + (re.sub(r"[^A-Za-z0-9._-]+", "-", user).strip("-") or "archive")
    log.info("")
    log.info("本地 git 初始化完成。以下命令需要你手动执行（脚本绝不会 push）：")
    log.info('  cd "%s"' % target)
    log.info("  git remote add origin https://github.com/<你的GitHub用户名>/%s.git" % repo_name)
    log.info("  git branch -M main")
    log.info("  git push -u origin main")


# --------------------------------------------------------------------------- #
# 归档主流程
# --------------------------------------------------------------------------- #
def _fs_path(target, relpath):
    return os.path.join(target, relpath.replace("/", os.sep))


def _entry_key(entry):
    return (str(entry.get("cid") or ""), str(entry.get("pid") or ""))


def _find_entry(entries, cid, pid, sid, mode):
    sid = str(sid)
    for entry in entries:
        if str(entry.get("submit_id")) == sid:
            return entry
    if mode == "latest":
        for entry in entries:
            if _entry_key(entry) == (str(cid), str(pid)):
                return entry
    return None


def _plan_items(rows, cfg, manifest, target, fetcher, log, args):
    mode = cfg["archive"]["mode"]
    layout = cfg["archive"]["layout"]
    template = cfg["archive"]["filename_template"]
    overwrite = bool(cfg["archive"].get("overwrite"))
    force = bool(args.force)
    strip_footer = bool(cfg["archive"].get("strip_footer"))
    no_titles = bool(args.no_titles)

    accepted = [row for row in rows if row.get("accepted") and row.get("cid") and row.get("pid") >= 0]
    groups = {}
    for row in accepted:
        groups.setdefault((row["cid"], row["pid"]), []).append(row)
    for versions in groups.values():
        versions.sort(key=lambda r: (r.get("submitted_at", ""), int(r["sid"])))

    keys = sorted(groups.keys(), key=lambda k: (int(k[0]), k[1]))
    if args.limit and args.limit > 0:
        keys = keys[: args.limit]

    entries = manifest.setdefault("entries", [])
    by_problem = {}
    for entry in entries:
        by_problem[_entry_key(entry)] = entry
    by_sid = {str(entry.get("submit_id")): entry for entry in entries}

    items = []
    title_cache = {}
    used_paths = set()

    for cid, pid in keys:
        versions = groups[(cid, pid)]
        chosen = versions if mode == "all" else [versions[-1]]
        problem_title = ""
        for entry in (by_problem.get((str(cid), str(pid))),):
            if entry and entry.get("title"):
                problem_title = entry["title"]
                break

        for row in chosen:
            sid = str(row["sid"])
            # --all 只按提交 ID 去重；同题的新 AC 应该新增，而不是被当成“覆盖”。
            existing = by_sid.get(sid)
            if existing is None and mode == "latest":
                existing = by_problem.get((str(cid), str(pid)))
            existing_path = existing.get("filename") if existing else ""
            existing_exists = bool(existing_path) and os.path.isfile(_fs_path(target, existing_path))

            action = ""
            reason = ""
            if existing and existing_exists:
                if str(existing.get("submit_id")) == sid and not force:
                    action = "skip"
                    reason = "已存在且提交未变化"
                elif not (overwrite or force):
                    action = "skip"
                    reason = "本地已有文件；新提交未覆盖（用 --overwrite）"
                else:
                    action = "update"
                    reason = "覆盖已有文件"
            elif existing_path and os.path.isfile(_fs_path(target, existing_path)):
                # manifest 指向的文件存在但上面没命中（理论上不会）
                action = "skip" if not (overwrite or force) else "update"
                reason = "本地已有同名文件"
            else:
                action = "create" if not existing_exists else "update"
                reason = "本地缺少文件，重新下载" if existing_exists else "新题目"

            # 需要标题时再抓，避免为已跳过的题目发请求
            need_title = action in ("create", "update") and not problem_title
            if need_title and not no_titles and not problem_title:
                try:
                    problem_title = fetch_problem_title(fetcher, cid, pid, log)
                    title_cache[(cid, pid)] = problem_title
                except ArchiveError as exc:
                    log.warn("题目 %s P%s 标题获取失败：%s（使用占位标题）" % (
                        cid, part_letter(pid), exc))
                    problem_title = "题 %s P%s" % (cid, part_letter(pid))

            title = problem_title or str(row.get("problem_number") or "") or "题 %s P%s" % (
                cid, part_letter(pid))
            ctx = {
                "id": cid,
                "pid": pid,
                "part": part_letter(pid),
                "letter": part_letter(pid),
                "problem_id": row.get("problem_number") or "",
                "title": title,
                "slug": slugify(title),
                "user": cfg["oj"].get("user") or "",
                "submit_id": sid,
                "date": (row.get("submitted_at") or "")[:10],
            }
            if existing and existing_path and not force:
                filename = os.path.basename(existing_path)
                relpath = existing_path
            else:
                filename = render_filename(template, ctx, mode, sid)
                if layout == "problem":
                    relpath = "%s/%s" % (cid, filename)
                elif layout == "submissions":
                    relpath = "submissions/%s" % filename
                else:
                    relpath = filename

            # 同一次运行内的重名处理
            key = os.path.normcase(relpath)
            if key in used_paths:
                stem, ext = os.path.splitext(relpath)
                relpath = "%s_sid%s%s" % (stem, sid, ext or ".cpp")
                key = os.path.normcase(relpath)
                log.warn("文件名冲突，已改为：%s" % relpath)
            used_paths.add(key)

            if action in ("create", "update") and os.path.isfile(_fs_path(target, relpath)):
                if not (overwrite or force) and not existing_exists:
                    action = "skip"
                    reason = "目标文件已存在（无清单记录）；用 --overwrite 覆盖"

            items.append({
                "row": row,
                "cid": cid,
                "pid": pid,
                "part": part_letter(pid),
                "title": title,
                "filename": relpath.replace("\\", "/"),
                "action": action,
                "reason": reason,
                "existing": existing,
                "strip_footer": strip_footer,
                "code": None,
            })
    return items


def _make_entry(item, cfg, user, code):
    row = item["row"]
    base = cfg["oj"]["base_url"]
    relpath = item["filename"]
    return {
        "problem": "%s P%s" % (item["cid"], item["part"]),
        "cid": str(item["cid"]),
        "pid": int(item["pid"]),
        "part": item["part"],
        "problem_id": str(row.get("problem_number") or ""),
        "title": item["title"],
        "submit_id": str(row["sid"]),
        "submitted_at": row.get("submitted_at") or "",
        "language": row.get("language") or "unknown",
        "status": "Accepted",
        "filename": relpath,
        "problem_url": "%s/problem.php?cid=%s&pid=%s" % (base, item["cid"], item["pid"]),
        "source_url": "%s/showsource.php?id=%s" % (base, row["sid"]),
        "sha256": sha256_of(code),
        "bytes": len(code.encode("utf-8")),
        "fetched_at": now_iso(),
    }


def _upsert_entry(manifest, entry, mode):
    entries = manifest.setdefault("entries", [])
    sid = str(entry.get("submit_id"))
    cid, pid = entry.get("cid"), entry.get("pid")
    kept = []
    for old in entries:
        if str(old.get("submit_id")) == sid:
            continue
        if mode == "latest" and str(old.get("cid")) == str(cid) and str(old.get("pid")) == str(pid):
            continue
        kept.append(old)
    kept.append(entry)
    manifest["entries"] = sort_entries(kept)


def _backup_existing(target, relpath, log):
    src = _fs_path(target, relpath)
    if not os.path.isfile(src):
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(target, "_backup", stamp, relpath.replace("/", os.sep))
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        log.info("    已备份旧文件：%s" % rel_display(dst, target))
    except OSError as exc:
        log.warn("备份旧文件失败：%s" % exc)


def describe_auth(auth):
    kind = (auth or {}).get("kind")
    if kind == "cookie":
        return "Cookie（来源：%s，值已脱敏）" % auth.get("source", "")
    if kind == "password":
        return "密码登录（来源：%s，值已脱敏）" % auth.get("source", "")
    return "未提供（匿名）"


def prepare_session(cfg, args, user, log, require_auth, dump_html=False):
    """建立会话：Cookie 优先；否则密码登录；probe 允许匿名。"""
    cookie, cookie_source = resolve_cookie(args.local_cfg, log)
    fetcher = Fetcher(cfg["oj"]["base_url"], cookie, cfg, log)

    exists, nickname = check_user_exists(fetcher, user, log)
    if not exists:
        raise UserNotFound("OJ 用户不存在：%s（userinfo.php 返回 No such User）" % user)

    if cookie:
        log.info("检测到 Cookie（来源：%s），优先使用 Cookie 鉴权并跳过密码登录。" % cookie_source)
        state = check_logged_in(fetcher, dump_html=dump_html)
        if state is False:
            raise AuthError(
                "Cookie 登录状态无效或已过期（modifypage.php 显示未登录）。\n"
                "请更新 BMEOJ_COOKIE / config.local.toml 的 [oj] cookie；"
                "如果同时配置了密码，请移除 Cookie 后重试密码登录。"
            )
        if state is None:
            log.warn("无法确认 Cookie 登录状态（页面结构未知）；将继续尝试。")
        return fetcher, {
            "kind": "cookie",
            "source": cookie_source,
            "verified": state is True,
            "nickname": nickname,
        }

    password, password_source = resolve_password(
        args.local_cfg, log, required=require_auth
    )
    if not password:
        return fetcher, {
            "kind": "anonymous",
            "source": "",
            "verified": False,
            "nickname": nickname,
        }

    login(fetcher, user, password, password_source, log, dump_html=dump_html)
    return fetcher, {
        "kind": "password",
        "source": password_source,
        "verified": True,
        "nickname": nickname,
    }


def run_archive(cfg, args, user, target, log):
    base = cfg["oj"]["base_url"]

    log.info("OJ：%s" % base)
    log.info("用户：%s" % user)
    log.info("目标：%s" % target)
    log.info("模式：%s / 布局：%s / 模板：%s" % (
        cfg["archive"]["mode"], cfg["archive"]["layout"], cfg["archive"]["filename_template"]))
    log.info("仅限本人账号或已授权账号；请遵守 OJ 服务条款与学术诚信。本工具不会 push。")

    fetcher, auth = prepare_session(cfg, args, user, log, require_auth=True)
    log.info("用户存在%s。" % ("，昵称：%s" % auth["nickname"] if auth["nickname"] else ""))
    log.info("鉴权：%s" % describe_auth(auth))
    if auth["kind"] == "password":
        log.info("会话：已登录（PHPSESSID 已建立，值已脱敏；modifypage.php 验证通过）。")
    elif auth["kind"] == "cookie":
        if auth["verified"]:
            log.info("Cookie 登录状态：正常（值已脱敏）。")
        else:
            log.warn("无法确认 Cookie 登录状态；如果源码下载失败，请更新 Cookie 或改用密码登录。")

    rows = fetch_all_submissions(fetcher, user, log, max_pages=args.max_pages)
    log.info("提交记录：%d 条" % len(rows))
    accepted = [row for row in rows if row.get("accepted")]
    log.info("其中正确/Accepted：%d 条" % len(accepted))
    if not accepted:
        if rows:
            log.warn("该用户有提交记录，但没有 AC/正确提交。未生成源码文件。")
        else:
            log.warn("没有读到任何提交记录：可能是新用户、提交全部隐藏，或站点页面结构变化。")
            log.warn("可运行 --probe 查看页面结构。")
        if args.dry_run:
            log.info("")
            log.info("dry-run：只列计划，不写任何文件。")
            return 0
        os.makedirs(target, exist_ok=True)
        log.attach(os.path.join(target, "logs", "archive_%s.log" % time.strftime("%Y%m%d-%H%M%S")))
        manifest = load_manifest(target, log)
        manifest["source"] = {
            "base_url": base,
            "user": user,
            "mode": cfg["archive"]["mode"],
            "layout": cfg["archive"]["layout"],
            "filename_template": cfg["archive"]["filename_template"],
        }
        write_manifest(target, manifest, cfg, user, log)
        write_repo_files(target, manifest, cfg, user, log)
        if args.init_git:
            init_git(target, user, cfg, log)
        log.info("未执行 git push，也未创建任何远程仓库。")
        if log.path:
            log.info("日志：%s" % rel_display(log.path, target))
        return 0

    manifest = load_manifest(target, log)
    items = _plan_items(rows, cfg, manifest, target, fetcher, log, args)
    downloads = [item for item in items if item["action"] in ("create", "update")]
    skips = [item for item in items if item["action"] == "skip"]

    log.info("")
    log.info("计划：新增/更新 %d 个，跳过 %d 个。" % (len(downloads), len(skips)))
    for item in skips:
        log.info("  [跳过] %s P%s：%s" % (item["cid"], item["part"], item["reason"]))
    for item in downloads:
        log.info("  [%s] %s P%s -> %s" % (
            "更新" if item["action"] == "update" else "新增",
            item["cid"], item["part"], item["filename"]))

    if args.dry_run:
        log.info("")
        log.info("dry-run：只列计划，不下载源码、不写任何文件。")
        return 0

    os.makedirs(target, exist_ok=True)
    log.attach(os.path.join(target, "logs", "archive_%s.log" % time.strftime("%Y%m%d-%H%M%S")))

    # 先全部下载到内存，再统一写盘：会话失效时不会留下半套文件。
    auth_errors = 0
    session_state = None
    for index, item in enumerate(downloads, 1):
        log.step("[%d/%d] 下载 %s P%s（提交 %s）" % (
            index, len(downloads), item["cid"], item["part"], item["row"]["sid"]))
        try:
            code = fetch_source(fetcher, item["row"]["sid"], log, dump_html=args.dump_html)
        except AuthError:
            auth_errors += 1
            if session_state is None:
                session_state = check_logged_in(fetcher, dump_html=args.dump_html)
            if session_state is False:
                raise AuthError(
                    "登录状态已失效（会话过期或被站点注销），已停止下载，未写入任何提交文件。\n"
                    "请重新运行；如果反复失效，请检查账号是否被限制登录。"
                )
            if session_state is True:
                log.error(
                    "登录成功，但 OJ 拒绝查看提交 %s 的源码：该提交可能不属于账号 %s，"
                    "或站点限制了源码可见性。" % (item["row"]["sid"], user)
                )
            else:
                log.error(
                    "无法查看提交 %s 的源码：可能是会话失效、该提交不属于账号 %s，"
                    "或站点限制了源码可见性。" % (item["row"]["sid"], user)
                )
            item["action"] = "failed"
            continue
        except ArchiveError as exc:
            log.error("提交 %s 下载失败：%s" % (item["row"]["sid"], exc))
            item["action"] = "failed"
            continue
        item["code"] = clean_code(code, item["strip_footer"])

    if downloads and auth_errors == len(downloads):
        if session_state is True:
            raise AuthError(
                "登录成功，但所有源码下载都被 OJ 拒绝：账号 %s 可能无权查看这些提交，"
                "或站点限制了源码可见性。" % user
            )
        raise AuthError(
            "所有源码下载都被拒绝：登录状态可能已失效，或账号 %s 无权查看这些提交。"
            % user
        )

    written = 0
    updated = 0
    failed = 0
    for item in downloads:
        if item.get("code") is None:
            failed += 1
            continue
        path = _fs_path(target, item["filename"])
        if os.path.isfile(path):
            _backup_existing(target, item["filename"], log)
        try:
            status = atomic_write(path, item["code"])
        except OSError as exc:
            log.error("写入 %s 失败：%s" % (item["filename"], exc))
            failed += 1
            continue
        entry = _make_entry(item, cfg, user, item["code"])
        _upsert_entry(manifest, entry, cfg["archive"]["mode"])
        if status == "updated":
            updated += 1
        else:
            written += 1
        log.info("    %s：%s" % ("已更新" if status == "updated" else "已写入", item["filename"]))

    manifest["source"] = {
        "base_url": base,
        "user": user,
        "mode": cfg["archive"]["mode"],
        "layout": cfg["archive"]["layout"],
        "filename_template": cfg["archive"]["filename_template"],
    }
    write_manifest(target, manifest, cfg, user, log)
    write_repo_files(target, manifest, cfg, user, log)

    if args.init_git:
        log.info("")
        init_git(target, user, cfg, log)

    log.info("")
    log.info("完成：新增 %d，更新 %d，跳过 %d，失败 %d。" % (
        written, updated, len(skips), failed))
    if failed:
        log.warn("有 %d 个文件失败；可用 --force 重试，或先运行 --probe 检查页面。" % failed)
    log.info("未执行 git push，也未创建任何远程仓库。")
    if log.path:
        log.info("日志：%s" % rel_display(log.path, target))
    return 0


# --------------------------------------------------------------------------- #
# 探测模式
# --------------------------------------------------------------------------- #
def run_probe(cfg, args, user, log):
    base = cfg["oj"]["base_url"]

    log.info("=== BME OJ 探测模式 ===")
    log.info("OJ：%s" % base)
    log.info("用户：%s" % user)
    log.info("请求间隔：%.1f 秒；超时：%.0f 秒；重试：%d" % (
        cfg["oj"]["request_interval"], cfg["oj"]["timeout"], cfg["oj"]["retries"]))
    log.info("")

    fetcher, auth = prepare_session(
        cfg, args, user, log, require_auth=False, dump_html=args.dump_html
    )
    log.info("用户存在：%s%s" % (user, "（%s）" % auth["nickname"] if auth["nickname"] else ""))
    log.info("鉴权：%s" % describe_auth(auth))
    if auth["kind"] == "password":
        log.info("会话状态：已登录（PHPSESSID 已建立，值已脱敏；modifypage.php 验证通过）。")
    elif auth["kind"] == "cookie":
        if auth["verified"]:
            log.info("会话状态：已登录（复用 Cookie，值已脱敏；modifypage.php 验证通过）。")
        else:
            log.warn("会话状态：无法确认 Cookie 是否有效（页面结构未知）。")
    else:
        log.warn("会话状态：未登录（未提供密码/Cookie）。源码页预计不可读。")

    try:
        html = fetcher.get("status.php", {"user_id": user}, "提交列表（探测）")
    except ArchiveError as exc:
        log.error("提交列表请求失败：%s" % exc)
        return 3
    rows = parse_status_page(html)
    log.info("首页解析到 %d 行提交。" % len(rows))
    for row in rows[:5]:
        log.info("  sid=%s cid=%s pid=%s result=%s(%s) lang=%s time=%s" % (
            row["sid"], row["cid"], row["pid"], row["result_code"],
            row["result_text"], row["language"] or "-", row["submitted_at"]))
    accepted = [row for row in rows if row["accepted"]]
    log.info("首页 AC：%d 条" % len(accepted))

    target_row = None
    if args.sid:
        target_row = {"sid": str(args.sid), "cid": "", "pid": -1}
    elif accepted:
        target_row = accepted[0]

    if target_row:
        log.info("")
        log.info("测试源码页：showsource.php?id=%s" % target_row["sid"])
        try:
            code = fetch_source(fetcher, target_row["sid"], log, dump_html=args.dump_html)
            cleaned = clean_code(code, cfg["archive"].get("strip_footer", True))
            log.info("源码可读：原始 %d 字符，清洗后 %d 字符。" % (len(code), len(cleaned)))
            log.info("首行：%s" % (cleaned.splitlines()[0] if cleaned.splitlines() else ""))
        except AuthError:
            if auth["kind"] == "anonymous":
                log.warn("源码不可读：当前未登录（未提供密码/Cookie）。")
            elif auth["kind"] == "cookie" and not auth["verified"]:
                log.warn("源码不可读：无法确认 Cookie 登录状态，源码页拒绝了请求。")
            else:
                log.warn(
                    "登录成功，但 OJ 拒绝查看提交 %s 的源码：该提交可能不属于账号 %s，"
                    "或站点限制了源码可见性。" % (target_row["sid"], user)
                )
            if auth["kind"] != "password" and not auth.get("verified"):
                log.warn("如需验证登录，请配置 BMEOJ_PASSWORD 或 BMEOJ_COOKIE 后重试。")
        except ArchiveError as exc:
            log.error("源码页解析失败：%s" % exc)
            return 3

    log.info("")
    log.info("探测完成。若页面结构变化，可加 --dump-html 把 HTML 存到 _debug/。")
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        prog="bmeoj_archive.py",
        description="导出 oj.bmeonline.cn 上指定用户的 AC 提交，生成可发布到 GitHub 的本地目录。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python bmeoj_archive.py\n"
            "  python bmeoj_archive.py --user 11226128 --dir output\n"
            "  python bmeoj_archive.py --all --force --overwrite\n"
            "  python bmeoj_archive.py --init-git\n"
            "  python bmeoj_archive.py --probe --user 11226128 --dump-html\n"
        ),
    )
    parser.add_argument("--user", "-u", help="OJ 用户名；不填则交互提示。")
    parser.add_argument("--dir", "-d", dest="directory", help="目标文件夹；不填则提示，默认 output。")
    parser.add_argument("--all", action="store_true", help="保留全部 AC，不只最新一次。")
    parser.add_argument("--force", action="store_true", help="强制重新下载并覆盖（含未变化的提交）。")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖已存在的 .cpp（默认跳过）。")
    parser.add_argument("--init-git", action="store_true", help="只在本地 git init/add/commit，不 push。")
    parser.add_argument("--license", action="store_true", help="生成 LICENSE（MIT，仅覆盖工具/文档）。")
    parser.add_argument("--dry-run", action="store_true", help="只列计划，不下载、不写文件。")
    parser.add_argument("--probe", action="store_true", help="验证登录/检查页面结构，不写提交。")
    parser.add_argument("--dump-html", action="store_true", help="解析失败时把 HTML 存到 _debug/。")
    parser.add_argument("--sid", type=int, help="--probe 时指定要测试的提交 ID。")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 道题（调试用，0=全部）。")
    parser.add_argument("--max-pages", type=int, default=200, help="提交列表最多翻多少页。")
    parser.add_argument("--interval", type=float, help="请求间隔秒数（下限 1.0）。")
    parser.add_argument("--base-url", help="OJ 根地址（调试/自建镜像用）。")
    parser.add_argument("--keep-footer", action="store_true", help="保留 OJ 追加的 /***** 尾部注释。")
    parser.add_argument("--no-titles", action="store_true", help="不抓题名，用占位标题（离线/极慢网络）。")
    parser.add_argument("--config", default=os.path.join(HERE, "config.toml"), help=argparse.SUPPRESS)
    parser.add_argument("--local-config", default=os.path.join(HERE, "config.local.toml"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version="bmeoj_archive.py %s" % VERSION)
    return parser


def ask_missing(args, require_dir=True):
    if not args.user:
        if not sys.stdin.isatty():
            raise ConfigError("缺少 --user，且当前不是交互终端。")
        try:
            args.user = input("OJ 用户名: ").strip()
        except EOFError:
            args.user = ""
    if not args.user:
        raise ConfigError("OJ 用户名不能为空。")

    if require_dir and not args.directory:
        default_dir = "output"
        if sys.stdin.isatty():
            try:
                entered = input("目标文件夹（默认 %s）: " % default_dir).strip()
            except EOFError:
                entered = ""
            args.directory = entered or default_dir
        else:
            args.directory = default_dir
    if require_dir and not args.directory:
        raise ConfigError("目标文件夹不能为空。")
    return args


def main(argv=None):
    global _ACTIVE_COOKIE
    parser = build_parser()
    args = parser.parse_args(argv)
    _ACTIVE_COOKIE = ""
    del _ACTIVE_SECRETS[:]

    # --help / --version 已在 argparse 内处理；这里再做依赖与版本检查。
    if sys.version_info < (3, 8):
        print("需要 Python 3.8 或更高版本。当前：%s" % sys.version.split()[0], file=sys.stderr)
        return 2

    log = Logger()
    try:
        cfg, _, local = resolve_config(args)
        args.local_cfg = local
        args = ask_missing(args, require_dir=not args.probe)

        user = args.user.strip()
        if not re.match(r"^[A-Za-z0-9_.@-]{1,64}$", user):
            raise ConfigError("用户名包含不支持的字符：%r" % user)

        if args.probe:
            return run_probe(cfg, args, user, log)

        target = os.path.abspath(os.path.expanduser(args.directory))
        if os.path.exists(target) and not os.path.isdir(target):
            raise ConfigError("目标路径不是文件夹：%s" % target)

        if args.init_git and args.dry_run:
            log.warn("--dry-run 与 --init-git 同时使用时不会初始化 git。")

        cfg["oj"]["user"] = user
        return run_archive(cfg, args, user, target, log)

    except UserNotFound as exc:
        log.error(str(exc))
        return 5
    except AuthError as exc:
        log.error(str(exc))
        return 4
    except NetworkError as exc:
        log.error(str(exc))
        return 3
    except ConfigError as exc:
        log.error(str(exc))
        return 2
    except ArchiveError as exc:
        log.error(str(exc))
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    finally:
        log.flush()


if __name__ == "__main__":
    sys.exit(main())
