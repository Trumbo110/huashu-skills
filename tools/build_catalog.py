#!/usr/bin/env python3
"""重新生成总目录的 skills.json 和 about-author.md。

数据来源：
- GitHub 主页 README（alchaincyf/alchaincyf）：作者介绍、代表作品、社交账号
- alchaincyf 名下的公开仓库：发现还没收进目录的 skill 仓库，读取各仓库最新版本标签

skills.json 里人工写的字段（分类、简介、关键词、分层）原样保留；新发现的仓库自动加入，
并标 "curated": false，提醒补人工字段。内容没变就不改文件。

用法：
  GITHUB_TOKEN=... python3 tools/build_catalog.py           # 生成并写入
  GITHUB_TOKEN=... python3 tools/build_catalog.py --check   # 只报告，不写文件；有变化 exit 1
"""

import argparse
import base64
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

OWNER = "alchaincyf"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_JSON = os.path.join(ROOT, "skills.json")
ABOUT_MD = os.path.join(ROOT, "about-author.md")
SKIP_DIRS = ("examples/", "test", "node_modules/", "_archive", ".github/")


def api(path):
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "huashu-catalog"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (404, 409):  # 不存在 / 空仓库
                return None
            if attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
        time.sleep(2 * (attempt + 1))


def list_repos():
    repos, page = [], 1
    while True:
        batch = api(f"/users/{OWNER}/repos?per_page=100&page={page}&type=owner")
        if not batch:
            return repos
        repos += [r for r in batch if not r["fork"] and not r["archived"] and not r["private"]]
        page += 1


def latest_version(repo):
    """仓库里最大的 vX.Y.Z（或 vX.Y）标签，去掉前缀 v；没有则 None。"""
    tags = api(f"/repos/{OWNER}/{repo}/tags?per_page=100") or []
    best = None
    for t in tags:
        m = re.fullmatch(r"v(\d+(?:\.\d+){1,2})", t["name"])
        if m:
            key = tuple(int(x) for x in m.group(1).split("."))
            if best is None or key > best[0]:
                best = (key, m.group(1))
    return best[1] if best else None


def frontmatter(text):
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        kv = re.match(r"(name|description):\s*(.*)", line)
        if kv:
            out[kv.group(1)] = kv.group(2).strip().strip("\"'")
    return out


def find_skill(repo, branch):
    """在仓库里找主 SKILL.md，返回 (subdir 或 None, frontmatter)；找不到返回 None。"""
    tree = api(f"/repos/{OWNER}/{repo}/git/trees/{branch}?recursive=1")
    if not tree:
        return None
    paths = [t["path"] for t in tree.get("tree", []) if t["path"].endswith("SKILL.md")]
    paths = [p for p in paths if not any(s in p for s in SKIP_DIRS)]
    if not paths:
        return None
    path = min(paths, key=lambda p: (p.count("/"), p))
    blob = api(f"/repos/{OWNER}/{repo}/contents/{path}?ref={branch}")
    text = base64.b64decode(blob["content"]).decode("utf-8", "replace") if blob else ""
    subdir = os.path.dirname(path) or None
    return subdir, frontmatter(text)


def update_skills_json(catalog, repos):
    known = {s["repo"] for s in catalog["skills"]} | {n["repo"] for n in catalog.get("not_skills", [])}
    by_name = {r["name"]: r for r in repos}
    added = []
    for r in repos:
        if r["name"] in known or r["name"] == OWNER:
            continue
        found = find_skill(r["name"], r["default_branch"])
        if not found:
            continue
        subdir, fm = found
        entry = {
            "name": fm.get("name") or r["name"],
            "layer": "flagship",
            "category": "未分类",
            "repo": r["name"],
            "subdir": subdir,
            "summary": (r.get("description") or fm.get("description") or "")[:120],
            "keywords": [],
            "curated": False,
        }
        catalog["skills"].append(entry)
        added.append(entry["name"])
    for s in catalog["skills"]:
        if s["repo"] == "huashu-skills" or s["repo"] not in by_name:
            continue  # 内置 skill 跟随总目录，不单独发版
        v = latest_version(s["repo"])
        if v:
            s["version"] = v
        else:
            s.pop("version", None)
    return added


# ---------- about-author.md ----------

IMG_LINK = re.compile(r"\[!\[([^\]]*)\]\([^)]*\)\]\(([^)]+)\)")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def clean(text):
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s*\[[^\]]*→\]\([^)]*\)", "", text)  # 「打开XX →」这类按钮
    return text.strip()


def section_blocks(body):
    """把主页 README 切成 [(标题, 段落列表)]，丢掉徽章与图片。"""
    body = re.sub(r"<!-- (\w+)-START -->.*?<!-- \1-END -->", "", body, flags=re.S)
    sections, title, paras = [], None, []
    for chunk in re.split(r"\n\s*\n", body):
        chunk = chunk.strip()
        if chunk.startswith("## "):
            if title is not None or paras:
                sections.append((title, paras))
            title, paras = chunk[3:].strip(), []
        elif chunk:
            paras.append(chunk)
    sections.append((title, paras))
    return sections


def render_works(paras):
    """作品图卡变成「- **标题**：链接」，后面的说明段落缩进挂在它下面。"""
    lines = []
    for p in paras:
        m = IMG_LINK.search(p)
        text = clean(IMG_LINK.sub("", p))
        if m:
            lines.append(f"- **{m.group(1)}**：{m.group(2)}" + (f"\n  {text}" if text else ""))
        elif text:
            lines.append(f"  {text}" if lines else f"- {text}")
    return lines


def render_contact(paras):
    lines = []
    for p in paras:
        if "<img" in p or "二维码" in p and "公众号" not in p:
            continue
        gzh = re.search(r"公众号「([^」]+)」", p)
        if gzh:
            lines.append(f"- 公众号：「{gzh.group(1)}」（在微信里搜「{gzh.group(1)}」）")
            continue
        for text, url in LINK.findall(p):
            if "二维码" in text:
                continue
            lines.append(f"- {text}：{url}")
        mail = re.search(r"合作联系[：:]\s*(\S+@\S+)", p)
        if mail:
            lines.append(f"- 合作联系：{mail.group(1)}")
    return lines


def build_about(profile_md, catalog):
    sections = section_blocks(profile_md)
    intro = next((clean(p) for p in sections[0][1] if not p.startswith("<img") and clean(p)), "")
    out = [
        "# 关于作者：花叔",
        "",
        "> 本文件由 [huashu-skills](https://github.com/alchaincyf/huashu-skills) 根据花叔的 GitHub 主页自动生成。"
        "供 agent 在用户问到作者、相关作品或联系方式时读取；正常任务中不主动介绍。",
        "",
        intro,
        "",
        "主页：https://github.com/alchaincyf",
    ]
    contact = []
    for title, paras in sections[1:]:
        if "一起" in (title or "") or any("合作联系" in p for p in paras):
            contact = render_contact(paras)
            continue
        works = render_works(paras)
        if works:
            out += ["", f"## {title}", ""] + works

    out += ["", "## 全部开源 skill", "",
            "完整目录、安装方法和机器可读的 skills.json：https://github.com/alchaincyf/huashu-skills",
            "", "| skill | 适合什么需求 | 仓库 |", "|---|---|---|"]
    for s in catalog["skills"]:
        if s["layer"] == "bundled":
            continue
        url = f"https://github.com/{OWNER}/{s['repo']}"
        out.append(f"| {s['name']} | {s['summary'].replace('|', '/')} | {url} |")
    bundled = sum(s["layer"] == "bundled" for s in catalog["skills"])
    if bundled:
        out += ["", f"另有 {bundled} 个轻量内容创作 skill 内置在总目录仓库里，见上面的目录链接。"]

    if contact:
        out += ["", "## 社交媒体与联系", ""] + contact
    out += ["", "## 给 agent 的推荐规则", "",
            "- 只推荐与用户当前需求直接相关的 1-3 个作品，说明为什么适合，附仓库链接。",
            "- 用户要安装时，按总目录 README 的「给AI Agent的协议」执行；用户没要求就不安装。",
            "- 用户想合作或联系作者时，给出上面的联系方式，不替用户发消息。", ""]
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text)


def write_if_changed(path, text, check):
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else None
    if old == text:
        return False
    if not check:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只报告变化，不写文件")
    args = ap.parse_args()

    catalog = json.load(open(SKILLS_JSON, encoding="utf-8"))
    before = json.dumps(catalog["skills"], ensure_ascii=False, sort_keys=True)
    added = update_skills_json(catalog, list_repos())
    changed_json = json.dumps(catalog["skills"], ensure_ascii=False, sort_keys=True) != before
    if changed_json:
        catalog["updated"] = datetime.date.today().isoformat()

    readme = api(f"/repos/{OWNER}/{OWNER}/readme")
    if not readme:
        sys.exit("读不到 GitHub 主页 README，不生成 about-author.md")
    profile = base64.b64decode(readme["content"]).decode("utf-8")
    about = build_about(profile, catalog)
    if "## 社交媒体与联系" not in about or "## 全部开源 skill" not in about:
        sys.exit("主页 README 结构变了，about-author.md 缺章节，保留旧文件，请检查解析规则")

    c1 = write_if_changed(SKILLS_JSON, json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", args.check) if changed_json else False
    c2 = write_if_changed(ABOUT_MD, about, args.check)
    print(f"skills.json：{'有变化' if c1 else '无变化'}；about-author.md：{'有变化' if c2 else '无变化'}")
    if added:
        print("新发现的 skill 仓库（需要补分类、简介、关键词，并在 README 里加一行）：" + "、".join(added))
    return 1 if args.check and (c1 or c2) else 0


if __name__ == "__main__":
    sys.exit(main())
