"""Synthetic fixtures only. No network, login state, or third-party articles."""
from datetime import date
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("organizer", Path(__file__).resolve().parents[1] / "scripts/organize_exports.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def article(**changes):
    row = dict(url="https://mp.weixin.qq.com/s?__biz=TEST&mid=1&idx=1&sn=abc&scene=1",
               title="测试标题🧭", account="合成号", author="", published_at="2026-10-27",
               content_md="## 合成正文\n\n中文与Emoji 🧭\n\n![测试](images/example.png)",
               fetched_at="2026-10-27T08:00:00+08:00", status="ok", tags=["测试"])
    row.update(changes)
    return row


class OrganizerTests(unittest.TestCase):
    def run_rows(self, rows, **kwargs):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "synthetic.jsonl"
            source.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
            return M.build_files(source, **kwargs)

    def test_url_clean_and_identity(self):
        url, identity = M.canonical_url(article()["url"] + "&key=SYNTHETIC_SECRET&uin=123#frag")
        self.assertNotIn("SYNTHETIC_SECRET", url)
        self.assertNotIn("scene", url)
        self.assertEqual(identity, "long:TEST:1:1")
        self.assertIn("sn=abc", url)

    def test_url_aliases_default_idx_html_entities(self):
        _, key = M.canonical_url("http://mp.weixin.qq.com/s?__biz=TEST&amp;appmsgid=001")
        self.assertEqual(key, "long:TEST:1:1")
        with self.assertRaises(ValueError):
            M.canonical_url("https://mp.weixin.qq.com/s?__biz=TEST&mid=1&appmsgid=2")

    def test_idx_distinguishes_multi_articles(self):
        files, report = self.run_rows([article(), article(url=article()["url"].replace("idx=1", "idx=2"))])
        self.assertEqual(report["unique_articles"], 2)
        self.assertEqual(len([p for p in files if p.startswith("articles/")]), 2)

    def test_reject_unsafe_urls(self):
        for url in ["file:///etc/passwd", "https://localhost/s/x", "https://mp.weixin.qq.com.evil.test/s/x",
                    "https://user@mp.weixin.qq.com/s/x", "https://mp.weixin.qq.com:443/s/x",
                    "https://mp.weixin.qq.com/s/../../x", "https://mp.weixin.qq.com/s?__biz=T&mid=1&mid=2"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                M.canonical_url(url)

    def test_short_url_stays_distinct(self):
        url, key = M.canonical_url("https://mp.weixin.qq.com/s/synthetic-ABC?key=EXAMPLE")
        self.assertEqual(url, "https://mp.weixin.qq.com/s/synthetic-ABC")
        self.assertEqual(key, "short:synthetic-ABC")

    def test_duplicate_prefers_body_then_fetched_time(self):
        files, report = self.run_rows([article(), article(content_md="较新正文", fetched_at="2026-10-27T09:00:00+08:00"),
                                      article(status="blocked", content_md="验证码", fetched_at="2026-10-27T10:00:00+08:00")])
        self.assertEqual(report["duplicate_identity_rows"], 2)
        self.assertEqual(report["input_statuses"], {"ok": 2, "blocked": 1})
        self.assertIn("较新正文", next(v.decode() for p, v in files.items() if p.startswith("articles/")))

    def test_missing_and_failure_not_exported_as_body(self):
        rows = [article(content_md=""), article(url="https://mp.weixin.qq.com/s/blocked", status="blocked", content_md="登录页")]
        files, report = self.run_rows(rows)
        self.assertEqual(report["statuses"], {"blocked": 1, "missing_body": 1})
        self.assertFalse(any(p.startswith("articles/") for p in files))
        self.assertNotIn("登录页", files["catalog.jsonl"].decode())

    def test_same_body_distinct_sources_preserved(self):
        files, report = self.run_rows([article(), article(url="https://mp.weixin.qq.com/s/another-source")])
        catalog = [json.loads(x) for x in files["catalog.jsonl"].decode().splitlines()]
        self.assertEqual(report["unique_articles"], 2)
        self.assertEqual(sum(bool(r["same_content_as"]) for r in catalog), 1)

    def test_dates_shanghai_and_unknown_filter(self):
        _, report = self.run_rows([article(published_at="2026-10-26T17:00:00Z"),
                                   article(url="https://mp.weixin.qq.com/s/no-date", published_at="")],
                                  since=date(2026, 10, 27), until=date(2026, 10, 27))
        self.assertEqual(report["unique_articles"], 1)
        self.assertEqual(report["filtered_unknown_date"], 1)
        with self.assertRaises(ValueError):
            self.run_rows([article(published_at="2026-10-27T09:00:00")])

    def test_csv_formula_and_markdown_index_escape(self):
        files, _ = self.run_rows([article(title="=1+1", account="<script>[link](bad)")])
        self.assertIn("'=1+1", files["catalog.csv"].decode("utf-8-sig"))
        self.assertNotIn("<script>", files["INDEX.md"].decode())
        self.assertIn("\\[link\\]", files["INDEX.md"].decode())

    def test_unknown_fields_and_invalid_dates_fail(self):
        for row in [article(cookie="SYNTHETIC"), article(tags="bad"), article(published_at="2026-99-99"),
                    article(fetched_at="yesterday"), article(status=["bad"]), article(title=None)]:
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, "line 1"):
                self.run_rows([row])

    def test_idempotent_and_no_overwrite(self):
        files, _ = self.run_rows([article(title="../../evil")])
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            self.assertEqual(M.write_files(out, files), "created")
            before = {p: (out / p).read_bytes() for p in files}
            self.assertEqual(M.write_files(out, files), "unchanged")
            with self.assertRaises(ValueError):
                M.write_files(out, {"INDEX.md": b"changed"})
            self.assertEqual(before, {p: (out / p).read_bytes() for p in files})
            link = Path(d) / "link"
            link.symlink_to(out, target_is_directory=True)
            with self.assertRaises(ValueError):
                M.write_files(link, files)

    def test_keyword_and_deterministic_output(self):
        files, report = self.run_rows([article(), article(url="https://mp.weixin.qq.com/s/excluded", content_md="other", title="other")], keyword="中文")
        repeated, _ = self.run_rows([article()], keyword="中文")
        # Reports differ in filtered/input counts, but article bytes are deterministic.
        self.assertEqual(files["catalog.jsonl"], repeated["catalog.jsonl"])
        self.assertEqual(report["filtered_other"], 1)

    def test_cli_success_idempotency_and_private_error(self):
        script = Path(M.__file__)
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "input.jsonl"
            out = Path(d) / "out"
            source.write_text(json.dumps(article()), encoding="utf-8")
            command = [sys.executable, str(script), "--input", str(source), "--output", str(out)]
            first = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(first.stdout)["result"], "created")
            second = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(second.stdout)["result"], "unchanged")
            self.assertTrue((out / "INDEX.md").is_file())
            source.write_text('{"cookie":"SYNTHETIC_PRIVATE_VALUE"}', encoding="utf-8")
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(failed.returncode, 2)
            self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", failed.stderr)
            self.assertIn("line 1", failed.stderr)


if __name__ == "__main__":
    unittest.main()
