"""Create approved student CNAMEs in Cloudflare; never update or delete records."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.messages import render_text
from scripts.validate import parse_record, validate_records


ZONE = "softdevtools.ru"
OWNER_COMMENT = "Managed by denrad/softdevtools-dns"
ZONE_ID_PATTERN = re.compile(r"[a-f0-9]{32}\Z")


class DnsError(Exception):
    """A DNS request cannot be safely completed."""


class CloudflareClient:
    def __init__(self, token: str, zone_id: str):
        if not token or not ZONE_ID_PATTERN.fullmatch(zone_id):
            raise DnsError("CLOUDFLARE_API_TOKEN and a valid CLOUDFLARE_ZONE_ID are required")
        self.token = token
        self.base_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"

    def _request(self, method: str, url: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                data = json.load(response)
        except HTTPError as exc:
            raise DnsError(f"Cloudflare API returned HTTP {exc.code}") from None
        except (URLError, TimeoutError, ValueError) as exc:
            raise DnsError(f"Cloudflare API request failed: {type(exc).__name__}") from None
        if not isinstance(data, dict) or data.get("success") is not True:
            raise DnsError("Cloudflare API did not confirm success")
        return data

    def records_at(self, name: str) -> list[dict]:
        records = []
        page = 1
        while True:
            query = urlencode({"name.exact": name, "page": page, "per_page": 100})
            data = self._request("GET", f"{self.base_url}?{query}")
            result = data.get("result")
            info = data.get("result_info")
            if not isinstance(result, list) or not isinstance(info, dict):
                raise DnsError("Cloudflare API returned an incomplete record list")
            records.extend(result)
            total_pages = info.get("total_pages")
            if type(total_pages) is int and total_pages == 0 and page == 1 and not result:
                break
            if type(total_pages) is not int or total_pages < 1 or total_pages < page:
                raise DnsError("Cloudflare API returned invalid pagination")
            if page >= total_pages:
                break
            page += 1
        return records

    def create(self, record: dict) -> None:
        data = self._request("POST", self.base_url, record)
        if not isinstance(data.get("result"), dict):
            raise DnsError("Cloudflare API did not return the created record")


def reconcile(root: Path, client: CloudflareClient, *, apply: bool = False) -> list[str]:
    """Validate everything first; plan or create only missing CNAMEs."""
    errors = validate_records(root)
    if errors:
        raise DnsError("Заявки не прошли проверку:\n" + render_text(errors))

    actions = []
    to_create = []
    for path in sorted((root / "records").glob("*.yaml")):
        # Полная проверка дерева выше уже прошла, поэтому ошибок здесь быть не может.
        record, _errors, _positions = parse_record(path)
        name = f"{record['subdomain']}.{ZONE}"
        target = record["target"].lower()
        existing = client.records_at(name)
        # Verify exact names ourselves too; never trust a loose API filter.
        if any(not isinstance(item, dict) or item.get("name", "").rstrip(".").lower() != name for item in existing):
            raise DnsError(f"{name}: Cloudflare returned an unexpected record")
        if existing:
            if len(existing) != 1 or not (
                existing[0].get("type") == "CNAME"
                and existing[0].get("content", "").rstrip(".").lower() == target
                and existing[0].get("proxied") is False
                and existing[0].get("comment") == OWNER_COMMENT
            ):
                raise DnsError(f"{name}: existing DNS record is not this project's CNAME")
            actions.append(f"unchanged {name} -> {target}")
            continue
        payload = {
            "type": "CNAME",
            "name": name,
            "content": target,
            "ttl": 1,
            "proxied": False,
            "comment": OWNER_COMMENT,
        }
        to_create.append(payload)
        actions.append(f"would create {name} -> {target}")
    # Finish all conflict checks before the first write.
    if apply:
        for payload in to_create:
            client.create(payload)
        actions = [action.replace("would create", "created", 1) for action in actions]
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan or apply approved Cloudflare CNAME records")
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    parser.add_argument("--apply", action="store_true", help="Create missing records; default is read-only")
    args = parser.parse_args()
    try:
        client = CloudflareClient(
            os.environ.get("CLOUDFLARE_API_TOKEN", ""),
            os.environ.get("CLOUDFLARE_ZONE_ID", ""),
        )
        actions = reconcile(args.root, client, apply=args.apply)
    except DnsError as exc:
        print(exc, file=sys.stderr)
        return 1
    for action in actions:
        print(action)
    print(f"{len(actions)} approved DNS request(s) checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
