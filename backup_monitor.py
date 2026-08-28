#!/usr/bin/env python3
import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

BASE = Path(__file__).resolve().parent
APP_VERSION = "1.1.0"
GITHUB_REPO = "Dikomillo/ServerBackupMonitor"
UPDATE_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CONFIG_PATH = BASE / "config.json"
CONFIG_EXAMPLE_PATH = BASE / "config.example.json"
STATE_PATH = BASE / "state.json"
LOG_DIR = BASE / "logs"
AGENT_PATH = BASE / "remote_backup_agent.py"
CONFIG_DEFAULTS = {
    "backup_root": "backups",
    "status_interval_seconds": 3600,
    "backup_interval_seconds": 18000,
    "retention_count": 30,
    "lock_port": 47651,
    "check_updates": True,
}
SUPPORTED_COMPONENTS = {"x-ui", "stack", "site"}


class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    MAGENTA = "\033[95m"
    DIM = "\033[90m"


def enable_windows_ansi():
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass


class Logger:
    def __init__(self, quiet=False):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOG_DIR / f"monitor-{datetime.now():%Y-%m}.log"
        self.quiet = quiet
        self.events = deque(maxlen=20)

    def write(self, level, message, color=Color.RESET, event=True):
        now = datetime.now()
        line = f"[{now:%Y-%m-%d %H:%M:%S}] {level:<4} {message}"
        if event:
            self.events.append((now.strftime("%H:%M:%S"), level, message))
        if not self.quiet:
            encoding = sys.stdout.encoding or "utf-8"
            display = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
            print(f"{Color.CYAN}{display[:21]}{Color.RESET}{color}{display[21:]}{Color.RESET}", flush=True)
        with self.path.open("a", encoding="utf-8") as log:
            log.write(line + "\n")

    def ok(self, message, event=True):
        self.write("OK", message, Color.GREEN, event)

    def warn(self, message, event=True):
        self.write("WARN", message, Color.YELLOW, event)

    def error(self, message, event=True):
        self.write("FAIL", message, Color.RED, event)

    def info(self, message, event=True):
        self.write("INFO", message, Color.DIM, event)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def paint(text, color):
    return f"{color}{text}{Color.RESET}"


def visible_len(text):
    return len(ANSI_RE.sub("", text))


def cell(text, width):
    plain = ANSI_RE.sub("", text)
    if len(plain) > width:
        text = plain[:max(1, width - 1)] + "…"
    return text + " " * max(0, width - visible_len(text))


def countdown(target):
    if not target:
        return "—"
    left = max(0, int(target - time.monotonic()))
    return f"{left // 60:02d}:{left % 60:02d}"


def format_size(size):
    return f"{size / 1024:.0f} КБ" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} МБ"


def version_tuple(value):
    match = re.search(r"\d+(?:\.\d+){0,2}", str(value))
    if not match:
        return ()
    parts = [int(part) for part in match.group(0).split(".")]
    return tuple((parts + [0, 0, 0])[:3])


def latest_release():
    request = urllib.request.Request(
        UPDATE_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ServerBackupMonitor"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        release = json.load(response)
    version = version_tuple(release.get("tag_name") or release.get("name", ""))
    if not version or version <= version_tuple(APP_VERSION):
        return None
    return {
        "version": ".".join(str(part) for part in version),
        "url": release.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases",
    }


class Dashboard:
    def __init__(self, logger):
        self.log = logger

    def badge(self, ok, good, bad="OFFLINE"):
        return paint(f"● {good if ok else bad}", Color.GREEN if ok else Color.RED)

    def line(self, text=""):
        inner = cell(text, self.width - 4)
        return f"{paint('│', Color.DIM)} {inner} {paint('│', Color.DIM)}"

    def rule(self, left="├", right="┤"):
        return paint(left + "─" * (self.width - 2) + right, Color.DIM)

    def docker(self, rows):
        labels = {"nginx": "Nginx", "wg-easy": "WireGuard", "shadowbox": "Outline"}
        items = []
        found = {}
        for row in rows:
            name, _, status = row.partition("|")
            found[name] = status
        for name, label in labels.items():
            if name in found:
                items.append(self.badge(found[name].startswith("Up"), label, label))
        return "  ".join(items) or paint("—", Color.DIM)

    def render(self, monitor):
        self.width = max(78, min(118, shutil.get_terminal_size((108, 32)).columns))
        compact = self.width < 116
        now = datetime.now()
        internet_ok = monitor.internet_state[0]
        online_servers = sum(bool(row.get("ssh")) for row in monitor.status_results)
        total_servers = len(monitor.config["servers"])
        all_ok = internet_ok is True and online_servers == total_servers and all(
            row.get("remote", {}).get("x_ui") == "active"
            and 200 <= row.get("panel", 0) < 400
            and (
                "site" not in row["server"].get("components", [])
                or (200 <= row.get("site", 0) < 400 and row.get("remote", {}).get("nginx") == "active")
            )
            for row in monitor.status_results
        )
        overall = self.badge(all_ok, "ВСЁ РАБОТАЕТ", "ТРЕБУЕТ ВНИМАНИЯ")
        internet = self.badge(internet_ok is True, "ИНТЕРНЕТ ЕСТЬ", "НЕТ ИНТЕРНЕТА") if internet_ok is not None else paint("● ПРОВЕРКА", Color.YELLOW)
        out = ["\033[2J\033[H"]
        out.append(paint("╭" + "─" * (self.width - 2) + "╮", Color.CYAN))
        out.append(self.line(f"{paint('SERVER BACKUP MONITOR', Color.BOLD + Color.WHITE)}   {overall}"))
        out.append(self.line(f"{internet}   Серверы: {paint(f'{online_servers}/{total_servers}', Color.GREEN if online_servers == total_servers else Color.RED)}   Время: {now:%d.%m.%Y %H:%M:%S}"))
        out.append(self.line(f"Следующая проверка: {paint(countdown(monitor.next_status_at), Color.CYAN)}   Бэкапы: {paint(countdown(monitor.next_backup_at), Color.CYAN)}   {paint(monitor.phase, Color.YELLOW)}"))
        out.append(self.rule())
        out.append(self.line(paint("СЕРВЕРЫ", Color.BOLD + Color.CYAN)))
        if compact:
            out.append(self.line(cell(paint("Имя", Color.DIM), 9) + cell(paint("Адрес", Color.DIM), 17) + cell(paint("SSH", Color.DIM), 16) + cell(paint("X-UI", Color.DIM), 14) + paint("Панель", Color.DIM)))
        else:
            out.append(self.line(
                cell(paint("Имя", Color.DIM), 10) + cell(paint("Адрес", Color.DIM), 18) +
                cell(paint("SSH", Color.DIM), 18) + cell(paint("X-UI", Color.DIM), 15) +
                cell(paint("Панель", Color.DIM), 16) + paint("Сервисы", Color.DIM)
            ))
        result_by_name = {row["server"]["name"]: row for row in monitor.status_results}
        for server in monitor.config["servers"]:
            row = result_by_name.get(server["name"])
            if not row:
                ssh = xui = panel = paint("● ОЖИДАНИЕ", Color.YELLOW)
                services = paint("—", Color.DIM)
            else:
                ssh = self.badge(row["ssh"], f"ONLINE {row['latency']:.1f}с")
                xui_ok = row.get("remote", {}).get("x_ui") == "active"
                xui = self.badge(xui_ok, "ACTIVE", "STOPPED")
                code = row.get("panel", 0)
                panel = self.badge(200 <= code < 400, f"HTTP {code}", "НЕДОСТУПНА")
                services = self.docker(row.get("remote", {}).get("containers", [])) if "stack" in server.get("components", []) else paint("—", Color.DIM)
            if compact:
                out.append(self.line(cell(paint(server["name"], Color.BOLD + Color.WHITE), 9) + cell(server["host"], 17) + cell(ssh, 16) + cell(xui, 14) + panel))
                if "stack" in server.get("components", []):
                    out.append(self.line(f"  Сервисы: {services}"))
            else:
                out.append(self.line(
                    cell(paint(server["name"], Color.BOLD + Color.WHITE), 10) +
                    cell(server["host"], 18) + cell(ssh, 18) + cell(xui, 15) + cell(panel, 16) + services
                ))
            if "site" in server.get("components", []):
                site_code = row.get("site", 0) if row else 0
                site = self.badge(200 <= site_code < 400, f"HTTP {site_code}", "НЕДОСТУПЕН")
                nginx_ok = row and row.get("remote", {}).get("nginx") == "active"
                out.append(self.line(f"  Сайт: {site}   {self.badge(nginx_ok, 'Nginx ACTIVE', 'Nginx STOPPED')}"))
        out.append(self.rule())
        out.append(self.line(paint("РЕЗЕРВНЫЕ КОПИИ", Color.BOLD + Color.CYAN)))
        if compact:
            out.append(self.line(cell(paint("Компонент", Color.DIM), 20) + cell(paint("Состояние", Color.DIM), 24) + cell(paint("Отпечаток", Color.DIM), 14) + paint("Время", Color.DIM)))
        else:
            out.append(self.line(cell(paint("Компонент", Color.DIM), 24) + cell(paint("Состояние", Color.DIM), 30) + cell(paint("Отпечаток", Color.DIM), 16) + paint("Последняя проверка", Color.DIM)))
        for server in monitor.config["servers"]:
            for component in server["components"]:
                key = f"{server['name']}/{component}"
                item = monitor.backup_status.get(key, {})
                state = item.get("state", "waiting")
                text = item.get("text", "Ожидание")
                color = {"clean": Color.GREEN, "saved": Color.GREEN, "checking": Color.YELLOW, "error": Color.RED}.get(state, Color.DIM)
                fp = item.get("fingerprint") or monitor.state.get("fingerprints", {}).get(server["name"], {}).get(component, "")
                checked = item.get("checked", "—")
                if compact:
                    out.append(self.line(cell(paint(key, Color.WHITE), 20) + cell(paint("● " + text, color), 24) + cell(fp[:12] or "—", 14) + checked))
                else:
                    out.append(self.line(cell(paint(key, Color.WHITE), 24) + cell(paint("● " + text, color), 30) + cell(fp[:12] or "—", 16) + checked))
        out.append(self.rule())
        out.append(self.line(paint("ПОСЛЕДНИЕ СОБЫТИЯ", Color.BOLD + Color.CYAN)))
        events = list(self.log.events)[-5:]
        if not events:
            out.append(self.line(paint("Пока всё спокойно — важных событий нет", Color.DIM)))
        for stamp, level, message in events:
            color = {"OK": Color.GREEN, "WARN": Color.YELLOW, "FAIL": Color.RED}.get(level, Color.DIM)
            out.append(self.line(f"{paint(stamp, Color.DIM)}  {paint(level, color)}  {message}"))
        out.append(self.rule("╰", "╯"))
        out.append(paint("  Ctrl+C — выход   •   Подробный журнал: logs", Color.DIM))
        print("\n".join(out), end="", flush=True)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def local_path(value):
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return path if path.is_absolute() else BASE / path


def load_config(path=CONFIG_PATH):
    if not path.exists():
        if CONFIG_EXAMPLE_PATH.exists():
            shutil.copy2(CONFIG_EXAMPLE_PATH, path)
        raise ValueError(f"Создан {path.name}. Укажите свои серверы и запустите монитор снова.")
    raw = load_json(path, None)
    if not isinstance(raw, dict):
        raise ValueError("config.json должен содержать JSON-объект")
    config = {**CONFIG_DEFAULTS, **raw}
    for key in ("status_interval_seconds", "backup_interval_seconds", "retention_count", "lock_port"):
        try:
            config[key] = int(config[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} должен быть целым числом") from None
        if config[key] <= 0:
            raise ValueError(f"{key} должен быть больше нуля")
    if not isinstance(config["check_updates"], bool):
        raise ValueError("check_updates должен быть true или false")
    config["backup_root"] = str(local_path(config["backup_root"]))
    servers = config.get("servers")
    if not isinstance(servers, list) or not servers:
        raise ValueError("Добавьте хотя бы один сервер в servers")
    normalized, names, folders = [], set(), set()
    for index, item in enumerate(servers, 1):
        if not isinstance(item, dict):
            raise ValueError(f"servers[{index}] должен быть JSON-объектом")
        host = str(item.get("host", "")).strip()
        panel_url = str(item.get("panel_url", "")).strip()
        if not host or not panel_url:
            raise ValueError(f"servers[{index}]: обязательны host и panel_url")
        parsed_url = urllib.parse.urlparse(panel_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError(f"servers[{index}]: panel_url должен быть полным HTTP(S)-адресом")
        name = str(item.get("name") or host).strip()
        generated_folder = re.sub(r"[^A-Za-z0-9._-]+", "_", host).strip("._") or f"server-{index}"
        folder = str(item.get("folder") or generated_folder).strip()
        if folder in {".", ".."} or Path(folder).name != folder or re.sub(r"[^A-Za-z0-9._-]+", "_", folder) != folder:
            raise ValueError(f"{name}: folder может содержать только буквы, цифры, точку, _ и -")
        components = item.get("components", ["x-ui"])
        if not isinstance(components, list) or "x-ui" not in components or len(components) != len(set(components)) or not set(components) <= SUPPORTED_COMPONENTS:
            allowed = ", ".join(sorted(SUPPORTED_COMPONENTS))
            raise ValueError(f"{name}: components должен содержать x-ui и может дополнительно содержать {allowed}")
        site_url = str(item.get("site_url", "")).strip()
        site_root = str(item.get("site_root", "/var/www/example.com")).strip()
        if "site" in components:
            parsed_site_url = urllib.parse.urlparse(site_url)
            if parsed_site_url.scheme not in {"http", "https"} or not parsed_site_url.netloc:
                raise ValueError(f"{name}: для компонента site нужен полный site_url HTTP(S)")
            if not re.fullmatch(r"/[A-Za-z0-9._/-]+", site_root) or site_root == "/" or ".." in site_root.split("/"):
                raise ValueError(f"{name}: site_root должен быть безопасным абсолютным Linux-путём")
        if name in names or folder in folders:
            raise ValueError(f"Дублируется имя или папка сервера: {name}")
        names.add(name)
        folders.add(folder)
        server = {**item, "name": name, "host": host, "panel_url": panel_url, "site_url": site_url, "site_root": site_root, "folder": folder, "user": item.get("user") or "root", "components": components}
        if server.get("key"):
            server["key"] = str(local_path(server["key"]))
        normalized.append(server)
    config["servers"] = normalized
    if config["lock_port"] > 65535:
        raise ValueError("lock_port должен быть не больше 65535")
    return config


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def run(command, timeout=60):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        creationflags=flags,
    )


def parse_json_output(text):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError("remote command did not return JSON")


def duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} сек"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"
    return f"{seconds // 86400} д {seconds % 86400 // 3600} ч"


class Monitor:
    def __init__(self, config, logger):
        self.config = config
        self.log = logger
        self.state = load_json(STATE_PATH, {"fingerprints": {}, "health": {}})
        self.uploaded = set()
        self.agent_hash = hashlib.sha256(AGENT_PATH.read_bytes()).hexdigest()
        self.remote_agent = f"/tmp/server-backup-agent-{self.agent_hash[:12]}.py"
        self.dashboard = None
        self.status_results = []
        self.backup_status = {}
        self.internet_state = (None, "")
        self.phase = "Запуск…"
        self.next_status_at = None
        self.next_backup_at = None
        self.last_status_at = None
        self.last_backup_at = None

    def refresh(self):
        if self.dashboard:
            self.dashboard.render(self)

    def target(self, server):
        return f"{server['user']}@{server['host']}"

    def ssh_base(self, server):
        args = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=yes", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=1",
        ]
        if server.get("key"):
            args += ["-i", server["key"]]
        return args + [self.target(server)]

    def scp_base(self, server):
        args = ["scp", "-O", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes"]
        if server.get("key"):
            args += ["-i", server["key"]]
        return args

    def ensure_agent(self, server):
        name = server["name"]
        if name in self.uploaded:
            return
        check = run(self.ssh_base(server) + [f"test -f {self.remote_agent}"], timeout=15)
        if check.returncode != 0:
            upload = run(self.scp_base(server) + [str(AGENT_PATH), f"{self.target(server)}:{self.remote_agent}"], timeout=30)
            if upload.returncode != 0:
                raise RuntimeError(upload.stderr.strip() or "cannot upload remote agent")
        self.uploaded.add(name)

    def remote_json(self, server, *args, timeout=90):
        self.ensure_agent(server)
        command = self.ssh_base(server) + ["python3", self.remote_agent, *args]
        result = run(command, timeout=timeout)
        if result.returncode != 0 and "can't open file" in result.stderr:
            self.uploaded.discard(server["name"])
            self.ensure_agent(server)
            result = run(command, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"ssh exit {result.returncode}")
        return parse_json_output(result.stdout)

    def internet(self):
        try:
            socket.getaddrinfo("github.com", 443)
            with socket.create_connection(("1.1.1.1", 443), timeout=4):
                return True, ""
        except OSError as exc:
            return False, str(exc)

    def panel_status(self, url):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        request = urllib.request.Request(url, headers={"User-Agent": "ServerBackupMonitor/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=8, context=context) as response:
                return response.status, ""
        except urllib.error.HTTPError as exc:
            return exc.code, str(exc)
        except Exception as exc:
            return 0, str(exc)

    def server_status(self, server):
        started = time.monotonic()
        remote, ssh_ok, error = {}, False, ""
        for attempt in range(2):
            try:
                remote = self.remote_json(server, "status", timeout=12)
                ssh_ok, error = True, ""
                break
            except Exception as exc:
                error = str(exc)
                if attempt == 0:
                    time.sleep(1)
        panel_code, panel_error = self.panel_status(server["panel_url"])
        site_code, site_error = (self.panel_status(server["site_url"]) if "site" in server.get("components", []) else (None, ""))
        return {
            "server": server,
            "ssh": ssh_ok,
            "ssh_error": error,
            "panel": panel_code,
            "panel_error": panel_error,
            "site": site_code,
            "site_error": site_error,
            "remote": remote,
            "latency": time.monotonic() - started,
        }

    def record_health(self, key, online, error=""):
        now = time.time()
        health = self.state.setdefault("health", {})
        old = health.get(key)
        if old is None:
            health[key] = {"online": online, "changed_at": now, "last_ok": now if online else None, "error": error}
            return None
        transition = None
        if bool(old.get("online")) != online:
            transition = (old.get("online"), now - old.get("changed_at", now))
            old["online"] = online
            old["changed_at"] = now
        if online:
            old["last_ok"] = now
            old["error"] = ""
        else:
            old["error"] = error
        return transition

    def show_status(self, result):
        server = result["server"]
        name = server["name"]
        ssh_transition = self.record_health(f"{name}:ssh", result["ssh"], result["ssh_error"])
        panel_ok = 200 <= result["panel"] < 400
        panel_transition = self.record_health(f"{name}:panel", panel_ok, result["panel_error"])
        site_enabled = "site" in server.get("components", [])
        site_ok = not site_enabled or 200 <= result["site"] < 400
        site_transition = self.record_health(f"{name}:site", site_ok, result["site_error"]) if site_enabled else None
        nginx_ok = not site_enabled or result["remote"].get("nginx") == "active"
        if ssh_transition:
            was_online, elapsed = ssh_transition
            if was_online:
                self.log.error(f"{name}: SSH стал недоступен; до этого был доступен {duration(elapsed)}")
            else:
                self.log.ok(f"{name}: SSH снова доступен; простой {duration(elapsed)}")
        if panel_transition:
            was_online, elapsed = panel_transition
            if was_online:
                self.log.error(f"{name}: панель стала недоступна; до этого работала {duration(elapsed)}")
            else:
                self.log.ok(f"{name}: панель снова доступна; простой {duration(elapsed)}")
        if site_transition:
            was_online, elapsed = site_transition
            if was_online:
                self.log.error(f"{name}: сайт стал недоступен; до этого работал {duration(elapsed)}")
            else:
                self.log.ok(f"{name}: сайт снова доступен; простой {duration(elapsed)}")

        if not result["ssh"]:
            self.log.error(f"{name}: SSH недоступен — {result['ssh_error']}", event=not bool(ssh_transition))
            return
        remote = result["remote"]
        xui = remote.get("x_ui", "unknown")
        containers = ", ".join(remote.get("containers", [])) or "нет/не проверяются"
        panel = result["panel"] or "DOWN"
        message = f"{name}: SSH {result['latency']:.1f}с | x-ui {xui} | панель {panel}"
        if site_enabled:
            message += f" | сайт {result['site'] or 'DOWN'} | nginx {remote.get('nginx', 'unknown')}"
        if "stack" in server.get("components", []):
            message += f" | Docker: {containers}"
        if xui == "active" and panel_ok and site_ok and nginx_ok:
            self.log.ok(message, event=False)
        else:
            self.log.warn(message, event=not bool(ssh_transition or panel_transition or site_transition))

    def monitor_once(self):
        self.phase = "Проверяю интернет…"
        self.refresh()
        online, error = self.internet()
        self.internet_state = (online, error)
        transition = self.record_health("internet", online, error)
        if transition:
            was_online, elapsed = transition
            if was_online:
                self.log.error(f"Интернет пропал; до этого работал {duration(elapsed)}")
            else:
                self.log.ok(f"Интернет восстановлен; простой {duration(elapsed)}")
        if online:
            self.log.ok("Интернет доступен", event=False)
        else:
            self.log.error(f"Интернет недоступен — {error}", event=not bool(transition))
        self.status_results = []
        for server in self.config["servers"]:
            self.phase = f"Проверяю {server['name']}…"
            self.refresh()
            result = self.server_status(server)
            self.status_results.append(result)
            self.show_status(result)
            self.refresh()
        self.phase = ""
        self.last_status_at = datetime.now()
        save_json(STATE_PATH, self.state)
        self.refresh()

    def remote_manifest(self, server, component):
        args = ["manifest", component]
        if component == "site":
            args += ["--site-root", server["site_root"]]
        return self.remote_json(server, *args, timeout=120)["fingerprint"]

    def remove_remote_archive(self, server, remote_path):
        if not remote_path.startswith("/tmp/") or not remote_path.endswith(".tar.gz"):
            return
        run(self.ssh_base(server) + [f"rm -f -- '{remote_path}'"], timeout=20)

    def download_backup(self, server, component, destination):
        args = ["backup", component]
        if component == "site":
            args += ["--site-root", server["site_root"]]
        result = self.remote_json(server, *args, timeout=600)
        remote_path = result["archive"]
        destination.mkdir(parents=True, exist_ok=True)
        archive = destination / Path(remote_path).name
        try:
            copied = run(self.scp_base(server) + [f"{self.target(server)}:{remote_path}", str(archive)], timeout=900)
            if copied.returncode != 0:
                raise RuntimeError(copied.stderr.strip() or "scp failed")
            local_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
            if local_hash != result["sha256"]:
                raise RuntimeError("SHA-256 downloaded archive does not match server")
            save_json(destination / "metadata.json", {**result, "saved_at": datetime.now().isoformat()})
            return result
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        finally:
            self.remove_remote_archive(server, remote_path)

    def archive_root(self):
        return Path(self.config["backup_root"]) / "auto-backups"

    def cache_dir(self, server, component):
        return self.archive_root() / ".latest" / server["folder"] / component

    def seed_cache(self, server, component):
        cache = self.cache_dir(server, component)
        if next(cache.glob("*.tar.gz"), None):
            return cache
        old_index = Path(self.config["backup_root"]) / server["folder"] / component / "auto-backups" / "latest.json"
        old = load_json(old_index, {})
        old_path = old.get("local_path")
        old_dir = Path(old_path) if old_path else None
        source = next(old_dir.glob("*.tar.gz"), None) if old_dir and old_dir.is_dir() else None
        if source:
            cache.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, cache / source.name)
            metadata = old_dir / "metadata.json"
            if metadata.exists():
                shutil.copy2(metadata, cache / metadata.name)
        return cache

    def update_cache(self, server, component, source):
        cache = self.cache_dir(server, component)
        cache.mkdir(parents=True, exist_ok=True)
        for old in cache.iterdir():
            if old.is_file():
                old.unlink()
        for item in source.iterdir():
            if item.is_file():
                shutil.copy2(item, cache / item.name)

    def prune_archives(self):
        keep = self.config["retention_count"]
        archives = sorted(self.archive_root().glob("server-backup-*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
        for old in archives[keep:]:
            old.unlink()

    def check_backups(self, baseline=False, force=False):
        action = "Инициализация умного диффа" if baseline else "Проверка изменений для резервного копирования"
        self.log.info(action, event=False)
        fingerprints = self.state.setdefault("fingerprints", {})
        archive_root = self.archive_root()
        archive_root.mkdir(parents=True, exist_ok=True)
        staging = None if baseline else Path(tempfile.mkdtemp(prefix=".partial-", dir=archive_root))
        records, pending_fingerprints = [], {}
        saved_any = bool(staging and not next(archive_root.glob("server-backup-*.zip"), None))
        had_error, partial_zip = False, None
        if saved_any:
            self.log.info("Создаю первую общую точку восстановления из последних локальных копий")
        try:
            for server in self.config["servers"]:
                for component in server["components"]:
                    label = f"{server['name']}/{component}"
                    destination = staging / server["folder"] / component if staging else None
                    self.phase = f"Проверяю бэкап {label}…"
                    self.backup_status[label] = {"state": "checking", "text": "Проверка…", "checked": datetime.now().strftime("%H:%M:%S")}
                    self.refresh()
                    try:
                        current = self.remote_manifest(server, component)
                        previous = fingerprints.setdefault(server["name"], {}).get(component)
                        if baseline:
                            fingerprints[server["name"]][component] = current
                            self.backup_status[label] = {"state": "clean", "text": "Baseline сохранён", "fingerprint": current, "checked": datetime.now().strftime("%H:%M:%S")}
                            self.log.ok(f"{label}: baseline {current[:12]}, скачивание не требуется", event=False)
                            records.append({"component": label, "status": "baseline", "fingerprint": current})
                        elif previous == current and not force:
                            self.backup_status[label] = {"state": "clean", "text": "Без изменений", "fingerprint": current, "checked": datetime.now().strftime("%H:%M:%S")}
                            self.log.info(f"{label}: изменений нет ({current[:12]}), пропуск", event=False)
                            records.append({"component": label, "status": "cached", "fingerprint": current})
                        else:
                            reason = "ручной полный бэкап" if force else ("первый бэкап" if not previous else f"найдены изменения {previous[:8]} → {current[:8]}")
                            self.backup_status[label] = {"state": "checking", "text": "Скачиваю…", "fingerprint": current, "checked": datetime.now().strftime("%H:%M:%S")}
                            self.refresh()
                            self.log.info(f"{label}: {reason}; скачиваю компонент")
                            result = self.download_backup(server, component, destination)
                            self.update_cache(server, component, destination)
                            pending_fingerprints[(server["name"], component)] = result["fingerprint"]
                            saved_any = True
                            self.backup_status[label] = {"state": "saved", "text": f"Обновлено {format_size(result['size'])}", "fingerprint": result["fingerprint"], "checked": datetime.now().strftime("%H:%M:%S")}
                            self.log.ok(f"{label}: скачано {format_size(result['size'])}")
                            records.append({"component": label, "status": "downloaded", "fingerprint": result["fingerprint"], "size": result["size"]})
                    except Exception as exc:
                        had_error = True
                        self.backup_status[label] = {"state": "error", "text": "Ошибка бэкапа", "checked": datetime.now().strftime("%H:%M:%S")}
                        self.log.error(f"{label}: бэкап не выполнен — {exc}")
                        records.append({"component": label, "status": "error", "error": str(exc)})
                    save_json(STATE_PATH, self.state)
                    self.refresh()

            if staging and saved_any and not had_error:
                self.phase = "Собираю полную точку восстановления…"
                self.refresh()
                for server in self.config["servers"]:
                    for component in server["components"]:
                        destination = staging / server["folder"] / component
                        if next(destination.glob("*.tar.gz"), None):
                            continue
                        cache = self.seed_cache(server, component)
                        if not next(cache.glob("*.tar.gz"), None):
                            self.log.info(f"{server['name']}/{component}: локальной копии ещё нет; скачиваю для полного архива")
                            result = self.download_backup(server, component, destination)
                            self.update_cache(server, component, destination)
                            pending_fingerprints[(server["name"], component)] = result["fingerprint"]
                        else:
                            shutil.copytree(cache, destination, dirs_exist_ok=True)
                save_json(staging / "manifest.json", {
                    "created_at": datetime.now().isoformat(),
                    "mode": "full" if force else "smart",
                    "components": records,
                })
                stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                final = archive_root / f"server-backup-{stamp}.zip"
                suffix = 1
                while final.exists():
                    final = archive_root / f"server-backup-{stamp}-{suffix}.zip"
                    suffix += 1
                partial_zip = Path(shutil.make_archive(str(archive_root / f".partial-{stamp}"), "zip", staging))
                with zipfile.ZipFile(partial_zip) as bundle:
                    broken = bundle.testzip()
                    if broken:
                        raise RuntimeError(f"повреждён файл внутри ZIP: {broken}")
                os.replace(partial_zip, final)
                partial_zip = None
                for (server_name, component), fingerprint in pending_fingerprints.items():
                    fingerprints.setdefault(server_name, {})[component] = fingerprint
                self.prune_archives()
                self.log.ok(f"Точка восстановления сохранена: {final.name} ({format_size(final.stat().st_size)})")
            elif staging and had_error:
                self.log.error("Точка восстановления не создана: не все компоненты удалось проверить")
            elif staging:
                self.log.info("Изменений нет — новый ZIP не создавался", event=False)
        except Exception as exc:
            self.log.error(f"Точка восстановления не создана — {exc}")
        finally:
            if partial_zip:
                partial_zip.unlink(missing_ok=True)
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
        self.phase = ""
        self.last_backup_at = datetime.now()
        save_json(STATE_PATH, self.state)
        self.refresh()


class BackupGUI:
    BG = "#090E17"
    CARD = "#111925"
    CARD_ALT = "#182231"
    BORDER = "#243247"
    TEXT = "#F2F5F9"
    MUTED = "#8996A9"
    GREEN = "#43D79E"
    YELLOW = "#F4BE5B"
    RED = "#F06F82"
    BLUE = "#70A7FF"

    def __init__(self, config, logger, lock):
        self.config = config
        self.log = logger
        self.lock = lock
        self.monitor = Monitor(config, logger)
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.status_event = threading.Event()
        self.backup_event = threading.Event()
        self.force_event = threading.Event()
        self.event_snapshot = None
        self.events_initialized = False
        self.last_notified_event = None
        self.update_info = None
        self.tray_icon = None
        self.tray_ready = False
        self.tray_error = ""
        self.ip_hidden = True
        self.ip_vars = {}
        self.ip_buttons = {}
        self.root = tk.Tk()
        self.root.title("Server Backup Monitor")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(1180, max(960, screen_width - 80))
        height = min(790, max(600, screen_height - 100))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(960, 600)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.build_styles()
        self.build_ui()
        self.setup_tray()
        self.worker = threading.Thread(target=self.worker_loop, name="backup-worker", daemon=True)
        self.worker.start()
        self.root.after(500, self.refresh_ui)
        self.root.after(100, self.check_for_updates)

    def build_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TButton", background=self.CARD_ALT, foreground=self.TEXT, borderwidth=0, padding=(9, 6), font=("Segoe UI", 9))
        style.map("TButton", background=[("active", "#25364D")])
        style.configure("Primary.TButton", background=self.BLUE, foreground="#08101B", padding=(11, 7), font=("Segoe UI", 9, "bold"))
        style.map("Primary.TButton", background=[("active", "#94BEFF")])
        style.configure("Small.TButton", background=self.CARD_ALT, foreground=self.MUTED, padding=(6, 3), font=("Segoe UI", 8))
        style.map("Small.TButton", foreground=[("active", self.TEXT)], background=[("active", "#25364D")])
        style.configure("Treeview", background=self.CARD, fieldbackground=self.CARD, foreground=self.TEXT, rowheight=31, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=self.CARD_ALT, foreground=self.MUTED, relief="flat", padding=(7, 7), font=("Segoe UI", 8, "bold"))
        style.map("Treeview", background=[("selected", "#21436b")], foreground=[("selected", self.TEXT)])

    def label(self, parent, text="", size=10, color=None, bold=False, textvariable=None):
        return tk.Label(parent, text=text, textvariable=textvariable, bg=parent.cget("bg"), fg=color or self.TEXT, font=("Segoe UI", size, "bold" if bold else "normal"), anchor="w")

    def build_ui(self):
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)
        header = tk.Frame(root, bg=self.CARD, highlightthickness=1, highlightbackground=self.BORDER)
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 12))
        header.grid_columnconfigure(1, weight=1)
        self.overall_var = tk.StringVar(value="Проверяем систему…")
        self.internet_var = tk.StringVar(value="Интернет: проверка")
        self.next_var = tk.StringVar()
        brand = tk.Frame(header, bg=self.CARD)
        brand.grid(row=0, column=0, sticky="w", padx=16, pady=(13, 8))
        tk.Label(brand, text="SB", bg=self.BLUE, fg="#08101B", font=("Segoe UI", 11, "bold"), width=3, height=2).pack(side="left", padx=(0, 10))
        brand_text = tk.Frame(brand, bg=self.CARD)
        brand_text.pack(side="left")
        self.label(brand_text, "Server Backup Monitor", 16, self.TEXT, True).pack(anchor="w")
        self.label(brand_text, "Серверы и резервные копии", 9, self.MUTED).pack(anchor="w", pady=(1, 0))
        right = tk.Frame(header, bg=self.CARD)
        right.grid(row=0, column=1, sticky="e", padx=16, pady=(13, 8))
        self.overall_label = self.label(right, textvariable=self.overall_var, size=10, color=self.YELLOW, bold=True)
        self.overall_label.pack(anchor="e")
        self.label(right, textvariable=self.internet_var, size=9, color=self.MUTED).pack(anchor="e", pady=(2, 0))
        self.label(right, f"Версия v{APP_VERSION}", 8, self.MUTED).pack(anchor="e", pady=(3, 0))
        self.update_button = ttk.Button(right, text="Проверка обновлений…", style="Small.TButton", state="disabled", command=self.open_update)
        self.update_button.pack(anchor="e", pady=(3, 0))

        controls = tk.Frame(header, bg=self.CARD_ALT)
        controls.grid(row=1, column=0, columnspan=2, sticky="ew")
        controls.grid_columnconfigure(0, weight=1)
        self.label(controls, textvariable=self.next_var, size=9, color=self.MUTED).grid(row=0, column=0, sticky="w", padx=16, pady=9)
        ttk.Button(controls, text="Проверить серверы", command=self.request_check).grid(row=0, column=1, padx=(6, 0), pady=7)
        ttk.Button(controls, text="Сохранить изменения", style="Primary.TButton", command=self.request_smart_backup).grid(row=0, column=2, padx=(6, 0), pady=7)
        ttk.Button(controls, text="Создать все копии", command=self.request_force).grid(row=0, column=3, padx=(6, 0), pady=7)
        ttk.Button(controls, text="Архивы", command=lambda: os.startfile(self.monitor.archive_root())).grid(row=0, column=4, padx=(6, 0), pady=7)
        ttk.Button(controls, text="Настройки", command=lambda: os.startfile(str(CONFIG_PATH))).grid(row=0, column=5, padx=(6, 0), pady=7)
        ttk.Button(controls, text="В трей", style="Small.TButton", command=self.minimize_to_tray).grid(row=0, column=6, padx=(6, 16), pady=7)

        body_host = tk.Frame(root, bg=self.BG)
        body_host.grid(row=1, column=0, sticky="nsew")
        body_host.grid_columnconfigure(0, weight=1)
        body_host.grid_rowconfigure(0, weight=1)
        self.body_canvas = tk.Canvas(body_host, bg=self.BG, highlightthickness=0)
        self.body_canvas.grid(row=0, column=0, sticky="nsew")
        body_scroll = ttk.Scrollbar(body_host, orient="vertical", command=self.body_canvas.yview)
        body_scroll.grid(row=0, column=1, sticky="ns")
        self.body_canvas.configure(yscrollcommand=body_scroll.set)
        body = tk.Frame(self.body_canvas, bg=self.BG, padx=20)
        body_window = self.body_canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _event: self.body_canvas.configure(scrollregion=self.body_canvas.bbox("all")))
        self.body_canvas.bind("<Configure>", lambda event: self.body_canvas.itemconfigure(body_window, width=event.width))
        self.body_canvas.bind_all("<MouseWheel>", lambda event: self.body_canvas.yview_scroll(-int(event.delta / 120), "units"))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(2, weight=1)
        cards_host = tk.Frame(body, bg=self.BG)
        cards_host.grid(row=0, column=0, sticky="ew")
        self.cards_canvas = tk.Canvas(cards_host, bg=self.BG, height=196, highlightthickness=0)
        self.cards_canvas.pack(fill="x")
        self.cards_frame = tk.Frame(self.cards_canvas, bg=self.BG)
        cards_window = self.cards_canvas.create_window((0, 0), window=self.cards_frame, anchor="nw")
        self.cards_frame.bind("<Configure>", self.resize_cards)
        self.cards_canvas.bind("<Configure>", lambda event: (self.cards_canvas.itemconfigure(cards_window, width=event.width), self.reflow_cards(event)))
        self.cards = {}
        self.card_widgets = []
        for index, server in enumerate(self.config["servers"]):
            card = tk.Frame(self.cards_frame, bg=self.CARD, highlightthickness=1, highlightbackground=self.BORDER)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 6, 0))
            self.card_widgets.append(card)
            card.grid_columnconfigure(1, weight=1)
            self.label(card, server["name"], 12, self.TEXT, True).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 2))
            host_frame = tk.Frame(card, bg=self.CARD)
            host_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=14)
            host_frame.grid_columnconfigure(0, weight=1)
            host_var = tk.StringVar(value="•••.•••.•••.•••")
            self.ip_vars[server["name"]] = (host_var, server["host"])
            self.label(host_frame, textvariable=host_var, size=9, color=self.MUTED).grid(row=0, column=0, sticky="w")
            ttk.Button(host_frame, text="Копировать", style="Small.TButton", command=lambda host=server["host"]: self.copy_to_clipboard(host)).grid(row=0, column=1, padx=(5, 0))
            ip_button = ttk.Button(host_frame, text="Показать", style="Small.TButton", command=lambda name=server["name"]: self.toggle_ip(name))
            ip_button.grid(row=0, column=2, padx=(4, 0))
            self.ip_buttons[server["name"]] = ip_button
            tk.Frame(card, bg=self.BORDER, height=1).grid(row=2, column=0, columnspan=2, sticky="ew", padx=14, pady=(9, 5))
            values = {}
            rows = (("ssh", "SSH"), ("xui", "X-UI"), ("panel", "ПАНЕЛЬ"), ("site", "САЙТ"), ("services", "СЕРВИСЫ"))
            for row_index, (key, caption) in enumerate(rows, start=3):
                self.label(card, caption, 8, self.MUTED, True).grid(row=row_index, column=0, sticky="w", padx=(14, 10), pady=3)
                values[key] = self.label(card, "Ожидание", 9, self.YELLOW)
                values[key].grid(row=row_index, column=1, sticky="w", padx=(0, 14), pady=3)
            card.grid_rowconfigure(8, minsize=9)
            self.cards[server["name"]] = values

        backups = tk.Frame(body, bg=self.CARD, highlightthickness=1, highlightbackground=self.BORDER)
        backups.grid(row=1, column=0, sticky="ew", pady=(10, 10))
        backup_header = tk.Frame(backups, bg=self.CARD)
        backup_header.pack(fill="x", padx=14, pady=(10, 7))
        self.label(backup_header, "Резервные копии", 11, self.TEXT, True).pack(side="left")
        self.label(backup_header, "Архив создаётся только при изменении конфигурации", 8, self.MUTED).pack(side="right")
        tree_host = tk.Frame(backups, bg=self.CARD)
        tree_host.pack(fill="x", padx=10, pady=(0, 10))
        tree_host.grid_columnconfigure(0, weight=1)
        total_components = sum(len(server["components"]) for server in self.config["servers"])
        self.backup_tree = ttk.Treeview(tree_host, columns=("component", "state", "fingerprint", "checked"), show="headings", height=min(max(total_components, 4), 8))
        for column, heading, width in (("component", "Компонент", 250), ("state", "Состояние", 260), ("fingerprint", "Отпечаток", 150), ("checked", "Проверено", 100)):
            self.backup_tree.heading(column, text=heading)
            self.backup_tree.column(column, width=width, anchor="w")
        self.backup_tree.tag_configure("clean", foreground=self.GREEN)
        self.backup_tree.tag_configure("checking", foreground=self.YELLOW)
        self.backup_tree.tag_configure("error", foreground=self.RED)
        self.backup_tree.grid(row=0, column=0, sticky="ew")
        tree_scroll = ttk.Scrollbar(tree_host, orient="vertical", command=self.backup_tree.yview)
        self.backup_tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.grid(row=0, column=1, sticky="ns")

        events = tk.Frame(body, bg=self.CARD, highlightthickness=1, highlightbackground=self.BORDER)
        events.grid(row=2, column=0, sticky="nsew")
        event_controls = tk.Frame(events, bg=self.CARD)
        event_controls.pack(fill="x", padx=14, pady=(10, 5))
        self.label(event_controls, "События", 11, self.TEXT, True).pack(side="left")
        ttk.Button(event_controls, text="Копировать всё", style="Small.TButton", command=self.copy_all_events).pack(side="right")
        ttk.Button(event_controls, text="Копировать выделенное", style="Small.TButton", command=self.copy_selected_event).pack(side="right", padx=(0, 5))
        self.events_text = tk.Text(events, height=7, bg=self.CARD, fg=self.MUTED, selectbackground="#29486F", selectforeground=self.TEXT, insertbackground=self.TEXT, relief="flat", borderwidth=0, font=("Cascadia Mono", 9), state="disabled", padx=14, pady=5, wrap="none")
        self.events_text.tag_configure("ok", foreground=self.GREEN)
        self.events_text.tag_configure("warn", foreground=self.YELLOW)
        self.events_text.tag_configure("fail", foreground=self.RED)
        event_scroll = ttk.Scrollbar(events, orient="vertical", command=self.events_text.yview)
        self.events_text.configure(yscrollcommand=event_scroll.set)
        self.events_text.pack(side="left", fill="both", expand=True)
        event_scroll.pack(side="right", fill="y", padx=(0, 8), pady=(0, 8))
        self.event_menu = tk.Menu(root, tearoff=0, bg=self.CARD_ALT, fg=self.TEXT)
        self.event_menu.add_command(label="Копировать выбранное", command=self.copy_selected_event)
        self.event_menu.add_command(label="Копировать всё", command=self.copy_all_events)
        self.events_text.bind("<Button-3>", self.show_event_menu)
        self.events_text.bind("<Control-c>", self.copy_event_key)

        footer = tk.Frame(root, bg=self.BG)
        footer.grid(row=2, column=0, sticky="ew", padx=20, pady=(8, 12))
        self.label(footer, f"Архивы: {self.monitor.archive_root()}", 8, self.MUTED).pack(anchor="w")
        footer_text = f"Проверка — {duration(self.config['status_interval_seconds'])}    ·    дифф — {duration(self.config['backup_interval_seconds'])}    ·    хранение — {self.config['retention_count']} архивов"
        self.label(footer, footer_text, 8, self.MUTED).pack(anchor="w", pady=(2, 0))

    def request_check(self):
        self.status_event.set()
        self.wake_event.set()

    def request_smart_backup(self):
        self.backup_event.set()
        self.wake_event.set()

    def request_force(self):
        if messagebox.askyesno("Создать все копии?", "Будут заново созданы архивы всех серверов, даже если изменений нет."):
            self.force_event.set()
            self.wake_event.set()

    def copy_to_clipboard(self, text):
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            self.next_var.set("Скопировано в буфер обмена")

    def copy_selected_event(self):
        try:
            text = self.events_text.get("sel.first", "sel.last")
        except tk.TclError:
            text = self.events_text.get("1.0", "end-1c")
        self.copy_to_clipboard(text)

    def copy_all_events(self):
        self.copy_to_clipboard(self.events_text.get("1.0", "end-1c"))

    def copy_event_key(self, _event):
        self.copy_selected_event()
        return "break"

    def show_event_menu(self, event):
        self.event_menu.tk_popup(event.x_root, event.y_root)

    def setup_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw

            image = Image.new("RGB", (64, 64), self.BG)
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((2, 2, 62, 62), radius=13, fill=self.BLUE)
            draw.text((15, 19), "SB", fill="#08101B")
            menu = pystray.Menu(
                pystray.MenuItem("Открыть", lambda icon, item: self.root.after(0, self.restore_from_tray)),
                pystray.MenuItem("Свернуть в трей", lambda icon, item: self.root.after(0, self.minimize_to_tray)),
                pystray.MenuItem("Выход", lambda icon, item: self.root.after(0, self.close)),
            )
            self.tray_icon = pystray.Icon("ServerBackupMonitor", image, "Server Backup Monitor", menu)
            threading.Thread(target=self.tray_icon.run, name="tray", daemon=True).start()
            self.tray_ready = True
        except ImportError as exc:
            self.tray_error = str(exc)

    def minimize_to_tray(self):
        if self.tray_ready:
            self.root.withdraw()
            self.next_var.set("Монитор работает в трее")
        else:
            self.root.iconify()
            if self.tray_error:
                messagebox.showinfo("Трей недоступен", "Установите зависимости командой:\npython -m pip install -r requirements.txt")

    def restore_from_tray(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def check_for_updates(self):
        if not self.config["check_updates"]:
            self.update_button.configure(text="Проверка отключена")
            return
        threading.Thread(target=self.update_worker, name="update-check", daemon=True).start()

    def update_worker(self):
        try:
            release = latest_release()
        except (OSError, ValueError, urllib.error.URLError):
            return
        if release:
            self.root.after(0, lambda: self.show_update(release))
        else:
            self.root.after(0, lambda: self.update_button.configure(text="Актуальная версия", state="disabled"))

    def show_update(self, release):
        self.update_info = release
        self.update_button.configure(text=f"Доступна v{release['version']}", state="normal")
        self.notify("Доступна новая версия программы", "Обновление")

    def open_update(self):
        if self.update_info:
            webbrowser.open(self.update_info["url"])

    def notify(self, message, title="Server Backup Monitor"):
        try:
            if self.tray_ready and self.tray_icon:
                self.tray_icon.notify(message, title)
            else:
                self.root.bell()
        except Exception:
            self.root.bell()

    def notify_event(self, event):
        stamp, level, message = event
        if level in {"WARN", "FAIL"} or "Точка восстановления сохранена" in message:
            self.notify(message, f"Server Backup Monitor · {level}")

    def toggle_ip(self, name):
        self.ip_hidden = not self.ip_hidden
        for server_name, (var, host) in self.ip_vars.items():
            var.set("•••.•••.•••.•••" if self.ip_hidden else host)
            self.ip_buttons[server_name].configure(text="Показать" if self.ip_hidden else "Скрыть")

    def worker_loop(self):
        status_interval = self.config["status_interval_seconds"]
        backup_interval = self.config["backup_interval_seconds"]
        try:
            self.monitor.monitor_once()
            self.monitor.check_backups()
        except Exception as exc:
            self.log.error(f"GUI worker: {exc}")
        next_status = time.monotonic() + status_interval
        next_backup = time.monotonic() + backup_interval
        self.monitor.next_status_at = next_status
        self.monitor.next_backup_at = next_backup
        while not self.stop_event.is_set():
            wait = max(0.5, min(5, next_status - time.monotonic(), next_backup - time.monotonic()))
            self.wake_event.wait(wait)
            self.wake_event.clear()
            if self.stop_event.is_set():
                return
            force = self.force_event.is_set()
            self.force_event.clear()
            status = self.status_event.is_set()
            self.status_event.clear()
            backup = self.backup_event.is_set()
            self.backup_event.clear()
            now = time.monotonic()
            if status or force or now >= next_status:
                try:
                    self.monitor.monitor_once()
                except Exception as exc:
                    self.log.error(f"Проверка серверов: {exc}")
                next_status = time.monotonic() + status_interval
                self.monitor.next_status_at = next_status
            if backup or force or now >= next_backup:
                try:
                    self.monitor.check_backups(force=force)
                except Exception as exc:
                    self.log.error(f"Проверка бэкапов: {exc}")
                next_backup = time.monotonic() + backup_interval
                self.monitor.next_backup_at = next_backup

    def refresh_ui(self):
        if not self.root.winfo_exists():
            return
        status = self.monitor.status_results
        online = self.monitor.internet_state[0] is True
        ready = bool(status) and len(status) == len(self.config["servers"]) and online and all(
            row.get("ssh")
            and row.get("remote", {}).get("x_ui") == "active"
            and 200 <= row.get("panel", 0) < 400
            and (
                "site" not in row["server"].get("components", [])
                or (200 <= row.get("site", 0) < 400 and row.get("remote", {}).get("nginx") == "active")
            )
            for row in status
        )
        self.overall_var.set("●  ВСЁ РАБОТАЕТ" if ready else ("●  ПРОВЕРКА…" if self.monitor.phase else "●  ТРЕБУЕТ ВНИМАНИЯ"))
        self.overall_label.configure(fg=self.GREEN if ready else (self.YELLOW if self.monitor.phase else self.RED))
        self.internet_var.set("● Интернет подключён" if online else ("● Проверка интернета…" if self.monitor.internet_state[0] is None else "● Интернет недоступен"))
        self.internet_var_label = getattr(self, "internet_var_label", None)
        self.next_var.set(f"Обновлено: {self.monitor.last_status_at:%H:%M:%S}   •   Следующая проверка: {countdown(self.monitor.next_status_at)}   •   Дифф бэкапа: {countdown(self.monitor.next_backup_at)}   {self.monitor.phase}") if self.monitor.last_status_at else self.next_var.set(f"Подключение к серверам…   {self.monitor.phase}")
        for row in status:
            name = row["server"]["name"]
            card = self.cards.get(name)
            if not card:
                continue
            self.set_status(card["ssh"], f"Доступен  ·  {row['latency']:.1f} сек" if row["ssh"] else "Недоступен", row["ssh"])
            xui_ok = row.get("remote", {}).get("x_ui") == "active"
            self.set_status(card["xui"], "Работает" if xui_ok else "Остановлен", xui_ok)
            panel_ok = 200 <= row.get("panel", 0) < 400
            self.set_status(card["panel"], f"Доступна  ·  HTTP {row.get('panel', 0)}" if panel_ok else "Недоступна", panel_ok)
            if "site" in row["server"].get("components", []):
                site_ok = 200 <= row.get("site", 0) < 400
                self.set_status(card["site"], f"Доступен  ·  HTTP {row.get('site', 0)}" if site_ok else "Недоступен", site_ok)
            else:
                card["site"].configure(text="—", fg=self.MUTED)
            services = self.service_text(row.get("remote", {}).get("containers", []))
            card["services"].configure(text=services, fg=self.GREEN if services != "● Docker не используется" else self.MUTED)
        for item in self.backup_tree.get_children():
            self.backup_tree.delete(item)
        for server in self.config["servers"]:
            for component in server["components"]:
                key = f"{server['name']}/{component}"
                item = self.monitor.backup_status.get(key, {})
                state = item.get("state", "waiting")
                tag = "error" if state == "error" else ("checking" if state == "checking" else "clean")
                fp = item.get("fingerprint") or self.monitor.state.get("fingerprints", {}).get(server["name"], {}).get(component, "")
                self.backup_tree.insert("", "end", values=(key, item.get("text", "Ожидание"), fp[:12] or "—", item.get("checked", "—")), tags=(tag,))
        snapshot = tuple(self.log.events)
        latest = snapshot[-1] if snapshot else None
        if not self.events_initialized:
            self.last_notified_event = latest
            self.events_initialized = True
        elif latest and latest != self.last_notified_event:
            self.notify_event(latest)
            self.last_notified_event = latest
        if snapshot != self.event_snapshot and not self.events_text.tag_ranges("sel"):
            self.events_text.configure(state="normal")
            self.events_text.delete("1.0", "end")
            for stamp, level, message in snapshot:
                tag = "fail" if level == "FAIL" else ("warn" if level == "WARN" else "ok" if level == "OK" else "")
                self.events_text.insert("end", f"{stamp}  {level:<4}  {message}\n", tag)
            self.events_text.configure(state="disabled")
            self.event_snapshot = snapshot
        self.root.after(1000, self.refresh_ui)

    def resize_cards(self, _event=None):
        bounds = self.cards_canvas.bbox("all")
        if bounds:
            self.cards_canvas.configure(scrollregion=bounds, height=max(196, bounds[3] - bounds[1]))

    def reflow_cards(self, event=None):
        cards = getattr(self, "card_widgets", [])
        if not cards:
            return
        width = max(1, event.width if event else self.cards_canvas.winfo_width())
        columns = max(1, min(len(cards), width // 360))
        for index, card in enumerate(cards):
            row, column = divmod(index, columns)
            card.grid_configure(row=row, column=column, padx=(0 if column == 0 else 6, 0), pady=(0 if row == 0 else 6, 0))
        for column in range(len(cards)):
            self.cards_frame.grid_columnconfigure(column, weight=1 if column < columns else 0, minsize=0)

    def set_status(self, widget, text, ok):
        widget.configure(text=text, fg=self.GREEN if ok else self.RED)

    def service_text(self, rows):
        labels = {"nginx": "Nginx", "wg-easy": "WireGuard", "shadowbox": "Outline"}
        result = []
        for row in rows:
            name, _, status = row.partition("|")
            if name in labels:
                result.append(("✓ " if status.startswith("Up") else "✕ ") + labels[name])
        return "  ".join(result) or "● Docker не используется"

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def self_test():
    assert duration(59) == "59 сек"
    assert duration(3600) == "1 ч 0 мин"
    assert format_size(42 * 1024) == "42 КБ"
    assert format_size(1536 * 1024) == "1.5 МБ"
    assert version_tuple("v1.2.3-beta") == (1, 2, 3)
    assert version_tuple("release-without-version") == ()
    sample = "noise\n{\"ok\":true}\n"
    assert parse_json_output(sample) == {"ok": True}
    assert visible_len(paint("ONLINE", Color.GREEN)) == 6
    assert visible_len(cell("abc", 8)) == 8
    fake = type("FakeMonitor", (), {
        "internet_state": (True, ""), "status_results": [{
            "server": {"name": "NL", "host": "127.0.0.1", "components": ["x-ui", "stack"]},
            "ssh": True, "latency": 0.4, "panel": 200,
            "remote": {"x_ui": "active", "containers": ["nginx|Up", "wg-easy|Up (healthy)", "shadowbox|Up"]},
        }],
        "config": {"servers": [{"name": "NL", "host": "127.0.0.1", "components": ["x-ui", "stack"]}]},
        "next_status_at": None, "next_backup_at": None, "phase": "",
        "backup_status": {}, "state": {"fingerprints": {"NL": {"x-ui": "a" * 64, "stack": "b" * 64}}},
    })()
    original_size = shutil.get_terminal_size
    try:
        for width in (80, 118):
            shutil.get_terminal_size = lambda fallback, width=width: os.terminal_size((width, 36))
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                Dashboard(type("FakeLogger", (), {"events": []})()).render(fake)
            lines = ANSI_RE.sub("", stream.getvalue()).splitlines()
            assert max(map(len, lines)) <= width
    finally:
        shutil.get_terminal_size = original_size
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        save_json(path, {"x": "тест"})
        assert load_json(path, {}) == {"x": "тест"}
        config_path = Path(tmp) / "config.json"
        save_json(config_path, {"servers": [
            {"host": "203.0.113.10", "panel_url": "https://203.0.113.10/panel/"},
            {"name": "second", "host": "vpn.example.com", "panel_url": "https://vpn.example.com/panel/", "site_url": "https://www.example.com/", "site_root": "/var/www/example.com", "components": ["x-ui", "stack", "site"]},
        ]})
        checked = load_config(config_path)
        assert checked["retention_count"] == 30
        assert checked["servers"][0]["name"] == "203.0.113.10"
        assert checked["servers"][0]["folder"] == "203.0.113.10"
        assert checked["servers"][1]["components"] == ["x-ui", "stack", "site"]
        assert checked["servers"][1]["site_root"] == "/var/www/example.com"
        monitor = object.__new__(Monitor)
        monitor.config = {"backup_root": tmp, "retention_count": 30}
        monitor.archive_root().mkdir()
        for index in range(32):
            archive = monitor.archive_root() / f"server-backup-2026-01-01_00-00-{index:02d}.zip"
            archive.touch()
            os.utime(archive, (index, index))
        monitor.prune_archives()
        assert len(list(monitor.archive_root().glob("server-backup-*.zip"))) == 30
        assert not (monitor.archive_root() / "server-backup-2026-01-01_00-00-00.zip").exists()
    print("SELF-TEST OK")


def acquire_lock(port):
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", port))
    except OSError:
        raise SystemExit("Монитор уже запущен в другом окне.")
    return lock


def show_config_error(message):
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Настройка Server Backup Monitor", message)
    root.destroy()


def main():
    enable_windows_ansi()
    parser = argparse.ArgumentParser(description="Монитор и умные резервные копии серверов")
    parser.add_argument("--once", action="store_true", help="одна проверка и выход")
    parser.add_argument("--baseline", action="store_true", help="запомнить текущее состояние без скачивания")
    parser.add_argument("--force", action="store_true", help="скачать бэкапы даже без изменений")
    parser.add_argument("--gui", action="store_true", help="запустить графический интерфейс")
    parser.add_argument("--console", action="store_true", help="запустить текстовый режим")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return

    gui_mode = args.gui or not (args.console or args.once or args.baseline or args.force)
    try:
        config = load_config()
    except (ValueError, json.JSONDecodeError) as exc:
        if gui_mode:
            show_config_error(str(exc))
            return
        raise SystemExit(f"Ошибка конфигурации: {exc}") from None
    if gui_mode:
        logger = Logger(quiet=True)
        lock = acquire_lock(config["lock_port"])
        try:
            BackupGUI(config, logger, lock).run()
        finally:
            lock.close()
        return
    dashboard_mode = not (args.once or args.baseline or args.force)
    logger = Logger(quiet=dashboard_mode)
    lock = acquire_lock(config["lock_port"])
    monitor = Monitor(config, logger)
    if dashboard_mode:
        monitor.dashboard = Dashboard(logger)
    logger.info("Server Backup Monitor запущен. Ctrl+C — остановить.")
    monitor.refresh()
    monitor.monitor_once()
    monitor.check_backups(baseline=args.baseline, force=args.force)
    if args.once or args.baseline or args.force:
        return

    status_interval = config["status_interval_seconds"]
    backup_interval = config["backup_interval_seconds"]
    next_status = time.monotonic() + status_interval
    next_backup = time.monotonic() + backup_interval
    monitor.next_status_at = next_status
    monitor.next_backup_at = next_backup
    monitor.refresh()
    try:
        while True:
            now = time.monotonic()
            if now >= next_status:
                monitor.monitor_once()
                next_status = time.monotonic() + status_interval
                monitor.next_status_at = next_status
            if now >= next_backup:
                monitor.check_backups()
                next_backup = time.monotonic() + backup_interval
                monitor.next_backup_at = next_backup
            monitor.refresh()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Монитор остановлен пользователем")
    finally:
        lock.close()


if __name__ == "__main__":
    main()
