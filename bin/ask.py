#!/usr/bin/env python3
"""Ask Jarvis from the terminal (goes through Open WebUI, so it uses the same prompt + vault retrieval).

usage: ask.py "your question" [--model jarvis] [--show-thinking]
"""
import json
import re
import sys
import time
import urllib.request

WEBUI = "http://127.0.0.1:8484"


def post(path, body, token=None, timeout=600):
    req = urllib.request.Request(f"{WEBUI}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    model = "jarvis"
    if "--model" in sys.argv:
        model = sys.argv[sys.argv.index("--model") + 1]
        args = [a for a in args if a != model]
    if not args:
        sys.exit(__doc__)
    token = post("/api/v1/auths/signin", {"email": "admin@localhost", "password": "admin"})["token"]
    t = time.time()
    r = post("/api/chat/completions", {"model": model, "stream": False,
                                       "messages": [{"role": "user", "content": " ".join(args)}]}, token)
    text = r["choices"][0]["message"]["content"]
    if "--show-thinking" not in sys.argv:
        text = re.sub(r"<think>.*?</think>|<details type=\"reasoning\".*?</details>", "", text, flags=re.S).strip()
    print(text)
    srcs = sorted({m.get("source") or m.get("name", "") for s in r.get("sources", []) for m in s.get("metadata", [])} - {""})
    u = r.get("usage", {})
    print(f"\n[{time.time() - t:.1f}s | {u.get('completion_tokens', '?')} tokens out"
          f"{' | ' + str(round(u['completion_tokens'] / (u['eval_duration'] / 1e9 if 'eval_duration' in u else 1), 1)) + ' tok/s' if 'eval_duration' in u else ''}"
          f"{' | retrieved: ' + ', '.join(srcs) if srcs else ''}]", file=sys.stderr)


if __name__ == "__main__":
    main()
