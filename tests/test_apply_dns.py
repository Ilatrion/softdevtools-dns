import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.apply_dns import CloudflareClient, DnsError, OWNER_COMMENT, reconcile


class FakeClient:
    def __init__(self, records=None):
        self.records = records or {}
        self.created = []

    def records_at(self, name):
        return self.records.get(name, [])

    def create(self, payload):
        self.created.append(payload)


class ApplyDnsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "records").mkdir()
        (self.root / "config").mkdir()
        (self.root / "config/reserved-names.txt").write_text("www\n")

    def add_record(self, name="ivan", target="ivan123.github.io"):
        (self.root / f"records/{name}.yaml").write_text(
            f"subdomain: {name}\ntype: CNAME\ntarget: {target}\n"
            f"repository: https://github.com/{target.removesuffix('.github.io')}/my-lab-site\n"
        )

    def test_dry_run_does_not_write(self):
        self.add_record()
        client = FakeClient()
        self.assertEqual(reconcile(self.root, client), ["would create ivan.softdevtools.ru -> ivan123.github.io"])
        self.assertEqual(client.created, [])

    def test_apply_creates_dns_only_record_with_ownership_comment(self):
        self.add_record()
        client = FakeClient()
        self.assertEqual(reconcile(self.root, client, apply=True), ["created ivan.softdevtools.ru -> ivan123.github.io"])
        self.assertEqual(client.created, [{
            "type": "CNAME", "name": "ivan.softdevtools.ru", "content": "ivan123.github.io",
            "ttl": 1, "proxied": False, "comment": OWNER_COMMENT,
        }])

    def test_managed_record_is_idempotent(self):
        self.add_record()
        client = FakeClient({"ivan.softdevtools.ru": [{
            "name": "ivan.softdevtools.ru", "type": "CNAME", "content": "ivan123.github.io",
            "proxied": False, "comment": OWNER_COMMENT,
        }]})
        self.assertEqual(reconcile(self.root, client, apply=True), ["unchanged ivan.softdevtools.ru -> ivan123.github.io"])
        self.assertEqual(client.created, [])

    def test_mixed_case_target_is_published_and_compared_as_lowercase(self):
        self.add_record(target="Ivan123.github.io")
        client = FakeClient()
        self.assertEqual(reconcile(self.root, client, apply=True), [
            "created ivan.softdevtools.ru -> ivan123.github.io"
        ])
        self.assertEqual(client.created[0]["content"], "ivan123.github.io")

        existing = FakeClient({"ivan.softdevtools.ru": [{
            "name": "ivan.softdevtools.ru", "type": "CNAME", "content": "ivan123.github.io",
            "proxied": False, "comment": OWNER_COMMENT,
        }]})
        self.assertEqual(reconcile(self.root, existing, apply=True), [
            "unchanged ivan.softdevtools.ru -> ivan123.github.io"
        ])
        self.assertEqual(existing.created, [])

    def test_foreign_record_blocks_all_writes(self):
        self.add_record("anna", "anna123.github.io")
        self.add_record()
        client = FakeClient({"ivan.softdevtools.ru": [{
            "name": "ivan.softdevtools.ru", "type": "CNAME", "content": "ivan123.github.io",
            "proxied": False, "comment": "created manually",
        }]})
        with self.assertRaisesRegex(DnsError, "not this project's CNAME"):
            reconcile(self.root, client, apply=True)
        self.assertEqual(client.created, [])

    def test_invalid_tree_blocks_api_access(self):
        self.add_record("www", "ivan123.github.io")
        client = FakeClient()
        with self.assertRaisesRegex(DnsError, "Заявки не прошли проверку"):
            reconcile(self.root, client, apply=True)
        self.assertEqual(client.created, [])

    def test_pagination_checks_every_record(self):
        client = CloudflareClient("test-token", "a" * 32)
        pages = iter([
            {"success": True, "result": [{"name": "ivan.softdevtools.ru"}], "result_info": {"total_pages": 2}},
            {"success": True, "result": [{"name": "ivan.softdevtools.ru"}], "result_info": {"total_pages": 2}},
        ])
        with patch.object(client, "_request", side_effect=lambda *args: next(pages)) as request:
            self.assertEqual(len(client.records_at("ivan.softdevtools.ru")), 2)
        self.assertIn("page=2", request.call_args.args[1])
        self.assertIn("name.exact=ivan.softdevtools.ru", request.call_args.args[1])

    def test_empty_cloudflare_page_can_report_zero_pages(self):
        client = CloudflareClient("test-token", "a" * 32)
        response = {"success": True, "result": [], "result_info": {"total_pages": 0}}
        with patch.object(client, "_request", return_value=response):
            self.assertEqual(client.records_at("ivan.softdevtools.ru"), [])


if __name__ == "__main__":
    unittest.main()
