"""Behavior tests for public data boundaries, freshness, and generated assets."""
from datetime import datetime, timezone
import json
from io import BytesIO
from urllib.error import HTTPError
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from update_profile import Client, APIError, collect, summarize, write_dashboard, dashboard_svg, START, END


def repo(name="example", **kw):
    return {"name":name,"owner":"sharanyapal","private":False,"fork":False,"language":"Python","default_branch":"main","pushed_at":"2026-11-07T12:00:00Z","updated_at":"2026-11-07T12:00:00Z",**kw}


def commit(n=1, **kw):
    return {"sha":f"{n:040x}","author":"sharanyapal","repository":"sharanyapal/example","message":"feat: add a parser","authored_at":"2026-11-07T12:00:00Z",**kw}


def source():
    return {"owner":"sharanyapal","timezone":"Asia/Kolkata","captured_at":"2026-11-07T20:00:00+00:00","repos":[repo(),repo("sharanyapal")],"commits":[commit()]}


class DashboardTests(unittest.TestCase):
    def test_only_owned_public_repositories_count(self):
        s=source()
        s["repos"] += [repo("secret",private=True),repo("external",owner="someone-else"),repo("forked",fork=True)]
        s["commits"] += [commit(2,repository="sharanyapal/secret"),commit(3,repository="sharanyapal/forked")]
        data=summarize(s)
        self.assertEqual(data["metrics"]["public_repos"],3)
        self.assertEqual(data["metrics"]["original_repos"],1)
        self.assertEqual(data["metrics"]["commits_30d"],1)
        self.assertNotIn("secret",json.dumps(data))

    def test_bot_duplicates_and_future_commits_do_not_count(self):
        s=source()
        s["commits"] += [commit(),commit(2,author="github-actions[bot]"),commit(3,authored_at="2026-12-01T00:00:00Z"),commit(4,authored_at="2026-01-01T00:00:00Z")]
        self.assertEqual(summarize(s)["metrics"]["commits_30d"],1)

    def test_india_midnight_and_thirty_day_boundary(self):
        s=source()
        s["commits"]=[commit(1,authored_at="2026-10-09T18:30:00Z"),commit(2,authored_at="2026-10-09T18:29:59Z"),commit(3,authored_at="2026-11-07T18:30:00Z")]
        data=summarize(s)
        self.assertEqual(data["window_start"],"2026-10-10")
        self.assertEqual(data["window_end"],"2026-11-08")
        self.assertEqual(data["metrics"]["commits_30d"],2)
        self.assertEqual(data["metrics"]["active_days_30d"],2)

    def test_same_signal_does_not_create_clock_only_update(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/"README.md").write_text("before\n"+START+"\n"+END+"\nafter\n")
            original=write_dashboard(root,source())
            before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob("*") if p.is_file()}
            later=source();later["captured_at"]="2026-11-07T21:00:00+00:00"
            later["repos"][1]["pushed_at"]="2026-11-07T20:30:00Z"
            later["repos"][1]["updated_at"]="2026-11-07T20:30:00Z"
            updated=write_dashboard(root,later)
            after={str(p.relative_to(root)):p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(before,after)
            self.assertEqual(original["generated_at"],updated["generated_at"])

    def test_new_commit_updates_metrics_and_image_url(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/"README.md").write_text("intro\n"+START+"\n"+END+"\noutro")
            first=write_dashboard(root,source())
            s=source();s["commits"].append(commit(2))
            second=write_dashboard(root,s)
            self.assertEqual(second["metrics"]["commits_30d"],2)
            self.assertNotEqual(first["fingerprint"],second["fingerprint"])
            self.assertEqual(len(list((root/"assets").glob("dashboard-*.svg"))),1)
            self.assertTrue((root/"README.md").read_text().startswith("intro"))
            self.assertTrue((root/"README.md").read_text().endswith("outro"))

    def test_invalid_markers_do_not_write_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/"README.md").write_text("unchanged")
            with self.assertRaises(ValueError):write_dashboard(root,source())
            self.assertFalse((root/"assets").exists())
            self.assertEqual((root/"README.md").read_text(),"unchanged")

    def test_svg_is_well_formed_with_untrusted_language_text(self):
        s=source();s["repos"][0]["language"]='<script>&" |'
        data=summarize(s);data["generated_at"]=s["captured_at"]
        svg=dashboard_svg(data)
        ET.fromstring(svg)
        self.assertNotIn("<script>",svg)

    def test_full_rest_pagination(self):
        client=Client()
        with patch.object(client,"get",side_effect=[list(range(100)),[100]]) as get:
            self.assertEqual(len(client.pages("/users/example/repos")),101)
            self.assertIn("page=2",get.call_args_list[-1].args[0])

    def test_only_explicitly_empty_repository_is_zero_activity(self):
        client=Client()
        empty=HTTPError("https://api.github.com/example",409,"Conflict",{},BytesIO(b'{"message":"Git Repository is empty."}'))
        with patch("update_profile.urlopen",side_effect=empty):
            self.assertEqual(client.get("/repos/example/empty/commits",empty_ok=True),[])
        conflict=HTTPError("https://api.github.com/example",409,"Conflict",{},BytesIO(b'{"message":"Repository conflict"}'))
        with patch("update_profile.urlopen",side_effect=conflict):
            with self.assertRaises(APIError):client.get("/repos/example/problem/commits",empty_ok=True)

    def test_collection_failure_is_not_a_zero_activity_snapshot(self):
        client=Client()
        with patch.object(client,"pages",side_effect=[[repo()],APIError("temporary outage")]):
            with self.assertRaises(APIError):
                collect(client,"sharanyapal",datetime(2026,11,7,20,tzinfo=timezone.utc),"Asia/Kolkata")


if __name__=="__main__":
    unittest.main()
