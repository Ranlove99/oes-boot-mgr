#!/usr/bin/env python3
"""仓库一致性检查（无需 pytest，CI 与本地均可直接跑）。

把过去踩过的坑固化成可自动验证的约束：
  1. 三处版本号必须一致（manifest / 守护脚本 / 界面标题）——曾出现过脚本忘改版；
  2. fpk 必需的入口文件必须齐全；
  3. 所有会被设备直接解释的脚本必须是 LF，不能混进 CRLF；
  4. 代码里不得再出现「引号内含 > | &」的 dpkg 钩子写法——1.1.14 事故的根因；
  5. 不得提交密钥、令牌或本机绝对路径。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELF = os.path.abspath(__file__)
errors = []
checks = 0


def rel(p):
    return os.path.relpath(p, ROOT).replace(os.sep, "/")


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def ok(msg):
    global checks
    checks += 1
    print("  [OK]   " + msg)


def bad(msg):
    global checks
    checks += 1
    errors.append(msg)
    print("  [FAIL] " + msg)


def require(cond, msg):
    ok(msg) if cond else bad(msg)


# ---------------------------------------------------------------- 1. 版本号
manifest_path = os.path.join(ROOT, "manifest")
manifest = read(manifest_path)


def field(name):
    m = re.search(r"^%s[ \t]*=[ \t]*(.*)$" % re.escape(name), manifest, re.M)
    return m.group(1).strip() if m else ""


version = field("version")
require(bool(re.fullmatch(r"\d+\.\d+\.\d+", version)),
        "manifest 版本号合法：%s" % (version or "缺失"))

script_ver = ""
m = re.search(r'^VERSION="([^"]+)"', read(os.path.join(ROOT, "app/assets/oes-boot-sync.sh")), re.M)
if m:
    script_ver = m.group(1)
require(script_ver == version,
        "守护脚本 VERSION 与 manifest 一致（%s / %s）" % (script_ver or "缺失", version))

html = read(os.path.join(ROOT, "app/ui/index.html"))
m = re.search(r"OES 系统盘管家 v(\d+\.\d+\.\d+)", html)
html_ver = m.group(1) if m else ""
require(html_ver == version,
        "界面标题版本与 manifest 一致（%s / %s）" % (html_ver or "缺失", version))

require(bool(re.search(r"^changelog=%s[：:]" % re.escape(version), manifest, re.M)),
        "changelog 首段对应当前版本")

m = re.search(r"badge/version-(\d+\.\d+\.\d+)-", read(os.path.join(ROOT, "README.md")))
badge_ver = m.group(1) if m else ""
require(badge_ver == version,
        "README 版本徽章与 manifest 一致（%s / %s）" % (badge_ver or "缺失", version))

# ------------------------------------------------------------ 1b. 作者署名
author = field("maintainer")
author_url = field("maintainer_url")
require(bool(author) and not author.startswith("OES "),
        "manifest maintainer 为具体作者：%s" % (author or "缺失"))
require("github.com/%s" % author in author_url,
        "maintainer_url 指向该作者的 GitHub：%s" % (author_url or "缺失"))
require("Copyright (c) 2026 %s" % author in read(os.path.join(ROOT, "LICENSE")),
        "LICENSE 版权人与 maintainer 一致")
require("badge/author-%s" % author in read(os.path.join(ROOT, "README.md")),
        "README 作者徽章与 maintainer 一致")
require("github.com/%s/oes-boot-mgr" % author in read(os.path.join(ROOT, "README.md")),
        "README 写死了仓库地址（无占位符）")

# ------------------------------------------------------- 2. fpk 必需入口文件
required = [
    "manifest", "ICON.PNG", "ICON_256.PNG",
    "cmd/main", "cmd/install_init", "cmd/install_callback",
    "cmd/upgrade_init", "cmd/upgrade_callback",
    "cmd/uninstall_init", "cmd/uninstall_callback",
    "cmd/config_init", "cmd/config_callback",
    "config/privilege", "config/resource", "wizard/install",
    "app/server.py", "app/cli.py", "app/bin/oesboot",
    "app/ui/index.html", "app/ui/config",
    "app/assets/oes-boot-sync.sh",
    "app/core/__init__.py",
]
missing = [p for p in required if not os.path.isfile(os.path.join(ROOT, p))]
require(not missing, "fpk 必需入口文件齐全（%d 项）%s" %
        (len(required), "；缺失 " + ", ".join(missing) if missing else ""))

# 关键脚本必须可执行（git 会记录 filemode）
exec_required = ["app/bin/oesboot", "app/assets/oes-boot-sync.sh", "tools/build.sh", "cmd/main"]
not_exec = [p for p in exec_required if not os.access(os.path.join(ROOT, p), os.X_OK)]
require(not not_exec, "关键脚本具备可执行位%s" %
        ("；缺失 " + ", ".join(not_exec) if not_exec else ""))

repo_required = [
    "README.md", "LICENSE", "CHANGELOG.md", "CONTRIBUTING.md",
    ".gitignore", ".gitattributes", ".editorconfig", "Makefile",
    "tools/build.sh", "tools/check_consistency.py", "tests/test_core.py",
    "docs/README.md", "docs/ARCHITECTURE.md", "docs/TROUBLESHOOTING.md",
    "dev/README.md", ".github/workflows/ci.yml",
    ".github/ISSUE_TEMPLATE/bug_report.md",
]
missing2 = [p for p in repo_required if not os.path.isfile(os.path.join(ROOT, p))]
require(not missing2, "仓库文档与工具文件齐全（%d 项）%s" %
        (len(repo_required), "；缺失 " + ", ".join(missing2) if missing2 else ""))

# ------------------------------------------------------------- 3. 换行符 LF
text_ext = {".py", ".sh", ".html", ".json", ".md", ".yml", ".yaml", ".cfg", ""}
crlf = []
for base, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in (".git", "dist", "__pycache__", ".pytest_cache")]
    for name in files:
        p = os.path.join(base, name)
        if os.path.splitext(name)[1].lower() in {".png", ".jpg", ".jpeg", ".fpk", ".tgz"}:
            continue
        if os.path.splitext(name)[1] not in text_ext and not name.startswith("cmd/"):
            continue
        with open(p, "rb") as f:
            data = f.read()
        if b"\r\n" in data:
            crlf.append(rel(p))
require(not crlf, "所有脚本/文本为 LF 换行%s" %
        ("；含 CRLF: " + ", ".join(crlf[:5]) if crlf else ""))

# ------------------------------------------- 4. 禁止危险的 dpkg 钩子写法
# dpkg 的配置解析器不按 shell 规则处理引号：引号里出现 > | & 会让
# 每一次 dpkg 操作在解析配置阶段直接退出（OTA 装内核 4 秒失败）。
danger = re.compile(r"""["'][^"']*[>|&][^"']*["']""")
hook_hits = []
for base, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in (".git", "dist", "__pycache__", ".pytest_cache")]
    for name in files:
        if os.path.splitext(name)[1] not in {".py", ".sh"} and not name.startswith("cmd/"):
            continue
        p = os.path.join(base, name)
        if os.path.abspath(p) == SELF:
            continue
        for lineno, line in enumerate(read(p).splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            if "post-invoke" not in line.lower() and "postinvoke" not in line.lower():
                continue
            if danger.search(line):
                hook_hits.append("%s:%d" % (rel(p), lineno))
require(not hook_hits, "无「引号内含 > | &」的 dpkg 钩子写法%s" %
        ("；命中 " + ", ".join(hook_hits[:5]) if hook_hits else ""))

# 钩子必须走 wrapper（永远 exit 0），不得直接内联同步命令
guard_src = read(os.path.join(ROOT, "app/core/guard.py"))
require("oesboot-dpkg-hook" in guard_src and "DPKG_HOOK" in guard_src,
        "dpkg 钩子使用独立 wrapper")

# ------------------------------------------------------ 5. 敏感信息 / 本机路径
secret = re.compile(
    r"(?i)(BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|"
    r"password\s*=\s*['\"][^'\"]{8,}|api[_-]?key\s*=\s*['\"][^'\"]{8,})")
local_path = re.compile(r"/workspace/|/root/uploads/|/Users/|[A-Za-z]:\\")
leaks = []
for base, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in (".git", "dist", "__pycache__", ".pytest_cache")]
    for name in files:
        if os.path.splitext(name)[1].lower() in {".png", ".jpg", ".jpeg", ".fpk", ".tgz"}:
            continue
        p = os.path.join(base, name)
        if os.path.abspath(p) == SELF:   # 检查器自身含有这些模式，跳过
            continue
        try:
            content = read(p)
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            if secret.search(line) or local_path.search(line):
                leaks.append("%s:%d" % (rel(p), lineno))
require(not leaks, "无密钥 / 本机绝对路径%s" %
        ("；命中 " + ", ".join(leaks[:5]) if leaks else ""))

# ------------------------------------------------------------------ 汇总
print("\n共 %d 项检查，%d 项失败" % (checks, len(errors)))
if errors:
    for e in errors:
        print("  - " + e)
    sys.exit(1)
print("全部通过")
