#!/usr/bin/env python3
"""Зберігання hash ID вакансій у гілці bot-state через GitHub Contents API."""
import argparse
import base64
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode

from bot import ServiceError, fetch_json, load_state, write_json

STATE = Path("state/sent.json")
META = Path("state/remote.json")
BRANCH = "bot-state"
REMOTE_PATH = ".bot/sent.json"


def api(path, payload=None, method=None, allow_missing=False):
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo or len(repo.split("/")) != 2:
        raise ServiceError("Потрібні GITHUB_TOKEN і GITHUB_REPOSITORY")
    url = "https://api.github.com/repos/" + "/".join(quote(part, safe="") for part in repo.split("/")) + path
    try:
        return fetch_json(url, payload, method, {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10"
        })
    except HTTPError as exc:
        if allow_missing and exc.code == 404:
            return None
        raise ServiceError(f"GitHub state: HTTP {exc.code}; перевірте contents:write і правила гілки bot-state") from None
    except (URLError, TimeoutError, ValueError, OSError):
        raise ServiceError("GitHub state: мережева помилка або неочікувана відповідь") from None


def ensure_branch():
    if api("/git/ref/heads/" + BRANCH, allow_missing=True) is not None:
        return
    default = os.environ.get("GITHUB_DEFAULT_BRANCH")
    if not default:
        default = api("")["default_branch"]
    parent = api("/git/ref/heads/" + quote(default, safe=""))
    api("/git/refs", {"ref": "refs/heads/" + BRANCH, "sha": parent["object"]["sha"]}, "POST")


def pull():
    ensure_branch()
    remote = api("/contents/" + REMOTE_PATH + "?" + urlencode({"ref": BRANCH}), allow_missing=True)
    if remote is None:
        content = {"version": 1, "sent": {}}
        response = api("/contents/" + REMOTE_PATH, {
            "message": "Initialize job delivery state",
            "branch": BRANCH,
            "content": base64.b64encode((json.dumps(content) + "\n").encode()).decode()
        }, "PUT")
        sha = response["content"]["sha"]
    else:
        if remote.get("encoding") != "base64":
            raise ServiceError("GitHub state: формат або розмір файлу не підтримується")
        content = json.loads(base64.b64decode(remote["content"]).decode("utf-8"))
        sha = remote["sha"]
    write_json(STATE, content)
    load_state(STATE)  # Пошкоджений стан ніколи не замінюємо порожнім.
    write_json(META, {"sha": sha, "original": content})
    print("Історію доставки завантажено.")


def push():
    content = load_state(STATE)
    meta = json.loads(META.read_text(encoding="utf-8"))
    if content == meta["original"]:
        print("Історія не змінилася.")
        return
    api("/contents/" + REMOTE_PATH, {
        "message": "Update job delivery state",
        "branch": BRANCH,
        "sha": meta["sha"],  # Optimistic concurrency: чужі зміни спричиняють конфлікт.
        "content": base64.b64encode(STATE.read_bytes()).decode()
    }, "PUT")
    print("Історію доставки збережено.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["pull", "push"])
    args = parser.parse_args()
    try:
        pull() if args.operation == "pull" else push()
    except (ServiceError, ValueError, KeyError, OSError) as exc:
        print(str(exc) if isinstance(exc, ServiceError) else "Помилка файлу state; доставку/збереження зупинено.")
        raise SystemExit(1)
