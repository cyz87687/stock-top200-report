#!/usr/bin/env python3
"""将本地生成的报告与评分数据上传至 GitHub Pages 源(main 分支)。
通过 gh api PUT contents 接口(base64 + 当前 SHA)。
用法: python3 upload826.py <index_html> <scored_json>
"""
import sys, os, base64, json, subprocess

REPO = "cyz87687/stock-top200-report"
ENV = ["env", "-u", "HTTP_PROXY", "-u", "HTTPS_PROXY", "-u", "http_proxy", "-u", "https_proxy"]


def gh_api(method, path, data=None):
    cmd = ENV + ["gh", "api", f"repos/{REPO}/contents/{path}"]
    if method != "GET":
        cmd += ["-X", method, "-H", "Content-Type: application/json"]
        if data is not None:
            cmd += ["--input", "-"]
    p = subprocess.run(cmd, input=json.dumps(data) if data is not None else None,
                       capture_output=True, text=True)
    return p


def get_sha(path):
    p = gh_api("GET", path)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout).get("sha")
    except Exception:
        return None


def put_file(local_path, repo_path):
    with open(local_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("ascii")
    sha = get_sha(repo_path)
    data = {
        "message": f"auto: update report {repo_path} (v2.21)",
        "content": content_b64,
        "branch": "main",
    }
    if sha:
        data["sha"] = sha
    p = gh_api("PUT", repo_path, data)
    if p.returncode == 0:
        print(f"  ✅ PUT {repo_path} -> {json.loads(p.stdout).get('commit',{}).get('sha','?')[:10]}")
        return True
    else:
        print(f"  ❌ PUT {repo_path} 失败: {p.stderr[:300]}")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: upload826.py <index_html> <scored_json>")
        sys.exit(1)
    index_html = sys.argv[1]
    scored_json = sys.argv[2]
    print(f"上传: {index_html} 与 {scored_json} 至 {REPO} (main)")
    ok1 = put_file(index_html, "index.html")
    ok2 = put_file(scored_json, os.path.basename(scored_json))
    sys.exit(0 if (ok1 and ok2) else 1)
