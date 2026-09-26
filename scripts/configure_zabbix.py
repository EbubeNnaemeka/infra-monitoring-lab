#!/usr/bin/env python3
"""Idempotently configure the lab Zabbix server via its JSON-RPC API.

- Rotates the default Admin password (reads ZBX_ADMIN_PASSWORD from .env)
- Creates host groups and registers every host in hosts.json with its template

Usage: python3 scripts/configure_zabbix.py [--url http://127.0.0.1:8081]
"""
import argparse
import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_ADMIN_PASSWORD = "zabbix"


def load_env(path):
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


class Zabbix:
    def __init__(self, url):
        self.url = url.rstrip("/") + "/api_jsonrpc.php"
        self.token = None
        self._id = 0

    def call(self, method, params):
        self._id += 1
        headers = {"Content-Type": "application/json-rpc"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": self._id})
        req = urllib.request.Request(self.url, body.encode(), headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error'].get('data') or data['error']}")
        return data["result"]

    def login(self, password):
        self.token = None
        self.token = self.call("user.login", {"username": "Admin", "password": password})


def ensure_admin_password(zbx, new_password):
    try:
        zbx.login(new_password)
        print("Admin password already rotated")
        return
    except RuntimeError:
        pass
    zbx.login(DEFAULT_ADMIN_PASSWORD)
    admin = zbx.call("user.get", {"filter": {"username": "Admin"}, "output": ["userid"]})[0]
    zbx.call("user.update", {
        "userid": admin["userid"],
        "current_passwd": DEFAULT_ADMIN_PASSWORD,
        "passwd": new_password,
    })
    zbx.login(new_password)
    print("Rotated default Admin password")


def ensure_group(zbx, name):
    found = zbx.call("hostgroup.get", {"filter": {"name": [name]}, "output": ["groupid"]})
    if found:
        return found[0]["groupid"]
    print(f"Created host group {name}")
    return zbx.call("hostgroup.create", {"name": name})["groupids"][0]


def template_id(zbx, name):
    found = zbx.call("template.get", {"filter": {"host": [name]}, "output": ["templateid"]})
    if not found:
        raise RuntimeError(f"Template not found: {name}")
    return found[0]["templateid"]


def ensure_host(zbx, host):
    group_id = ensure_group(zbx, host["group"])
    tmpl_ids = [{"templateid": template_id(zbx, t)} for t in host["templates"]]
    use_dns = not host["address"].replace(".", "").isdigit()
    interface = {
        "type": 1, "main": 1, "port": "10050",
        "useip": 0 if use_dns else 1,
        "dns": host["address"] if use_dns else "",
        "ip": "" if use_dns else host["address"],
    }
    existing = zbx.call("host.get", {"filter": {"host": [host["name"]]}, "output": ["hostid"]})
    if existing:
        zbx.call("host.update", {
            "hostid": existing[0]["hostid"],
            "groups": [{"groupid": group_id}],
            "templates": tmpl_ids,
        })
        print(f"Updated host {host['name']}")
    else:
        zbx.call("host.create", {
            "host": host["name"],
            "groups": [{"groupid": group_id}],
            "templates": tmpl_ids,
            "interfaces": [interface],
        })
        print(f"Created host {host['name']}")


def disable_builtin_server_host(zbx):
    # The image ships a "Zabbix server" host that polls an agent on 127.0.0.1
    # inside the server container. There is none, so it alerts forever.
    found = zbx.call("host.get", {"filter": {"host": ["Zabbix server"]}, "output": ["hostid", "status"]})
    if found and found[0]["status"] == "0":
        zbx.call("host.update", {"hostid": found[0]["hostid"], "status": 1})
        print("Disabled built-in 'Zabbix server' host (no local agent in this stack)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8081")
    parser.add_argument("--hosts", default=str(ROOT / "hosts.json"))
    args = parser.parse_args()

    env = load_env(ROOT / ".env")
    new_password = env.get("ZBX_ADMIN_PASSWORD")
    if not new_password:
        sys.exit("Set ZBX_ADMIN_PASSWORD in .env before running")

    zbx = Zabbix(args.url)
    ensure_admin_password(zbx, new_password)
    disable_builtin_server_host(zbx)

    hosts = json.loads(pathlib.Path(args.hosts).read_text())
    for host in hosts:
        if host.get("enabled", True):
            ensure_host(zbx, host)
        else:
            print(f"Skipped {host['name']} (enabled: false)")


if __name__ == "__main__":
    main()
