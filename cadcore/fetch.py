# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetch robot descriptions and CAD files from public GitHub repositories, for the user and the assistant.

Deliberately narrow:
- only https://github.com/<owner>/<repo>[/tree|blob/<ref>/<path>] URLs;
- two steps: `browse` lists the repository's URDF, xacro and STEP files without downloading their
  contents; `get` downloads only the folder (ROS package) that holds the chosen file, plus packages
  it refers to by name;
- git runs with hooks and symlinks disabled and no credential prompts; nothing from the download is
  ever executed;
- a repository's licence (or its absence) is always reported, so the user knows whether they may
  share what they build from it.

Files go to ~/Documents/AI-CAD-Downloads/<owner>-<repo> (AI_CAD_DOWNLOADS overrides).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

URL_RE = re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/(?:tree|blob)/([^/]+)(?:/(.*))?)?/?$")
ROBOT_EXT = (".urdf", ".xacro")
CAD_EXT = (".step", ".stp")
MESH_EXT = (".stl", ".dae", ".obj", ".ply")
MAX_MB = 400
TIMEOUT = 300


class FetchError(Exception):
    pass


def downloads_dir() -> Path:
    return Path(os.environ.get("AI_CAD_DOWNLOADS") or Path.home() / "Documents" / "AI-CAD-Downloads").expanduser()


def parse_url(url: str) -> dict[str, str | None]:
    m = URL_RE.match(url.strip())
    if not m:
        raise FetchError("only GitHub repository links are supported, like https://github.com/owner/repo "
                         "or https://github.com/owner/repo/tree/main/some/folder")
    owner, repo, ref, path = m.groups()
    return {"owner": owner, "repo": repo, "ref": ref, "path": (path or "").strip("/") or None}


def _git(*args: str, cwd: Path | None = None) -> str:
    if not shutil.which("git"):
        raise FetchError("git is not installed (sudo apt install git)")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.symlinks=false", "-c", "protocol.file.allow=never", *args]
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise FetchError("GitHub download timed out") from e
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise FetchError(f"git failed: {msg[-1] if msg else 'unknown error'}")
    return r.stdout


def _checkout(owner: str, repo: str, ref: str | None) -> Path:
    """Blob-less, sparse clone (trees only, no file contents yet). Reused if already present."""
    dest = downloads_dir() / f"{owner}-{repo}"
    if not (dest / ".git").exists():
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--depth", "1", "--filter=blob:none", "--no-checkout"]
        if ref:
            args += ["--branch", ref]
        _git(*args, f"https://github.com/{owner}/{repo}.git", str(dest))
        _git("sparse-checkout", "init", "--cone", cwd=dest)
        _git("checkout", cwd=dest)  # root files only (README, licence, package lists)
    return dest


def _tree(dest: Path) -> list[str]:
    return [p for p in _git("ls-tree", "-r", "--name-only", "HEAD", cwd=dest).splitlines() if p]


def _licence(files: list[str], folder: str | None = None) -> str | None:
    pat = re.compile(r"^(LICEN[CS]E|COPYING)(\.[A-Za-z]+)?$", re.I)
    for f in files:
        p = PurePosixPath(f)
        if pat.match(p.name) and (str(p.parent) == "." or (folder and str(p.parent) == folder)):
            return f
    return None


def _licence_note(dest: Path, lic: str | None) -> str:
    if not lic:
        return ("no licence file: the author keeps all rights. Fine for personal use and simulation; "
                "don't redistribute these files or models built from them without permission.")
    text = (dest / lic).read_text(errors="ignore")[:3000] if (dest / lic).exists() else ""
    for name, pat in [("MIT", r"MIT License|Permission is hereby granted, free of charge"), ("Apache-2.0", r"Apache License"),
                      ("BSD", r"Redistribution and use in source and binary forms"), ("GPL", r"GNU GENERAL PUBLIC LICENSE"),
                      ("LGPL", r"GNU LESSER GENERAL PUBLIC LICENSE"), ("CC", r"Creative Commons")]:
        if re.search(pat, text, re.I):
            return f"licence: {name} ({lic}); follow its terms if you share models built from it."
    return f"licence file {lic}; read it before sharing models built from it."


def browse(url: str) -> dict[str, Any]:
    """List the robot description (URDF/xacro) and STEP files in a repository, without downloading them."""
    u = parse_url(url)
    dest = _checkout(u["owner"], u["repo"], u["ref"])
    files = _tree(dest)
    scope = u["path"] or ""
    inside = [f for f in files if not scope or f == scope or f.startswith(scope + "/")]
    robots = [f for f in inside if f.lower().endswith(ROBOT_EXT)]
    # likely entry points first: files no other xacro includes are top-level; urdf/ folders; shorter paths
    robots.sort(key=lambda f: ("include" in f.lower() or "macro" in f.lower(), "/urdf/" not in f, f.count("/"), f))
    cad = [f for f in inside if f.lower().endswith(CAD_EXT)]
    packages = sorted({str(PurePosixPath(f).parent) for f in files if PurePosixPath(f).name == "package.xml"})
    lic = _licence(files)
    return {"ok": True, "repository": f"{u['owner']}/{u['repo']}", "local_folder": str(dest),
            "robot_files": robots[:60], "step_files": cad[:60], "mesh_files": sum(f.lower().endswith(MESH_EXT) for f in inside),
            "ros_packages": packages[:60], "licence": _licence_note(dest, lic),
            "message": f"{len(robots)} URDF/xacro and {len(cad)} STEP file(s) found; call get with one of them to download it"}


def get(url: str, path: str | None = None) -> dict[str, Any]:
    """Download the folder holding `path` (its ROS package when there is one, so meshes come along)."""
    u = parse_url(url)
    path = (path or u["path"] or "").strip("/")
    if not path:
        raise FetchError("say which file to download (use browse to list them)")
    dest = _checkout(u["owner"], u["repo"], u["ref"])
    files = _tree(dest)
    if path not in files:
        near = [f for f in files if f.endswith("/" + PurePosixPath(path).name) or f == PurePosixPath(path).name]
        if len(near) == 1:
            path = near[0]
        else:
            raise FetchError(f"{path} is not in {u['owner']}/{u['repo']}" + (f"; did you mean {', '.join(near[:3])}?" if near else ""))
    pkg_dirs = {str(PurePosixPath(f).parent): f for f in files if PurePosixPath(f).name == "package.xml"}
    folder = _package_of(path, pkg_dirs) or str(PurePosixPath(path).parent.parent if PurePosixPath(path).parent.name in ("urdf", "robots", "xacro") else PurePosixPath(path).parent)
    folders = {folder if folder != "." else ""}
    _sparse(dest, folders)
    # packages referenced by name ($(find x), package://x/...) that live in the same repository
    for _ in range(3):
        wanted = _referenced_packages(dest, folders) - {PurePosixPath(f).name for f in folders}
        extra = {d for d in pkg_dirs if PurePosixPath(d).name in wanted} - folders
        if not extra:
            break
        folders |= extra
        _sparse(dest, folders)
    size = sum(p.stat().st_size for d in folders for p in (dest / d).rglob("*") if p.is_file()) / 1e6
    if size > MAX_MB:
        raise FetchError(f"the selected folders are {size:.0f} MB, over the {MAX_MB} MB limit")
    lfs = [str(p.relative_to(dest)) for d in folders for p in (dest / d).rglob("*")
           if p.is_file() and p.suffix.lower() in MESH_EXT and p.stat().st_size < 300 and p.read_bytes().startswith(b"version https://git-lfs")]
    lic = _licence(files, folder)
    # entry points: robot files that no other downloaded xacro includes
    robots = [p for d in folders for p in (dest / d).rglob("*") if p.is_file() and p.name.lower().endswith(ROBOT_EXT)]
    included = {m for p in robots for m in re.findall(r"""<xacro:include[^>]*filename=["'][^"']*?([^/"']+)["']""", p.read_text(errors="ignore"))}
    entry = sorted(str(p) for p in robots if p.name not in included and "gazebo" not in p.name.lower())
    out = {"ok": True, "file": str(dest / path), "entry_points": entry[:20], "folders": sorted(str(dest / d) for d in folders), "size_mb": round(size, 1),
           "licence": _licence_note(dest, lic),
           "message": f"downloaded {path} ({size:.1f} MB); import it with assembly import_urdf" if path.lower().endswith(ROBOT_EXT)
           else f"downloaded {path} ({size:.1f} MB)"}
    if lfs:
        out["warning"] = (f"{len(lfs)} mesh file(s) are stored with Git LFS and weren't downloaded (install git-lfs and run "
                          f"'git lfs pull' in {dest}); those links will be reported as missing")
    return out


def _package_of(path: str, pkg_dirs: dict[str, str]) -> str | None:
    p = PurePosixPath(path).parent
    while True:
        if str(p) in pkg_dirs:
            return str(p)
        if str(p) in (".", ""):
            return None
        p = p.parent


def _sparse(dest: Path, folders: set[str]) -> None:
    dirs = sorted(f for f in folders if f)
    if dirs:
        _git("sparse-checkout", "set", *dirs, cwd=dest)
    else:
        _git("sparse-checkout", "disable", cwd=dest)


def _referenced_packages(dest: Path, folders: set[str]) -> set[str]:
    names: set[str] = set()
    for d in folders:
        for p in (dest / d).rglob("*"):
            if p.is_file() and p.suffix.lower() in (".urdf", ".xacro", ".launch", ".xml") and p.stat().st_size < 5_000_000:
                text = p.read_text(errors="ignore")
                names |= set(re.findall(r"\$\(find\s+([A-Za-z0-9_]+)\)", text)) | set(re.findall(r"package://([A-Za-z0-9_]+)/", text))
    return names
