#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DB = Path("/etc/x-ui/x-ui.db")
XUI_PATHS = [
    "/etc/x-ui",
    "/usr/local/x-ui/bin/config.json",
    "/etc/systemd/system/x-ui.service",
    "/root/cert",
    "/etc/letsencrypt",
    "/etc/ufw",
    "/etc/sysctl.conf",
    "/etc/sysctl.d",
    "/etc/ssh/sshd_config",
    "/etc/ssh/sshd_config.d",
]
STACK_PATHS = ["/root/docker-compose.yml", "/root/.wg-easy", "/root/.nginx", "/opt/outline"]
VOLATILE_TABLES = {
    "client_global_traffics",
    "client_hwids",
    "inbound_client_ips",
    "node_client_ips",
    "node_client_traffics",
    "outbound_traffics",
}
VOLATILE_COLUMNS = {
    "client_external_links": {"last_fetch_at", "last_fetch_error"},
    "client_traffics": {"up", "down", "last_online", "last_sub_fetch", "reset_count"},
    "inbounds": {"up", "down", "last_traffic_reset_time"},
    "nodes": {
        "status", "last_heartbeat", "latency_ms", "xray_version", "panel_version",
        "cpu_pct", "mem_pct", "uptime_secs", "last_error", "xray_state", "xray_error",
        "config_dirty", "config_dirty_at", "net_up", "net_down",
    },
    "outbound_subscriptions": {"last_updated", "last_error", "last_fetched_outbounds"},
}
SKIP_NAMES = {"x-ui.db", "x-ui.db-wal", "x-ui.db-shm", "system_metrics.gob", "update-status.json"}


def utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=lambda x: x.hex()).encode()


def db_fingerprint():
    if not DB.is_file():
        raise FileNotFoundError(DB)
    h = hashlib.sha256()
    uri = f"file:{DB}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=15) as db:
        db.execute("PRAGMA query_only=ON")
        tables = db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for table, schema in tables:
            if table in VOLATILE_TABLES:
                continue
            h.update(json_bytes([table, schema]))
            quoted = table.replace('"', '""')
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{quoted}")')]
            columns = [c for c in columns if c not in VOLATILE_COLUMNS.get(table, set())]
            if not columns:
                continue
            selected = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
            rows = db.execute(f'SELECT {selected} FROM "{quoted}"').fetchall()
            encoded = sorted(json_bytes(row) for row in rows)
            h.update(json_bytes(columns))
            for row in encoded:
                h.update(row)
    return h.hexdigest()


def skipped(path):
    parts = {p.lower() for p in path.parts}
    return path.name in SKIP_NAMES or "logs" in parts or "prometheus" in parts or path.suffix.lower() == ".log"


def iter_files(source):
    source = Path(source)
    if not source.exists():
        return
    if source.is_file():
        if not skipped(source):
            yield source, source.name
        return
    root = source.resolve()
    seen = set()
    for current, dirs, files in os.walk(root, followlinks=True):
        real = os.path.realpath(current)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        dirs[:] = sorted(d for d in dirs if d.lower() not in {"logs", "prometheus"})
        for name in sorted(files):
            path = Path(current) / name
            if not skipped(path) and path.is_file():
                yield path, str(path.relative_to(root)).replace("\\", "/")


def paths_fingerprint(paths):
    h = hashlib.sha256()
    for source in paths:
        base = Path(source)
        h.update(source.encode())
        if not base.exists():
            h.update(b"<missing>")
            continue
        for path, rel in iter_files(base):
            h.update(rel.encode())
            h.update(bytes.fromhex(hash_file(path)))
    return h.hexdigest()


def fingerprint(component):
    h = hashlib.sha256()
    if component == "x-ui":
        h.update(db_fingerprint().encode())
        h.update(paths_fingerprint(XUI_PATHS).encode())
    elif component == "stack":
        h.update(paths_fingerprint(STACK_PATHS).encode())
    else:
        raise ValueError(f"unknown component: {component}")
    return h.hexdigest()


def ignore_copy(_directory, names):
    return [name for name in names if name in SKIP_NAMES or name.lower() in {"logs", "prometheus"} or name.lower().endswith(".log")]


def copy_source(source, files_root):
    source = Path(source)
    if not source.exists():
        return
    destination = files_root / str(source).lstrip("/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=False, ignore=ignore_copy, dirs_exist_ok=True)
    else:
        shutil.copy2(source.resolve(), destination)


def run_report(command):
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        return f"exit={result.returncode}\n{result.stdout}"
    except Exception as exc:
        return f"error={exc}\n"


def write_reports(report_dir, component):
    commands = {
        "hostname.txt": ["hostnamectl"],
        "listeners.txt": ["ss", "-lntup"],
        "disk.txt": ["df", "-h"],
        "ufw.txt": ["ufw", "status", "verbose"],
    }
    if component == "x-ui":
        commands.update({
            "x-ui-service.txt": ["systemctl", "status", "x-ui", "--no-pager", "-l"],
            "x-ui-version.txt": ["/usr/local/x-ui/x-ui", "version"],
        })
    else:
        commands.update({
            "docker-ps.txt": ["docker", "ps", "-a"],
            "docker-compose.txt": ["docker", "compose", "-f", "/root/docker-compose.yml", "config"],
        })
    report_dir.mkdir(parents=True, exist_ok=True)
    for name, command in commands.items():
        (report_dir / name).write_text(run_report(command), encoding="utf-8")


def write_checksums(root):
    lines = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        lines.append(f"{hash_file(path)}  {path.relative_to(root).as_posix()}")
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def snapshot_database(destination):
    source_uri = f"file:{DB}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    with sqlite3.connect(destination) as check:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"database integrity check failed: {integrity}")
        dump = "\n".join(check.iterdump()) + "\n"
    destination.with_suffix(".dump.sql").write_text(dump, encoding="utf-8")
    return integrity


def make_backup(component):
    stamp = utc_stamp()
    host = socket.gethostname().replace("/", "-")
    current_fingerprint = fingerprint(component)
    with tempfile.TemporaryDirectory(prefix=f"backup-{component}-", dir="/tmp") as temporary:
        root = Path(temporary) / f"{component}-backup-{host}-{stamp}"
        files = root / "files"
        reports = root / "reports"
        files.mkdir(parents=True)
        integrity = None
        sources = XUI_PATHS if component == "x-ui" else STACK_PATHS
        for source in sources:
            copy_source(source, files)
        if component == "x-ui":
            integrity = snapshot_database(files / "x-ui.db")
        write_reports(reports, component)
        metadata = {
            "component": component,
            "created_utc": stamp,
            "fingerprint": current_fingerprint,
            "hostname": host,
            "database_integrity": integrity,
        }
        (root / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_checksums(root)
        archive = Path("/tmp") / f"{component}-backup-{host}-{stamp}.tar.gz"
        with tarfile.open(archive, "w:gz", dereference=True) as tar:
            tar.add(root, arcname=root.name, recursive=True)
    return {
        "archive": str(archive),
        "component": component,
        "fingerprint": current_fingerprint,
        "sha256": hash_file(archive),
        "size": archive.stat().st_size,
    }


def status():
    xui = run_report(["systemctl", "is-active", "x-ui"]).splitlines()
    docker = run_report(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Status}}"]).splitlines()
    return {
        "hostname": socket.gethostname(),
        "x_ui": xui[-1].strip() if xui else "unknown",
        "containers": [line for line in docker if "|" in line],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["manifest", "backup", "status"])
    parser.add_argument("component", nargs="?", choices=["x-ui", "stack"])
    args = parser.parse_args()
    if args.command in {"manifest", "backup"} and not args.component:
        parser.error("component is required")
    if args.command == "manifest":
        result = {"component": args.component, "fingerprint": fingerprint(args.component)}
    elif args.command == "backup":
        result = make_backup(args.component)
    else:
        result = status()
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
