#!/usr/bin/env python3
"""通过 GitHub Git Data API 推送本地改动 (网络受限时的备用通道)。

背景: 本机 git push 走 github.com:443 常被代理 CONNECT 502 拦截, 但 api.github.com
      经代理稳定可达。本脚本用 Git Data API 组装单个原子 commit 并更新 ref, 绕开 git 协议。

用法:
  python3 push_via_api.py <file1> <file2> ...     # 推送指定文件
  python3 push_via_api.py --all-changed           # 推送所有已修改/新增的已跟踪文件

要求: gh CLI 已认证 (脚本内取 `gh auth token`)。
"""
import os
import sys
import json
import base64
import subprocess
import urllib.request
import urllib.error

REPO = "cyz87687/stock-top200-report"
BRANCH = "main"
HERE = os.path.dirname(os.path.abspath(__file__))


def _proxy():
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k)
        if v:
            return v
    return None


def _token():
    return subprocess.check_output(["gh", "auth", "token"], text=True).strip()


class Api:
    def __init__(self):
        self.token = _token()
        self.proxy = _proxy()
        if self.proxy:
            handler = urllib.request.ProxyHandler({"https": self.proxy, "http": self.proxy})
            self.opener = urllib.request.build_opener(handler)
        else:
            self.opener = urllib.request.build_opener()

    def call(self, method, path, payload=None):
        url = f"https://api.github.com/repos/{REPO}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"token {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "push-via-api")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=60) as r:
                body = r.read().decode()
            return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:400]}")


def git_mode(path):
    """返回 git tree mode: 100755(可执行) 或 100644(普通)。"""
    full = os.path.join(HERE, path)
    return "100755" if os.access(full, os.X_OK) else "100644"


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    if args[0] == "--all-changed":
        out = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=HERE, text=True)
        paths = []
        for line in out.splitlines():
            st, p = line[:2], line[3:].strip()
            if p.endswith("/"):
                continue
            if not os.path.isfile(os.path.join(HERE, p)):
                continue
            paths.append(p)
    else:
        paths = args

    if not paths:
        print("无待推送文件")
        return 0

    api = Api()
    print(f"代理: {api.proxy or '(无)'}")
    print(f"待推送 {len(paths)} 个文件")

    # 1) 取远程当前 commit / base tree
    ref = api.call("GET", f"/git/ref/heads/{BRANCH}")
    base_commit = ref["object"]["sha"]
    commit_obj = api.call("GET", f"/git/commits/{base_commit}")
    base_tree = commit_obj["tree"]["sha"]
    print(f"远程 base commit: {base_commit[:12]}")

    # 2) 逐文件建 blob
    tree_items = []
    for p in paths:
        full = os.path.join(HERE, p)
        if not os.path.exists(full):
            print(f"  跳过(不存在): {p}")
            continue
        with open(full, "rb") as f:
            raw = f.read()
        blob = api.call("POST", "/git/blobs", {
            "content": base64.b64encode(raw).decode(),
            "encoding": "base64",
        })
        mode = git_mode(p)
        tree_items.append({"path": p, "mode": mode, "type": "blob", "sha": blob["sha"]})
        print(f"  blob {p} ({len(raw)} bytes, mode {mode})")

    # 3) 建 tree(基于 base_tree, 只覆盖改动文件)
    tree = api.call("POST", "/git/trees", {"base_tree": base_tree, "tree": tree_items})
    print(f"新 tree: {tree['sha'][:12]}")

    # 4) 建 commit
    msg = os.environ.get("COMMIT_MSG") or "fix: 推荐跟踪表接入每日链路 + 回填bug修复 + cron延迟补偿"
    new_commit = api.call("POST", "/git/commits", {
        "message": msg, "tree": tree["sha"], "parents": [base_commit],
    })
    print(f"新 commit: {new_commit['sha'][:12]}")

    # 5) 更新 ref (非 force: 若远程已前进会失败, 需先 fetch+rebase)
    api.call("PATCH", f"/git/refs/heads/{BRANCH}", {"sha": new_commit["sha"]})
    print(f"✅ 已更新 refs/heads/{BRANCH} -> {new_commit['sha'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
