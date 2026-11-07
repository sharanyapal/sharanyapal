#!/usr/bin/env python3
"""Build a public GitHub profile dashboard using only Python's standard library.

No tracking service, personal access token, or private repository data is needed.
Metrics are explicitly scoped; this is not a replacement for GitHub contributions.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

API = "https://api.github.com"
START = "<!-- PROFILE:LIVE:START -->"
END = "<!-- PROFILE:LIVE:END -->"
ROOT = Path(__file__).resolve().parents[1]
BOT_NAMES = {"github-actions[bot]", "dependabot[bot]"}
PALETTE = ["#b9ef5b", "#a799ed", "#65cbb9", "#f2b86c", "#e393b6", "#85bde3"]


class APIError(RuntimeError):
    pass


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class Client:
    def __init__(self, token: str | None = None):
        self.token = token

    def get(self, path: str, *, empty_ok: bool = False):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Only GitHub API paths are accepted")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "sharanyapal-profile-dashboard", "X-GitHub-Api-Version": "2026-03-10"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        for attempt in range(3):
            try:
                with urlopen(Request(API + path, headers=headers), timeout=30) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code == 409 and empty_ok:
                    try:
                        message=json.loads(error.read()).get("message", "").lower()
                    except (ValueError, AttributeError):
                        message=""
                    if "repository is empty" in message:
                        return []
                if error.code in (429, 500, 502, 503, 504) and attempt < 2:
                    delay = min(30, max(1, int(error.headers.get("Retry-After", "2"))))
                    time.sleep(delay * (attempt + 1))
                    continue
                raise APIError(f"GitHub API returned HTTP {error.code}; previous dashboard was preserved.") from None
            except (URLError, TimeoutError):
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise APIError("GitHub API connection failed; previous dashboard was preserved.") from None
        raise APIError("GitHub request did not complete")

    def pages(self, path: str, params: dict | None = None, *, empty_ok=False):
        values = dict(params or {})
        page, rows = 1, []
        while True:
            query = urlencode({**values, "per_page": 100, "page": page})
            batch = self.get(path + "?" + query, empty_ok=empty_ok)
            if not isinstance(batch, list):
                raise APIError("Unexpected GitHub response; refusing to publish partial metrics")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
            page += 1


def sanitize_repo(repo: dict, owner: str) -> dict | None:
    actual_owner = repo.get("owner", "")
    if isinstance(actual_owner, dict):
        actual_owner = actual_owner.get("login", "")
    if repo.get("private") or str(actual_owner).lower() != owner.lower():
        return None
    name = repo["name"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("Invalid repository name")
    return {
        "name": name, "full_name": owner + "/" + name,
        "url": "https://github.com/" + owner + "/" + name,
        "description": str(repo.get("description") or ""),
        "language": repo.get("language"), "fork": bool(repo.get("fork")),
        "archived": bool(repo.get("archived")), "disabled": bool(repo.get("disabled")),
        "stars": int(repo.get("stars", repo.get("stargazers_count", 0))),
        "forks": int(repo.get("forks", repo.get("forks_count", 0))),
        "default_branch": repo.get("default_branch") or "main",
        # Dashboard bot commits must not keep making the dashboard dirty.
        "pushed_at": None if name.lower() == owner.lower() else repo.get("pushed_at"),
        "updated_at": None if name.lower() == owner.lower() else repo.get("updated_at"),
        "owner": owner, "private": False,
    }


def collect(client: Client, owner: str, now: datetime, zone: str) -> dict:
    local = now.astimezone(ZoneInfo(zone))
    start = datetime.combine(local.date() - timedelta(days=29), datetime.min.time(), tzinfo=ZoneInfo(zone))
    raw_repos = client.pages(f"/users/{quote(owner)}/repos", {"type": "owner", "sort": "full_name"})
    repos = [r for value in raw_repos if (r := sanitize_repo(value, owner)) is not None]
    authored_repos = [r for r in repos if not r["fork"] and not r["disabled"]]

    def commits_for(repo):
        path = "/repos/" + repo["full_name"] + "/commits"
        rows = client.pages(path, {"author": owner, "since": start.astimezone(timezone.utc).isoformat(), "sha": repo["default_branch"]}, empty_ok=True)
        return [{"sha": c["sha"], "url": c["html_url"], "message": c["commit"]["message"].splitlines()[0], "authored_at": c["commit"]["author"]["date"], "author": (c.get("author") or {}).get("login", owner), "repository": repo["full_name"]} for c in rows]

    with ThreadPoolExecutor(max_workers=4) as pool:
        groups = list(pool.map(commits_for, authored_repos))
    return {"owner": owner, "timezone": zone, "captured_at": now.isoformat(), "repos": repos, "commits": [c for group in groups for c in group]}


def summarize(source: dict) -> dict:
    owner = source["owner"]
    if not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        raise ValueError("Invalid owner")
    zone = ZoneInfo(source.get("timezone", "Asia/Kolkata"))
    now = parse_time(source["captured_at"])
    today = now.astimezone(zone).date()
    start = today - timedelta(days=29)
    repos = sorted([r for value in source["repos"] if (r := sanitize_repo(value, owner)) is not None], key=lambda r: r["name"].lower())
    originals = {r["full_name"] for r in repos if not r["fork"] and not r["disabled"]}
    commits, seen = [], set()
    for c in source["commits"]:
        if c.get("author", "").lower() != owner.lower() or c.get("author") in BOT_NAMES:
            continue
        if c.get("repository") not in originals:
            continue
        stamp = parse_time(c["authored_at"])
        day = stamp.astimezone(zone).date()
        # This is an authored-date metric, not a count of all repository pushes.
        if not start <= day <= today or stamp > now:
            continue
        if not re.fullmatch(r"[a-fA-F0-9]{40}", c["sha"]):
            raise ValueError("Invalid commit SHA")
        if c["sha"] in seen:
            continue
        seen.add(c["sha"])
        repo = c["repository"]
        commits.append({"sha": c["sha"], "repository": repo, "url": f"https://github.com/{repo}/commit/{c['sha']}", "message": c["message"].splitlines()[0][:240], "authored_at": stamp.astimezone(timezone.utc).isoformat(), "day": day.isoformat()})
    commits.sort(key=lambda c: (c["authored_at"], c["sha"]), reverse=True)
    days = Counter(c["day"] for c in commits)
    languages = Counter(r["language"] for r in repos if not r["fork"] and r["name"].lower() != owner.lower() and r.get("language"))
    series = [{"date": (start + timedelta(days=i)).isoformat(), "count": days[(start + timedelta(days=i)).isoformat()]} for i in range(30)]
    recent = sorted([r for r in repos if r.get("pushed_at") and r["name"].lower() != owner.lower()], key=lambda r: (r["pushed_at"], r["name"]), reverse=True)[:5]
    result = {"owner": owner, "timezone": str(zone), "window_start": start.isoformat(), "window_end": today.isoformat(), "repos": repos, "commits": commits, "recent": recent, "series": series, "languages": dict(sorted(languages.items(), key=lambda row: (-row[1], row[0]))), "metrics": {"public_repos": len(repos), "original_repos": sum(not r["fork"] and r["name"].lower() != owner.lower() for r in repos), "languages": len(languages), "commits_30d": len(commits), "active_days_30d": len(days), "stars": sum(r["stars"] for r in repos), "forks": sum(r["forks"] for r in repos)}}
    result["fingerprint"] = sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def text(x, y, content, size=16, fill="#dce6d0", weight="400", extra=""):
    return f'<text x="{x}" y="{y}" font-family="DejaVu Sans,Arial,sans-serif" font-size="{size}" font-weight="{weight}" fill="{fill}" {extra}>{escape(str(content))}</text>'


def svg_open(width, height, title, description):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc"><title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>'


def header_svg():
    parts = [svg_open(1200, 320, "Sharanya Pal — Systems & Stories", "A music-player-inspired creative coding profile. Identity, intelligence, algorithms, and a few side quests."), '<style>@keyframes levels{to{transform:scaleY(.45)}}.eq{transform-box:fill-box;transform-origin:50% 100%;animation:levels 1.5s ease-in-out infinite alternate}@media(prefers-reduced-motion:reduce){.eq{animation:none}}</style><defs><radialGradient id="glow"><stop stop-color="#b9ef5b" stop-opacity=".13"/><stop offset="1" stop-color="#b9ef5b" stop-opacity="0"/></radialGradient></defs>', '<rect x="1" y="1" width="1198" height="318" rx="22" fill="#0c110d" stroke="#35452c"/>', '<circle cx="1020" cy="160" r="240" fill="url(#glow)"/>']
    parts += [text(42, 40, "SP / SYSTEMS & STORIES", 12, "#b9ef5b", "700", 'letter-spacing="3"'), text(42, 123, "Sharanya Pal", 76, "#f1f6ea", "700", 'letter-spacing="-4"'), text(44, 170, "Code. Commit. Play it again.", 25, "#bcccae"), text(44, 218, "Identity. Intelligence. Algorithms. A few side quests.", 16, "#879a77")]
    for i in range(9):
        parts.append(f'<ellipse cx="1000" cy="144" rx="{42+i*12}" ry="107" transform="rotate(-28 1000 144)" fill="none" stroke="#b9ef5b" stroke-opacity="{.12+i*.04}"/>')
    parts += [text(965, 167, "sp", 62, "#c7f77a", "700", 'letter-spacing="-6"'), '<rect x="42" y="258" width="225" height="29" rx="14" fill="#b9ef5b"/>', text(60, 278, "BUILD MODE: CURIOUS", 11, "#18220e", "700", 'letter-spacing="1.6"'), text(290, 278, "IAM BY DAY / CODE AFTER HOURS", 10, "#829576", "400", 'letter-spacing="1.4"')]
    for i, h in enumerate([8,18,12,28,19,35,15,23,11]):
        parts.append(f'<rect class="eq" style="animation-delay:-{i*.14:.2f}s" x="{1010+i*10}" y="{284-h}" width="4" height="{h}" rx="2" fill="#b9ef5b" opacity=".7"/>')
    return "".join(parts) + "</svg>\n"


def dashboard_svg(data):
    m = data["metrics"]
    desc = f"{m['public_repos']} public repositories, {m['languages']} primary languages, {m['commits_30d']} authored commits and {m['active_days_30d']} active days in the last 30 days. Public owned default branches; forks excluded from commit counts."
    parts = [svg_open(1200, 670, "Sharanya's build signal dashboard", desc), '<rect x="1" y="1" width="1198" height="668" rx="22" fill="#0c110d" stroke="#35452c"/>', text(36, 38, "BUILD SIGNAL", 13, "#b9ef5b", "700", 'letter-spacing="3"'), text(36, 68, "Small commits. Long-term side quests.", 26, "#eff5e7", "600"), text(1160, 38, "PUBLIC GITHUB / 30-DAY WINDOW", 10, "#879c77", extra='text-anchor="end" letter-spacing="1.5"')]
    stamp = parse_time(data["generated_at"]).astimezone(ZoneInfo(data["timezone"]))
    parts.append(text(1160, 67, "SIGNAL CHANGED " + stamp.strftime("%d %b %Y, %H:%M IST").upper(), 10, "#728569", extra='text-anchor="end"'))
    tiles = [("PUBLIC REPOS", m["public_repos"], f"{m['original_repos']} original + profile / forks included"), ("LANGUAGES", m["languages"], "Primary language across original repos"), ("AUTHORED COMMITS / 30D", m["commits_30d"], "Owned public default branches"), ("BUILD DAYS / 30D", m["active_days_30d"], "Days with an authored commit · IST")]
    for i, (label, value, note) in enumerate(tiles):
        x = 36 + i*287
        parts.extend([f'<rect x="{x}" y="94" width="267" height="133" rx="12" fill="#172014" stroke="#2e3e26"/>', text(x+18, 119, label, 9, "#93aa81", "600", 'letter-spacing="1.25"'), text(x+18, 179, str(value).zfill(2), 48, "#cff399", "700", 'letter-spacing="-2"'), text(x+18, 207, note, 9, "#819574")])
    parts.extend([text(36, 267, "THE COMMIT PULSE", 11, "#b8ccaa", "700", 'letter-spacing="1.6"'), text(36, 289, "Your authored default-branch commits · last 30 days · IST", 10, "#7d9271"), text(811, 267, "LANGUAGE MIX", 11, "#b8ccaa", "700", 'letter-spacing="1.6"'), text(811, 289, "Original repos by primary language", 10, "#7d9271")])
    peak = max(1, max(d["count"] for d in data["series"]))
    for i, day in enumerate(data["series"]):
        x = 36 + i*23.7
        height = max(4, day["count"]/peak*126)
        parts.extend([f'<rect x="{x:.1f}" y="{439-height:.1f}" width="17" height="{height:.1f}" rx="3" fill="{"#b9ef5b" if day["count"] else "#23331d"}"><title>{day["date"]}: {day["count"]} authored commits</title></rect>'])
    start, end = data["window_start"], data["window_end"]
    parts.extend([text(36, 461, datetime.fromisoformat(start).strftime("%d %b").upper(), 9, "#6e8661"), text(748, 461, datetime.fromisoformat(end).strftime("%d %b").upper(), 9, "#6e8661", extra='text-anchor="end"'), text(748, 318, f"PEAK: {peak if m['commits_30d'] else 0} IN A DAY", 8, "#829b73", extra='text-anchor="end"')])
    langs = list(data["languages"].items())
    max_count = max([n for _, n in langs] or [1])
    for i, (language, count) in enumerate(langs[:6]):
        y=314+i*25
        parts.extend([text(811,y,language,10,"#b4c8a6"), text(1163,y,count,10,"#9daf8d",extra='text-anchor="end"'), f'<rect x="934" y="{y-7}" width="{max(2,count/max_count*190):.1f}" height="5" rx="2" fill="{PALETTE[i]}"/>'])
    parts += [f'<rect x="811" y="454" width="352" height="29" rx="6" fill="#172014"/>', text(824, 473, f"{m['stars']} STARS RECEIVED / {m['forks']} {'FORK' if m['forks']==1 else 'FORKS'} RECEIVED", 9, "#adc694", "600", 'letter-spacing="1"')]
    parts += [f'<line x1="36" y1="494" x2="1163" y2="494" stroke="#2b3c23"/>', text(36, 526, "RECENTLY PUSHED / YOUR REPOSITORY ROTATION", 10, "#9db88a", "700", 'letter-spacing="1.6"')]
    for i, repo in enumerate(data["recent"][:3]):
        x=36+i*384
        title=repo["name"]
        if len(title)>31:title=title[:28]+"..."
        parts.extend([text(x,562,title,13,"#d8ebc7","600"), text(x,587,(repo.get("language") or "No primary language")+" / "+parse_time(repo["pushed_at"]).astimezone(ZoneInfo(data["timezone"])).strftime("%d %b %Y"),10,"#7f9970")])
    parts += [text(36,639,"Real signals, not a skill score. Forks, private repos and automation commits are excluded from the commit pulse.",9,"#738b64"), text(1163,639,"CODE. COMMIT. REPEAT.",9,"#a8c68f",extra='text-anchor="end" letter-spacing="1.3"')]
    return "".join(parts)+"</svg>\n"


def markdown_escape(value):
    return re.sub(r"([\\`*_{}\[\]()#+.!|<>])", r"\\\1", str(value).replace("\n", " ").replace("\r", " "))


def live_block(data):
    m=data["metrics"]
    asset="./assets/dashboard-"+data["asset_hash"][:16]+".svg"
    lines=[START, f'![Build signal: {m["public_repos"]} public repositories, {m["languages"]} languages, {m["commits_30d"]} authored commits and {m["active_days_30d"]} build days in the last 30 days.]({asset})', "", f'**{m["public_repos"]} public repos** · **{m["languages"]} primary languages** · **{m["commits_30d"]} authored commits / 30 days** · **{m["active_days_30d"]} build days / 30 days** · **{m["stars"]} stars received** · **{m["forks"]} forks received**', "", "### Recently in the rotation", "", "| Repository | Primary language | Last push (IST) |", "| :-- | :-- | :-- |"]
    for r in data["recent"]:
        date=parse_time(r["pushed_at"]).astimezone(ZoneInfo(data["timezone"])).strftime("%d %b %Y")
        lines.append(f'| [{markdown_escape(r["name"])}]({r["url"]}) | {markdown_escape(r.get("language") or "—")} | {date} |')
    if data["commits"]:
        lines += ["", "### Fresh from the keyboard", ""]
        for c in data["commits"][:3]:
            date=parse_time(c["authored_at"]).astimezone(ZoneInfo(data["timezone"])).strftime("%d %b")
            lines.append(f'- **{markdown_escape(c["message"])}** — [{markdown_escape(c["repository"].split("/")[1])}@{c["sha"][:7]}]({c["url"]}) · {date}')
    lines += ["", "<details>", "<summary>How the signal works</summary>", "", "The dashboard checks my public repositories every 30 minutes and refreshes after pushes to this profile repository. GitHub scheduling and image caching can add a delay.", "", "Commit counts cover my authored commits on the default branches of owned, non-fork public repositories over 30 calendar days in India time. The pulse is not GitHub's contribution graph or an all-time total. Language counts reflect primary repository languages, not proficiency. Private repository details are never included. Generated bot commits do not count toward the pulse.", "", "The dashboard only commits when its data or daily window changes. The timestamp shows when the displayed signal changed, not when a visitor last opened the page.", "", "</details>", END]
    return "\n".join(lines)


def write_dashboard(root: Path, source: dict):
    data=summarize(source)
    state=root/"data/profile.json"
    previous=json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
    data["generated_at"]=previous.get("generated_at",source["captured_at"]) if previous.get("fingerprint")==data["fingerprint"] else source["captured_at"]
    path=root/"README.md"
    readme=path.read_text(encoding="utf-8")
    if readme.count(START)!=1 or readme.count(END)!=1 or readme.index(START)>readme.index(END):
        raise ValueError("README must contain exactly one ordered PROFILE:LIVE marker pair")
    data["asset_hash"]=sha256(dashboard_svg(data).encode()).hexdigest()
    new=readme.split(START)[0]+live_block(data)+readme.split(END)[1]
    # All collection/validation finishes before any generated file is replaced.
    outputs={root/"assets/header.svg":header_svg(), root/f'assets/dashboard-{data["asset_hash"][:16]}.svg':dashboard_svg(data), state:json.dumps(data,indent=2,ensure_ascii=False)+"\n", path:new}
    for dest,content in outputs.items():
        dest.parent.mkdir(parents=True,exist_ok=True)
        if not dest.exists() or dest.read_text(encoding="utf-8")!=content:
            temp=dest.with_name(dest.name+".tmp")
            temp.write_text(content,encoding="utf-8")
            temp.replace(dest)
    for asset in (root/"assets").glob("dashboard-*.svg"):
        if re.fullmatch(r"dashboard-[a-f0-9]{16}\.svg",asset.name) and asset not in outputs:
            asset.unlink()
    return data


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--owner",default="sharanyapal")
    parser.add_argument("--timezone",default="Asia/Kolkata")
    parser.add_argument("--from-snapshot",type=Path)
    parser.add_argument("--root",type=Path,default=ROOT)
    args=parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]+",args.owner):
        parser.error("Invalid GitHub username")
    source=json.loads(args.from_snapshot.read_text(encoding="utf-8")) if args.from_snapshot else collect(Client(os.environ.get("GH_TOKEN")),args.owner,datetime.now(timezone.utc),args.timezone)
    if source.get("owner")!=args.owner:
        parser.error("Snapshot owner differs from the requested account")
    data=write_dashboard(args.root,source)
    print(json.dumps({"metrics":data["metrics"],"signal_changed_at":data["generated_at"],"fingerprint":data["fingerprint"][:16]}))


if __name__=="__main__":
    main()
