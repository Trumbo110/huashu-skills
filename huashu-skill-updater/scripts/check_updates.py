#!/usr/bin/env python3
"""检查花叔系 skills 是否有新版本。

识别两类安装方式：
- git clone 安装：目录带 .git，且 origin 指向 github.com/alchaincyf。
  比较 SKILL.md 里的 metadata.version 和远程仓库最大的 vX.Y.Z 标签——只有作者打了标签的实质更新才算新版本
- 复制安装：目录带 .huashu-skill-meta.json（含来源仓库与安装时 commit），按 commit 比较

用法：
  python3 check_updates.py                        # 检查 ~/.claude/skills
  python3 check_updates.py --skills-dir <目录>     # 检查其他 skills 目录
  python3 check_updates.py --mark-checked         # 检查并回写 last_checked

exit code：0 = 全部最新；1 = 有可更新项；2 = 出错。
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

OWNER = "alchaincyf"


def run(cmd, cwd=None, timeout=60):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, "", str(e)


def remote_head(repo):
    """repo 形如 'alchaincyf/huashu-skills' 或裸仓库名。返回远程 HEAD sha 或 None。"""
    if "/" not in repo:
        repo = f"{OWNER}/{repo}"
    code, out, _ = run(["git", "ls-remote", f"https://github.com/{repo}.git", "HEAD"])
    if code == 0 and out:
        return out.split()[0]
    return None


def version_tuple(v):
    return tuple(int(x) for x in v.split("."))


def local_version(path):
    """读 SKILL.md frontmatter 里 metadata.version；没有返回 None。"""
    try:
        text = open(os.path.join(path, "SKILL.md"), encoding="utf-8").read()
    except OSError:
        return None
    fm = text.split("---", 2)[1] if text.startswith("---") else ""
    m = re.search(r"^metadata:\s*\n((?:[ \t]+.*\n?)*)", fm, re.M)
    v = re.search(r"^[ \t]+version:\s*[\"']?(\d+(?:\.\d+){1,2})", m.group(1), re.M) if m else None
    return v.group(1) if v else None


def remote_version(path):
    """返回 (是否联网成功, 远程最大版本号或 None)。只读标签，不下载文件。"""
    code, out, _ = run(["git", "-C", path, "ls-remote", "--tags", "--refs",
                        "--sort=-v:refname", "origin", "v*"])
    if code != 0:
        return False, None
    for line in out.splitlines():
        m = re.search(r"refs/tags/v(\d+(?:\.\d+){1,2})$", line)
        if m:
            return True, m.group(1)
    return True, None


def check_git_install(path, mark_checked=False):
    code, url, _ = run(["git", "-C", path, "config", "--get", "remote.origin.url"])
    if code != 0 or OWNER not in url:
        return None  # 不是花叔系，跳过
    ok, remote = remote_version(path)
    if not ok:
        return ("unknown", "无法访问远程仓库（断网或被拦截），下次再查", None)
    state = os.path.join(path, ".last-update-check")
    user_off = os.path.exists(state) and open(state).read().strip() == "off"
    if mark_checked and not user_off:
        # skill 末尾「版本检查」段读这个文件决定 30 天内是否跳过联网；只有联网成功才写，用户写了 off 不覆盖
        with open(state, "w") as f:
            f.write(datetime.date.today().isoformat() + " ok\n")
    local = local_version(path)
    if remote is None:
        return ("ok", "远程还没有发布版本标签", None)
    if local and version_tuple(local) >= version_tuple(remote):
        return ("ok", f"最新（v{local}）", None)
    return ("outdated", f"本地 {'v' + local if local else '未标版本'} → 远端 v{remote}",
            f"git -C {path} pull --ff-only")


def check_copy_install(path, meta_file, mark_checked):
    try:
        meta = json.load(open(meta_file, encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return ("unknown", f"meta 文件损坏：{e}", None)
    repo = meta.get("repo", "")
    local = meta.get("commit", "")
    remote = remote_head(repo) if repo else None
    if not remote:
        return ("unknown", f"无法访问远程仓库 {repo or '（meta 缺 repo 字段）'}", None)
    if mark_checked:
        meta["last_checked"] = datetime.date.today().isoformat()
        json.dump(meta, open(meta_file, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    if remote == local:
        return ("ok", f"最新（{local[:7]}，装于 {meta.get('installed_at', '?')}）", None)
    return ("outdated", f"安装时 {local[:7] or '未知'} → 远端 {remote[:7]}",
            f"重新复制来源仓库 {repo} 的 {meta.get('subdir', '?')}/ 子目录（见 huashu-skill-updater/SKILL.md）")


def main():
    p = argparse.ArgumentParser(description="检查花叔系 skills 更新")
    p.add_argument("--skills-dir", default=os.path.expanduser("~/.claude/skills"))
    p.add_argument("--mark-checked", action="store_true",
                   help="联网检查成功后，把 .last-update-check / .huashu-skill-meta.json 的检查日期更新为今天")
    args = p.parse_args()

    skills_dir = os.path.abspath(os.path.expanduser(args.skills_dir))
    if not os.path.isdir(skills_dir):
        print(f"skills 目录不存在：{skills_dir}", file=sys.stderr)
        return 2

    results = []
    for name in sorted(os.listdir(skills_dir)):
        path = os.path.join(skills_dir, name)
        if not os.path.isdir(path):
            continue
        meta_file = os.path.join(path, ".huashu-skill-meta.json")
        if os.path.isdir(os.path.join(path, ".git")):
            r = check_git_install(path, args.mark_checked)
        elif os.path.isfile(meta_file):
            r = check_copy_install(path, meta_file, args.mark_checked)
        else:
            continue  # 非花叔系或无法识别，跳过
        if r:
            results.append((name, *r))

    if not results:
        print(f"{skills_dir} 下没有找到可识别的花叔系 skill。")
        return 0

    icons = {"ok": "✅", "outdated": "⬆️ ", "unknown": "❓"}
    outdated = 0
    for name, status, detail, fix in results:
        print(f"{icons[status]} {name} · {detail}")
        if fix:
            print(f"    更新：{fix}")
        if status == "outdated":
            outdated += 1

    print(f"\n共检查 {len(results)} 个，{outdated} 个有更新。")
    return 1 if outdated else 0


if __name__ == "__main__":
    sys.exit(main())
