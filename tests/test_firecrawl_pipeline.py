"""Synthetic-only fixtures. No WeChat requests, credentials, or copied article text."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from clean_firecrawl import extract_response
from firecrawl_batch import collect, load_manifest, local_scrape, validate_api
import firecrawl_batch

URL = "https://mp.weixin.qq.com/s/synthetic-sample"


def fixture(body="<p>合成正文🌟</p>", account="合成账号", extra="", date="2026年10月27日 10:00"):
    return dict(success=True, data=dict(metadata=dict(statusCode=200, title=""), html=(
        '<h1 id="activity-name">合成标题</h1><span id="js_name">' + account + '</span>'
        '<em id="publish_time">' + date + '</em><div id="js_content">' + body + '</div>' + extra)))


class CleanerTests(unittest.TestCase):
    def clean(self, payload, account=""):
        return extract_response(payload, URL, account)

    def test_body_only_and_title_fallback(self):
        r, q = self.clean(fixture(extra="<div>写留言 投诉</div><script>evil()</script>"))
        self.assertEqual(r["content_md"], "合成正文🌟")
        self.assertEqual(r["title"], "合成标题")
        self.assertEqual(r["published_at"], "2026-10-27")
        self.assertEqual(r["status"], "ok")

    def test_empty_metadata_never_invents_date(self):
        r, q = self.clean(fixture(date=""))
        self.assertEqual(r["published_at"], "")
        self.assertIn("published_at_missing", q["warnings"])
        self.assertEqual(r["fetched_at"], "")

    def test_invalid_date_not_invented(self):
        r, q = self.clean(fixture(date="2026年2月31日"))
        self.assertEqual(r["published_at"], "")

    def test_lazy_images_only_in_body(self):
        r, q = self.clean(fixture('<p>正文</p><img data-src="https://example.com/body.png" alt="图">',
                                 extra='<img src="https://example.com/chrome.png">'))
        self.assertEqual(q["image_refs"], 1)
        self.assertIn("body.png", r["content_md"])
        self.assertNotIn("chrome.png", r["content_md"])

    def test_image_only_flag(self):
        r, q = self.clean(fixture('<img src="https://example.com/a.png">'))
        self.assertEqual(r["status"], "ok")
        self.assertIn("image_only_requires_visual_review", q["warnings"])

    def test_hidden_parent_not_counted(self):
        r, q = self.clean(fixture('<div hidden><img src="https://example.com/a.png"></div>'))
        self.assertEqual(r["status"], "missing_body")
        self.assertEqual(q["image_refs"], 0)

    def test_no_executable_links_or_scripts(self):
        r, _ = self.clean(fixture('<script>secret()</script><a href="javascript:evil()">文字</a><img src="data:x">'))
        self.assertNotIn("javascript", r["content_md"])
        self.assertNotIn("secret", r["content_md"])
        self.assertEqual(r["content_md"], "文字")

    def test_private_address_images_not_emitted(self):
        r, q = self.clean(fixture('<p>正文</p><img src="http://127.0.0.1/admin">'
                                 '<img src="http://169.254.169.254/latest">'
                                 '<img src="http://localhost/a.png">'))
        self.assertEqual(q["image_refs"], 0)
        self.assertNotIn("127.0.0.1", r["content_md"])

    def test_fetch_time_must_have_timezone(self):
        with self.assertRaises(ValueError):
            extract_response(fixture(), URL, fetched_at="2026-10-27T10:00:00")

    def test_missing_container_no_markdown_fallback(self):
        r, _ = self.clean(dict(success=True, data=dict(html="<p>非正文</p>", markdown="搜索摘要")))
        self.assertEqual(r["status"], "missing_body")
        self.assertEqual(r["content_md"], "")

    def test_empty_body(self):
        r, _ = self.clean(fixture("<p> </p>"))
        self.assertEqual(r["status"], "missing_body")

    def test_challenge_page(self):
        r, _ = self.clean(dict(success=True, data=dict(html="<p>环境异常，请完成验证</p>")))
        self.assertEqual(r["status"], "blocked")

    def test_markdown_only_challenge(self):
        r, _ = self.clean(dict(success=True, data=dict(markdown="请完成验证")))
        self.assertEqual(r["status"], "blocked")

    def test_article_about_verification_is_not_blocked(self):
        r, _ = self.clean(fixture("<p>文章在介绍验证码的用途。</p>"))
        self.assertEqual(r["status"], "ok")

    def test_http_restrictions(self):
        for code in (401, 403, 429):
            with self.subTest(code=code):
                r, _ = self.clean(dict(success=False, data=dict(metadata=dict(statusCode=code))))
                self.assertEqual(r["status"], "blocked")

    def test_wrong_account(self):
        r, q = self.clean(fixture(account="另一个号"), "目标号")
        self.assertEqual(r["status"], "error")
        self.assertEqual(q["reason"], "account_mismatch")
        self.assertEqual(r["content_md"], "")

    def test_structure_and_media_warning(self):
        r, q = self.clean(fixture('<h2>小节</h2><ul><li>甲</li><li>乙</li></ul>'
                                 '<blockquote><p>引用</p></blockquote><table><tr><th>列</th></tr>'
                                 '<tr><td>值</td></tr></table><video src="x"></video>'))
        for content in ("## 小节", "- 甲", "> 引用", "| 值 |", "嵌入音视频未归档"):
            self.assertIn(content, r["content_md"])
        self.assertIn("embedded_media_not_archived", q["warnings"])

    def test_non_article_url_rejected(self):
        with self.assertRaises(ValueError):
            extract_response(fixture(), "https://mp.weixin.qq.com/mp/profile_ext")


class BatchTests(unittest.TestCase):
    def rows(self, count):
        return [dict(url=f"https://mp.weixin.qq.com/s/synthetic-{i}", discovery_source="synthetic") for i in range(count)]

    def run_batch(self, rows, out, **kwargs):
        return collect(rows, out, "http://127.0.0.1:3002", "合成账号", sleeper=lambda _: None,
                       scraper=kwargs.pop("scraper", lambda *_: fixture()), **kwargs)

    def test_100_synthetic_not_latest_100_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_batch(self.rows(100), Path(directory) / "batch", limit=100, sample_verified=True)
            self.assertEqual(report["statuses"], {"ok": 100})
            self.assertEqual(report["pending"], 0)
            self.assertFalse(report["latest_100_verified"])
            self.assertFalse(report["history_list_verified"])
            self.assertEqual(len(list((Path(directory) / "batch/archive/articles").glob("*.md"))), 100)

    def test_stop_on_first_block_and_no_resume_retry(self):
        calls = []
        def scrape(*args):
            calls.append(args)
            return dict(success=False, data=dict(metadata=dict(statusCode=429)))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "batch"
            report = self.run_batch(self.rows(5), out, scraper=scrape)
            self.assertEqual(len(calls), 1)
            self.assertEqual(report["pending"], 4)
            with self.assertRaises(ValueError):
                self.run_batch(self.rows(5), out, resume=True, scraper=scrape)
            self.assertEqual(len(calls), 1)

    def test_account_mismatch_stops_source(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_batch(self.rows(5), Path(directory) / "batch", scraper=lambda *_: fixture(account="其他"))
            self.assertEqual(report["processed"], 1)
            self.assertEqual(report["stopped_reason"], "account_mismatch")

    def test_api_error_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_batch(self.rows(5), Path(directory) / "batch", scraper=lambda *_: dict(success=False))
            self.assertEqual(report["processed"], 1)

    def test_missing_body_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_batch(self.rows(2), Path(directory) / "batch", scraper=lambda *_: fixture(""))
            self.assertEqual(report["statuses"], {"missing_body": 2})

    def test_large_batch_requires_sample_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                self.run_batch(self.rows(6), Path(directory) / "batch", limit=6)

    def test_default_limit_five(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_batch(self.rows(8), Path(directory) / "batch")
            self.assertEqual(report["selected"], 5)
            self.assertEqual(report["unique_discovered"], 8)

    def test_completed_resume_makes_no_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "batch"
            self.run_batch(self.rows(2), out)
            def unexpected(*_):
                self.fail("completed response must not be fetched again")
            report = self.run_batch(self.rows(2), out, resume=True, scraper=unexpected)
            self.assertEqual(report["processed"], 2)

    def test_saved_raw_recovery_does_not_refetch(self):
        calls, checkpoints = [], []
        original_write = firecrawl_batch.private_write
        def interrupted_write(path, value):
            if path.name == "checkpoint.json":
                checkpoints.append(path)
                if len(checkpoints) == 2:
                    raise KeyboardInterrupt()
            return original_write(path, value)
        def scrape(*args):
            calls.append(args)
            return fixture()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "batch"
            with patch("firecrawl_batch.private_write", side_effect=interrupted_write):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_batch(self.rows(1), out, scraper=scrape)
            report = self.run_batch(self.rows(1), out, scraper=scrape, resume=True)
            self.assertEqual(len(calls), 1)
            self.assertEqual(report["statuses"], {"ok": 1})
            checkpoint = json.loads((out / "checkpoint.json").read_text())
            self.assertTrue(checkpoint["completed"][0]["recovered_saved_response"])

    def test_empty_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            self.run_batch([], Path(directory) / "batch")

    def test_changed_manifest_resume_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "batch"
            self.run_batch(self.rows(1), out)
            with self.assertRaises(ValueError):
                self.run_batch(self.rows(2), out, resume=True)

    def test_invalid_payload_stops_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_batch(self.rows(2), Path(directory) / "batch", scraper=lambda *_: [])
            self.assertEqual(report["statuses"], {"error": 1})
            self.assertEqual(report["pending"], 1)

    def test_manifest_dedup_and_credential_stripping(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "input.jsonl"
            manifest.write_text(json.dumps(dict(url=URL + "?access_token=synthetic")) + "\n" + json.dumps(dict(url=URL)))
            rows, count = load_manifest(manifest)
            self.assertEqual(count, 2)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["url"], URL)

    def test_only_local_api(self):
        for value in ("https://api.firecrawl.dev", "http://localhost:3002", "http://127.0.0.1:3002/path", "http://u:p@127.0.0.1:3002"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_api(value)

    def test_live_request_contract_mock_transport(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, *_): return json.dumps(fixture()).encode()
        with patch("firecrawl_batch.build_opener") as opener:
            opener.return_value.open.return_value = Response()
            result = local_scrape("http://127.0.0.1:3002", URL)
            request = opener.return_value.open.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(request.full_url, "http://127.0.0.1:3002/v2/scrape")
            self.assertEqual(body["proxy"], "basic")
            self.assertEqual(body["maxAge"], 0)
            self.assertNotIn("headers", body)
            self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
