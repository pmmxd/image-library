from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import closing
from pathlib import Path

from library_db import connect, ensure_schema


BACKEND = "http://127.0.0.1:18766"
MANAGER = "http://127.0.0.1:7080"
DEFAULT_MANAGER_EXE = Path(os.environ.get("LOCALDOCK_EXE", "LocalDock.exe"))


def request_json(url: str, method: str = "GET", payload: dict | None = None, headers: dict | None = None, timeout: int = 15) -> dict:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_backend(seconds: int = 30) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            return request_json(f"{BACKEND}/api/status", timeout=2)
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"图片库后端无法启动：{BACKEND}")


def wait_manager(seconds: int = 30) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{MANAGER}/", timeout=2):
                return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"LocalDock 无法启动：{MANAGER}")


def catalog_ready(data_dir: Path) -> bool:
    if not (data_dir / "图片浏览.html").is_file():
        return False
    try:
        with closing(connect(data_dir / "图片索引.db")) as conn:
            ensure_schema(conn)
            return bool(
                conn.execute(
                    "SELECT 1 FROM media WHERE kind = 'image' LIMIT 1"
                ).fetchone()
            )
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.environ.get("MEDIA_LIBRARY_ROOT", ""))
    parser.add_argument("--localdock", default=str(DEFAULT_MANAGER_EXE))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    app_dir = Path(__file__).resolve().parent
    if args.root:
        library_root = Path(args.root).resolve()
    elif app_dir.parent.name == ".media-library":
        library_root = app_dir.parent.parent
    else:
        parser.error("please specify --root or set MEDIA_LIBRARY_ROOT")
    if not library_root.is_dir():
        parser.error(f"image library root does not exist: {library_root}")
    manager_exe = Path(args.localdock).expanduser()
    manager_token = manager_exe.parent / "data" / "manager.token"
    project_dir = library_root / ".media-library"
    data_dir = project_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["attrib", "+H", str(project_dir)],
            capture_output=True,
            check=False,
        )
    except OSError:
        pass

    backend_ok = False
    try:
        status = request_json(f"{BACKEND}/api/status", timeout=2)
        backend_ok = Path(status["root"]).resolve() == library_root
        if not backend_ok:
            request_json(f"{BACKEND}/api/shutdown", method="POST", payload={}, timeout=5)
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    request_json(f"{BACKEND}/api/status", timeout=1)
                    time.sleep(0.2)
                except Exception:
                    break
    except Exception:
        backend_ok = False

    if not backend_ok:
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        backend_env = os.environ.copy()
        backend_env["MEDIA_LIBRARY_ROOT"] = str(library_root)
        subprocess.Popen(
            [sys.executable, str(app_dir / "image_server.py")],
            cwd=app_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            env=backend_env,
        )
        status = wait_backend()
        if Path(status["root"]).resolve() != library_root:
            raise RuntimeError("图片库后端仍在运行另一个图片库")

    if not catalog_ready(data_dir):
        print("首次加载，正在扫描当前图片库...", flush=True)
        result = request_json(
            f"{BACKEND}/api/rebuild",
            method="POST",
            payload={"mode": "incremental"},
            timeout=900,
        )
        print(result.get("message", "索引已生成"), flush=True)

    try:
        wait_manager(1)
    except Exception:
        if not manager_exe.is_file():
            raise RuntimeError(f"未找到 LocalDock：{manager_exe}")
        subprocess.Popen([str(manager_exe)], cwd=str(manager_exe.parent))
        wait_manager(30)

    token = manager_token.read_text(encoding="utf-8").strip()
    headers = {"X-LocalDock-Token": token}
    state = request_json(f"{MANAGER}/api/state", headers=headers)
    service = next(
        (
            item for item in state.get("services", [])
            if item.get("root_path") and Path(item["root_path"]).resolve() == data_dir.resolve()
        ),
        None,
    )
    payload = {
        "name": f"图片库 - {library_root.name}",
        "root_path": str(data_dir),
        "entry_file": "图片浏览.html",
        "auto_open": False,
        "bridge_enabled": False,
        "bridge_root": "",
        "write_enabled": False,
        "watch_enabled": False,
        "watch_root": "",
        "watch_interval": 5,
        "auto_reload": False,
        "auto_command_id": "",
        "command_enabled": False,
        "commands_json": "[]",
    }
    if service:
        needs_update = (
            service.get("name") != payload["name"]
            or service.get("entry_file") != payload["entry_file"]
            or Path(service.get("root_path", "")).resolve() != data_dir.resolve()
        )
        if needs_update:
            if service.get("status") == "running":
                request_json(
                    f"{MANAGER}/api/services/{service['id']}/stop",
                    method="POST",
                    payload={},
                    headers=headers,
                )
            payload.update({"port_mode": "manual", "port": service["port"]})
            result = request_json(
                f"{MANAGER}/api/services/{service['id']}",
                method="PUT",
                payload=payload,
                headers=headers,
            )
        else:
            result = {"service": service}
    else:
        payload.update({"port_mode": "auto", "port": 0})
        result = request_json(
            f"{MANAGER}/api/services",
            method="POST",
            payload=payload,
            headers=headers,
        )
    service_id = result["service"]["id"]
    state = request_json(f"{MANAGER}/api/state", headers=headers)
    service = next(item for item in state["services"] if item["id"] == service_id)
    if service.get("status") != "running":
        try:
            request_json(
                f"{MANAGER}/api/services/{service_id}/start",
                method="POST",
                payload={},
                headers=headers,
            )
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                raise
        state = request_json(f"{MANAGER}/api/state", headers=headers)
        service = next(item for item in state["services"] if item["id"] == service_id)
        if service.get("status") != "running":
            raise RuntimeError("LocalDock 站点未能启动")
    site_url = service.get("url") or f"{MANAGER}/"
    print(f"图片库后端：{BACKEND}/library/")
    print(f"LocalDock 站点：{site_url}")
    if not args.no_browser and os.environ.get("MEDIA_LIBRARY_NO_BROWSER") != "1":
        webbrowser.open(site_url)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
