# SPDX-License-Identifier: AGPL-3.0-or-later
"""3D printing: printer, material and quality profiles, and slicing to G-code.

The slicing itself is done by the PrusaSlicer engine (AGPL-3.0, run as a separate program through its command
line). This module finds the engine, reads its vendor profile bundles (about 35 vendors, 1200 printers),
resolves preset inheritance and the "compatible printers/prints" rules the way PrusaSlicer does, writes one flat
config for the chosen printer + quality + material + the user's overrides, runs the engine, and reads the
G-code back (statistics and toolpaths for the preview).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

KINDS = ("printer", "print", "filament")
_META = {"inherits", "renamed_from", "alias", "compatible_printers", "compatible_printers_condition",
         "compatible_prints", "compatible_prints_condition"}
LOCAL_ROOT = Path(os.environ.get("AI_CAD_SLICER_ROOT", Path.home() / ".local/share/ai-cad/slicer-root"))
INSTALL_HINT = ("The slicer engine (PrusaSlicer) is not installed. Install it with 'sudo apt install prusa-slicer' "
                "(Debian/Ubuntu) or 'flatpak install flathub com.prusa3d.PrusaSlicer', or use Install engine, "
                "which unpacks it for this user without admin rights.")


class SlicerError(Exception):
    pass


# ---------------- engine ----------------

@dataclass
class Engine:
    command: list[str]
    profiles: Path
    env: dict[str, str] = field(default_factory=dict)
    version: str = ""
    source: str = ""

    def run(self, args: list[str], timeout: float = 900, cwd: str | None = None,
            on_line: Callable[[str], None] | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, **self.env}
        env.pop("DISPLAY", None)  # the command line needs no window; a stale display only slows startup
        env.pop("WAYLAND_DISPLAY", None)
        p = subprocess.Popen([*self.command, *args], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace")
        lines: list[str] = []
        timer = threading.Timer(timeout, p.kill)
        timer.start()
        try:
            assert p.stdout
            for line in p.stdout:
                lines.append(line)
                if on_line:
                    on_line(line.rstrip())
            p.wait()
        finally:
            timer.cancel()
        return subprocess.CompletedProcess(p.args, p.returncode, "".join(lines), "")


def _local_env(root: Path) -> dict[str, str]:
    dirs = sorted({str(p.parent) for p in root.rglob("*.so*")})
    old = os.environ.get("LD_LIBRARY_PATH", "")
    return {"LD_LIBRARY_PATH": ":".join(dirs + ([old] if old else []))}


def _profiles_near(binary: Path) -> Path | None:
    for base in (binary.resolve().parent.parent, binary.parent.parent, Path("/usr"), Path("/usr/local")):
        for sub in ("share/PrusaSlicer/profiles", "share/prusa-slicer/profiles", "resources/profiles"):
            if (base / sub).is_dir():
                return base / sub
    return None


_ENGINE: dict[str, Any] = {}


def find_engine(refresh: bool = False) -> Engine | None:
    """The first working PrusaSlicer: $AI_CAD_SLICER, the per-user unpacked copy, PATH, then Flatpak."""
    if _ENGINE and not refresh:
        return _ENGINE.get("engine")
    candidates: list[tuple[str, list[str], Path | None, dict[str, str]]] = []
    if os.environ.get("AI_CAD_SLICER"):
        b = Path(os.environ["AI_CAD_SLICER"])
        candidates.append(("AI_CAD_SLICER", [str(b)], _profiles_near(b), {}))
    local = LOCAL_ROOT / "usr/bin/prusa-slicer"
    if local.exists():
        candidates.append(("unpacked for this user", [str(local)], LOCAL_ROOT / "usr/share/PrusaSlicer/profiles", _local_env(LOCAL_ROOT)))
    for name in ("prusa-slicer", "PrusaSlicer"):
        w = shutil.which(name)
        if w:
            candidates.append(("system", [w], _profiles_near(Path(w)), {}))
    if shutil.which("flatpak"):
        for base in (Path("/var/lib/flatpak"), Path.home() / ".local/share/flatpak"):
            prof = base / "app/com.prusa3d.PrusaSlicer/current/active/files/share/PrusaSlicer/profiles"
            if prof.is_dir():
                candidates.append(("flatpak", ["flatpak", "run", "--command=prusa-slicer", "com.prusa3d.PrusaSlicer"], prof, {}))
    engine = None
    for source, cmd, prof, env in candidates:
        if not prof or not prof.is_dir():
            continue
        e = Engine(cmd, prof, env, source=source)
        try:
            out = e.run(["--help"], timeout=60).stdout
        except OSError:
            continue
        m = re.search(r"PrusaSlicer-([0-9][^\s]*)", out)
        if m:
            e.version = m.group(1).split("+")[0]
            engine = e
            break
    _ENGINE.clear()
    _ENGINE["engine"] = engine
    return engine


def engine_status() -> dict[str, Any]:
    e = find_engine()
    if not e:
        return {"found": False, "message": INSTALL_HINT, "can_install": bool(shutil.which("apt-get") and shutil.which("dpkg"))}
    return {"found": True, "version": e.version, "source": e.source, "profiles": str(e.profiles)}


def install_engine(progress: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    """Unpack the distribution's PrusaSlicer package and its missing libraries into the user's data folder.
    No admin rights: 'apt-get download' only fetches the .deb files, 'dpkg -x' only extracts them."""
    if not (shutil.which("apt-get") and shutil.which("dpkg")):
        raise SlicerError("automatic install needs apt-get (Debian/Ubuntu). " + INSTALL_HINT)
    sim = subprocess.run(["apt-get", "install", "--simulate", "prusa-slicer"], capture_output=True, text=True)
    pkgs = re.findall(r"^Inst (\S+)", sim.stdout, re.M)
    if not pkgs:
        if "is already the newest" in sim.stdout:
            find_engine(refresh=True)
            return engine_status()
        raise SlicerError("this system's package lists have no prusa-slicer package. " + INSTALL_HINT)
    debs = LOCAL_ROOT.parent / "slicer-debs"
    debs.mkdir(parents=True, exist_ok=True)
    progress(f"downloading {len(pkgs)} packages")
    r = subprocess.run(["apt-get", "download", *pkgs], cwd=debs, capture_output=True, text=True)
    if r.returncode:
        raise SlicerError(f"download failed: {(r.stderr or r.stdout).strip()[-400:]}")
    LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
    files = sorted(debs.glob("*.deb"))
    for i, f in enumerate(files):
        progress(f"unpacking {i + 1}/{len(files)}: {f.name}")
        subprocess.run(["dpkg", "-x", str(f), str(LOCAL_ROOT)], check=True)
    e = find_engine(refresh=True)
    if not e:
        raise SlicerError("the engine was unpacked but does not start; install it with 'sudo apt install prusa-slicer'")
    return engine_status()


# ---------------- profiles ----------------

def _read_ini(path: Path) -> list[tuple[str, dict[str, str]]]:
    sections: list[tuple[str, dict[str, str]]] = []
    cur: dict[str, str] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        if s.startswith("[") and s.endswith("]"):
            cur = {}
            sections.append((s[1:-1], cur))
        elif cur is not None and "=" in s:
            k, v = s.split("=", 1)
            cur[k.strip()] = v.strip()
    return sections


@dataclass
class Vendor:
    id: str
    name: str
    folder: Path
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    presets: dict[str, dict[str, dict[str, str]]] = field(default_factory=lambda: {k: {} for k in KINDS})
    _resolved: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)

    def resolve(self, kind: str, name: str, _chain: tuple[str, ...] = ()) -> dict[str, str]:
        key = (kind, name)
        if key in self._resolved:
            return self._resolved[key]
        raw = self.presets[kind].get(name)
        if raw is None:
            raise SlicerError(f"{self.name}: no {kind} profile named {name!r}")
        if name in _chain:
            raise SlicerError(f"{self.name}: profile {name!r} inherits from itself")
        out: dict[str, str] = {}
        for parent in _split_list(raw.get("inherits", "")):
            out.update(self.resolve(kind, parent, (*_chain, name)))
        out.update(raw)
        out.pop("inherits", None)
        self._resolved[key] = out
        return out


def _split_list(v: str) -> list[str]:
    """'a; b' or '"a";"b"' -> ['a', 'b']."""
    parts = [p.strip() for p in v.split(";")]
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts if p]


class Profiles:
    def __init__(self, folder: Path):
        self.folder = folder
        self.vendors: dict[str, Vendor] = {}
        for ini in sorted(folder.glob("*.ini")):
            v = self._load(ini)
            if v:
                self.vendors[v.id] = v

    @staticmethod
    def _load(ini: Path) -> Vendor | None:
        secs = _read_ini(ini)
        head = next((d for s, d in secs if s == "vendor"), {})
        v = Vendor(ini.stem, head.get("name", ini.stem), ini.parent / ini.stem)
        for sec, d in secs:
            kind, _, name = sec.partition(":")
            if kind == "printer_model":
                if d.get("technology", "FFF") != "FFF":
                    continue
                thumb = v.folder / f"{name}_thumbnail.png"
                v.models[name] = {"id": name, "name": d.get("name", name), "family": d.get("family", ""),
                                  "variants": _split_list(d.get("variants", "")), "bed_model": d.get("bed_model", ""),
                                  "bed_texture": d.get("bed_texture", ""), "thumbnail": thumb.name if thumb.exists() else "",
                                  "default_materials": _split_list(d.get("default_materials", ""))}
            elif kind in KINDS and name:
                v.presets[kind][name] = d
        return v

    # ----- printers -----

    def printers(self) -> list[dict[str, Any]]:
        """Every concrete FFF printer: vendor, model, nozzle variant, bed."""
        out = []
        for v in self.vendors.values():
            for name in v.presets["printer"]:
                if name.startswith("*"):
                    continue
                cfg = v.resolve("printer", name)
                if cfg.get("printer_technology", "FFF") != "FFF":
                    continue
                model = v.models.get(cfg.get("printer_model", ""), {})
                out.append({"vendor": v.id, "vendor_name": v.name, "name": name, "model": model.get("id", ""),
                            "model_name": model.get("name", name), "family": model.get("family", ""),
                            "variant": cfg.get("printer_variant", ""), "nozzle": _num_list(cfg.get("nozzle_diameter", "0.4"))[0],
                            "thumbnail": model.get("thumbnail", ""), **bed_of(cfg)})
        return out

    def find(self, kind: str, name: str, vendor: str | None = None) -> tuple[Vendor, dict[str, str]]:
        for v in ([self.vendors[vendor]] if vendor in self.vendors else self.vendors.values()):
            if name in v.presets[kind] and not name.startswith("*"):
                return v, v.resolve(kind, name)
        raise SlicerError(f"no {kind} profile named {name!r}")

    def printer(self, name: str) -> tuple[Vendor, dict[str, str]]:
        custom = custom_printers().get(name)
        if custom:
            return self.vendors.get("Templates") or next(iter(self.vendors.values())), custom_printer_config(custom)
        return self.find("printer", name)

    # ----- presets for a printer -----

    def prints_for(self, printer: str) -> list[dict[str, Any]]:
        v, pcfg = self.printer(printer)
        pcfg = {**pcfg, "printer_preset": printer}
        out = []
        for vend in self._search_vendors(v):
            for name in vend.presets["print"]:
                if name.startswith("*"):
                    continue
                cfg = vend.resolve("print", name)
                if compatible(cfg, printer, pcfg):
                    out.append({"name": name, "layer_height": _num(cfg.get("layer_height", "0.2")), "vendor": vend.id})
        if not out and "PrusaResearch" in self.vendors:  # custom (or orphaned) printer: generic qualities for its nozzle
            out = self._generic_prints(pcfg)
        out.sort(key=lambda p: (p["layer_height"], p["name"]))
        return out

    def _generic_prints(self, pcfg: dict[str, str]) -> list[dict[str, Any]]:
        """Quality profiles for a printer without its own: Prusa's MK3 profiles for the same nozzle size (the most
        complete generic set), else for the nearest nozzle size that has some."""
        v = self.vendors["PrusaResearch"]
        nozzle = _num_list(pcfg.get("nozzle_diameter", "0.4"))[0]
        for n in sorted({nozzle, 0.25, 0.4, 0.6, 0.8}, key=lambda x: abs(x - nozzle)):
            proxy = {"printer_notes": "PRINTER_VENDOR_PRUSA3D\\nPRINTER_MODEL_MK3\\n", "printer_model": "MK3S", "nozzle_diameter": f"{n:g}",
                     "printer_variant": f"{n:g}", "single_extruder_multi_material": "0"}
            out = []
            for name in v.presets["print"]:
                if name.startswith("*") or "SOLUBLE" in name:
                    continue
                cfg = v.resolve("print", name)
                if compatible(cfg, "Original Prusa i3 MK3S & MK3S+", proxy):
                    out.append({"name": name, "layer_height": _num(cfg.get("layer_height", "0.2")), "vendor": v.id})
            if out:
                return out
        return []

    def filaments_for(self, printer: str, print_name: str | None = None) -> list[dict[str, Any]]:
        v, pcfg = self.printer(printer)
        pcfg = {**pcfg, "printer_preset": printer}
        prcfg = self.print_config(print_name, v) if print_name else {}
        model = v.models.get(pcfg.get("printer_model", ""), {})
        defaults = model.get("default_materials", [])
        out, seen = [], set()
        for vend in [*self._search_vendors(v), *([self.vendors["Templates"]] if "Templates" in self.vendors and v.id != "Templates" else [])]:
            for name in vend.presets["filament"]:
                if name.startswith("*") or name in seen:
                    continue
                cfg = vend.resolve("filament", name)
                if not compatible(cfg, printer, pcfg):
                    continue
                if prcfg and not compatible(cfg, print_name or "", {**pcfg, **prcfg}, prints=True):
                    continue
                seen.add(name)
                out.append({"name": name, "type": cfg.get("filament_type", "").strip('"'), "vendor": vend.id,
                            "maker": cfg.get("filament_vendor", "").strip('"'), "default": name in defaults,
                            "generic": vend.id == "Templates"})
        rank = {n: i for i, n in enumerate(defaults)}
        out.sort(key=lambda f: (not f["default"], rank.get(f["name"], 0), f["generic"], f["name"].lower()))
        return out

    def print_config(self, name: str, near: Vendor | None = None) -> dict[str, str]:
        if near and name in near.presets["print"]:
            return near.resolve("print", name)
        return self.find("print", name)[1]

    def filament_config(self, name: str, near: Vendor | None = None) -> dict[str, str]:
        if near and name in near.presets["filament"]:
            return near.resolve("filament", name)
        if "Templates" in self.vendors and name in self.vendors["Templates"].presets["filament"]:
            return self.vendors["Templates"].resolve("filament", name)
        return self.find("filament", name)[1]

    def _search_vendors(self, v: Vendor) -> list[Vendor]:
        return [v]

    def defaults(self, printer: str) -> dict[str, str | None]:
        prints = self.prints_for(printer)
        _, pcfg = self.printer(printer)
        nozzle = _num_list(pcfg.get("nozzle_diameter", "0.4"))[0]
        target = round(nozzle / 2, 2)

        def score(p):
            n = p["name"].upper()
            pref = 0 if any(k in n for k in ("QUALITY", "NORMAL", "STANDARD", "OPTIMAL", "DEFAULT")) else 1
            return (abs(p["layer_height"] - target), pref, "SPEED" in n, p["name"])
        pr = min(prints, key=score)["name"] if prints else None
        fils = self.filaments_for(printer, pr)
        fil = (next((f for f in fils if f["default"]), None)  # the printer maker's recommended list first, then plain PLA
               or next((f for f in fils if f["name"].startswith("Generic PLA") and "Silk" not in f["name"]), None)
               or next((f for f in fils if f["type"].upper() == "PLA"), None) or (fils[0] if fils else None))
        return {"print": pr, "filament": fil["name"] if fil else None}

    def config(self, printer: str, print_name: str, filament: str, overrides: dict[str, Any] | None = None) -> dict[str, str]:
        """One flat PrusaSlicer config: printer, then quality, then material, then the user's overrides."""
        v, pcfg = self.printer(printer)
        out: dict[str, str] = {}
        out.update(pcfg)
        out.update(self.print_config(print_name, v))
        out.update(self.filament_config(filament, v))
        for k in _META:
            out.pop(k, None)
        out["printer_settings_id"], out["print_settings_id"], out["filament_settings_id"] = printer, print_name, filament
        for k in ("perimeter_extruder", "infill_extruder", "solid_infill_extruder"):  # 0 = "default" in some bundles;
            if out.get(k, "1").strip() in ("", "0"):  # the GUI turns it into extruder 1, the command line rejects it
                out[k] = "1"
        custom = custom_printers().get(printer)
        if custom and custom.get("heated_bed") is False:  # the material's bed temperature cannot apply
            out["bed_temperature"] = out["first_layer_bed_temperature"] = "0"
        for k, val in (overrides or {}).items():
            if not re.fullmatch(r"[a-z0-9_]+", k):
                raise SlicerError(f"invalid setting name {k!r}")
            out[k] = _ini_value(val)
        return out


def _ini_value(v: Any) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v).replace("\r", "").replace("\n", "\\n")


def bed_of(cfg: dict[str, str]) -> dict[str, Any]:
    pts = []
    for p in cfg.get("bed_shape", "0x0,200x0,200x200,0x200").split(","):
        try:
            x, y = p.split("x")
            pts.append([float(x), float(y)])
        except ValueError:
            continue
    if len(pts) < 3:
        pts = [[0, 0], [200, 0], [200, 200], [0, 200]]
    arr = np.array(pts)
    lo, hi = arr.min(0), arr.max(0)
    return {"bed": {"shape": pts, "min": lo.tolist(), "max": hi.tolist(), "center": ((lo + hi) / 2).tolist(),
                    "height": _num(cfg.get("max_print_height", "200")), "circular": len(pts) > 8}}


def _num(s: Any, default: float = 0.0) -> float:
    try:
        return float(str(s).strip().rstrip("%").split(",")[0].split(";")[0])
    except ValueError:
        return default


def _num_list(s: str) -> list[float]:
    out = []
    for x in re.split(r"[,;]", s or ""):
        try:
            out.append(float(x))
        except ValueError:
            pass
    return out or [0.4]


# ---------------- user (custom) printers ----------------

def user_dir() -> Path:
    return Path(os.environ.get("AI_CAD_CONFIG", Path.home() / ".config/ai-cad/config.json")).parent


def _custom_file() -> Path:
    return user_dir() / "printers.json"


def custom_printers() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(_custom_file().read_text())
    except (OSError, ValueError):
        return {}


def save_custom_printer(p: dict[str, Any]) -> dict[str, Any]:
    name = str(p.get("name", "")).strip()
    if not name:
        raise SlicerError("a custom printer needs a name")
    for k in ("width", "depth", "height", "nozzle"):
        if not isinstance(p.get(k), (int, float)) or p[k] <= 0:
            raise SlicerError(f"{k} must be a positive number")
    allp = custom_printers()
    allp[name] = {k: p.get(k) for k in ("name", "width", "depth", "height", "nozzle", "filament_diameter", "flavor", "heated_bed",
                                         "origin_center", "retract_length", "start_gcode", "end_gcode")}
    user_dir().mkdir(parents=True, exist_ok=True)
    _custom_file().write_text(json.dumps(allp, indent=2))
    return allp[name]


def delete_custom_printer(name: str) -> None:
    allp = custom_printers()
    allp.pop(name, None)
    _custom_file().write_text(json.dumps(allp, indent=2))


DEFAULT_START = ("G90 ; absolute positioning\\nM83 ; relative extrusion\\nM140 S[first_layer_bed_temperature]\\n"
                 "M104 S[first_layer_temperature]\\nG28 ; home\\nM190 S[first_layer_bed_temperature]\\n"
                 "M109 S[first_layer_temperature]\\nG1 Z2 F3000\\nG1 X5 Y20 Z0.3 F5000\\nG1 X5 Y120 E10 F1500 ; prime line\\nG92 E0")
DEFAULT_END = "M104 S0\\nM140 S0\\nM107\\nG91\\nG1 Z10 F600\\nG90\\nG28 X0\\nM84"


def custom_printer_config(p: dict[str, Any]) -> dict[str, str]:
    w, d = float(p["width"]), float(p["depth"])
    x0, y0 = (-w / 2, -d / 2) if p.get("origin_center") else (0, 0)
    noz = float(p["nozzle"])
    return {
        "printer_technology": "FFF", "printer_model": "", "printer_notes": "PRINTER_CUSTOM",
        "bed_shape": f"{x0:g}x{y0:g},{x0 + w:g}x{y0:g},{x0 + w:g}x{y0 + d:g},{x0:g}x{y0 + d:g}",
        "max_print_height": f"{float(p['height']):g}", "nozzle_diameter": f"{noz:g}", "printer_variant": f"{noz:g}",
        "gcode_flavor": p.get("flavor") or "marlin2", "use_relative_e_distances": "1", "layer_gcode": "G92 E0",
        "start_gcode": _ini_value(p.get("start_gcode") or DEFAULT_START.replace("\\n", "\n")),
        "end_gcode": _ini_value(p.get("end_gcode") or DEFAULT_END.replace("\\n", "\n")),
        "retract_length": f"{float(p.get('retract_length') or 5):g}", "retract_speed": "40", "retract_lift": "0.4",
        "min_layer_height": f"{noz * 0.17:.2f}", "max_layer_height": f"{noz * 0.75:.2f}",
        "machine_max_acceleration_extruding": "1000", "machine_max_acceleration_travel": "1000",
        "machine_max_feedrate_x": "300", "machine_max_feedrate_y": "300", "machine_max_feedrate_z": "10",
        "machine_limits_usage": "time_estimate_only", "binary_gcode": "0", "thumbnails": "",
        "filament_diameter": f"{float(p.get('filament_diameter') or 1.75):g}",
    }


# ---------------- compatibility conditions ----------------

def compatible(cfg: dict[str, str], target_name: str, target: dict[str, str], prints: bool = False) -> bool:
    """PrusaSlicer's rule: an explicit list wins; otherwise the condition expression; neither means compatible."""
    lst = _split_list(cfg.get("compatible_prints" if prints else "compatible_printers", ""))
    if lst:
        return target_name in lst
    cond = cfg.get("compatible_prints_condition" if prints else "compatible_printers_condition", "").strip()
    if len(cond) >= 2 and cond[0] == cond[-1] == '"':
        cond = cond[1:-1]
    if not cond:
        return True
    try:
        return bool(evaluate(cond, target))
    except SlicerError:
        return False


_TOKEN = re.compile(r"""\s*(?:
    (?P<num>\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|\.\d+)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<op>==|!=|=~|!~|<=|>=|&&|\|\||[<>()!])
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])?)
)""", re.X)


def _tokens(expr: str) -> list[tuple[str, str]]:
    out, i = [], 0
    while i < len(expr):
        if expr[i].isspace():
            i += 1
            continue
        if expr[i] == "/" and out and out[-1] in (("op", "=~"), ("op", "!~")):
            j = i + 1
            while j < len(expr) and expr[j] != "/":
                j += 2 if expr[j] == "\\" else 1
            out.append(("re", expr[i + 1:j]))
            i = j + 1
            continue
        m = _TOKEN.match(expr, i)
        if not m or m.end() == i:
            raise SlicerError(f"cannot read condition at {expr[i:i + 20]!r}")
        kind = m.lastgroup or ""
        val = m.group(kind)
        if kind == "id" and val in ("and", "or", "not"):
            kind = "op"
        out.append((kind, val))
        i = m.end()
    return out


def _lookup(name: str, cfg: dict[str, str]) -> Any:
    m = re.fullmatch(r"([a-z_0-9]+)(?:\[(\d+)\])?", name, re.I)
    if not m:
        raise SlicerError(f"bad variable {name}")
    key, idx = m.group(1), m.group(2)
    if key in ("true", "false"):
        return key == "true"
    if key == "num_extruders":
        return len(_num_list(cfg.get("nozzle_diameter", "0.4")))
    if key not in cfg:
        raise SlicerError(f"unknown variable {key}")
    v = cfg[key]
    if idx is not None:
        parts = [p.strip().strip('"') for p in re.split(r"[;,]", v)]
        v = parts[int(idx)] if int(idx) < len(parts) else parts[-1]
    return v.strip('"') if isinstance(v, str) else v


def evaluate(expr: str, cfg: dict[str, str]) -> Any:
    toks = _tokens(expr)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else ("end", "")

    def take():
        nonlocal pos
        t = peek()
        pos += 1
        return t

    def atom():
        k, v = take()
        if k == "op" and v == "(":
            r = disj()
            if take() != ("op", ")"):
                raise SlicerError("missing )")
            return r
        if k == "op" and v in ("!", "not"):
            return not _truth(cmp())
        if k == "num":
            return float(v)
        if k == "str":
            return v[1:-1]
        if k == "id":
            return _lookup(v, cfg)
        raise SlicerError(f"unexpected {v!r}")

    def cmp():
        a = atom()
        k, v = peek()
        if k == "op" and v in ("==", "!=", "<", ">", "<=", ">="):
            take()
            b = atom()
            return _compare(a, b, v)
        if k == "op" and v in ("=~", "!~"):
            take()
            rk, rv = take()
            if rk != "re":
                raise SlicerError("regular expression expected")
            hit = re.fullmatch(rv, str(a), re.S) is not None
            return hit if v == "=~" else not hit
        return a

    def conj():
        r = cmp()
        while peek() in (("op", "and"), ("op", "&&")):
            take()
            b = cmp()
            r = _truth(r) and _truth(b)
        return r

    def disj():
        r = conj()
        while peek() in (("op", "or"), ("op", "||")):
            take()
            b = conj()
            r = _truth(r) or _truth(b)
        return r

    r = disj()
    if pos != len(toks):
        raise SlicerError(f"unexpected {toks[pos][1]!r}")
    return r


def _truth(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip() not in ("", "0", "false")
    return bool(v)


def _compare(a: Any, b: Any, op: str) -> bool:
    try:
        x, y = float(a), float(b)
    except (TypeError, ValueError):
        x, y = str(a), str(b)
    return {"==": x == y, "!=": x != y, "<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y}[op]


# ---------------- option help ----------------

_HELP: dict[str, dict[str, str]] = {}


def option_help(engine: Engine) -> dict[str, dict[str, str]]:
    """Descriptions of every print option, from the engine's own --help-fff."""
    if _HELP:
        return _HELP
    text = engine.run(["--help-fff"], timeout=60).stdout
    cur = None
    for line in text.splitlines():
        m = re.match(r"^\s{1,2}--([a-z0-9-]+)(?: ([A-Z]+))?\s*(.*)$", line)
        if m:
            cur = m.group(1).replace("-", "_")
            _HELP[cur] = {"type": (m.group(2) or "bool").lower(), "text": m.group(3).strip()}
        elif cur and line.startswith(" " * 10) and line.strip():
            _HELP[cur]["text"] += " " + line.strip()
        elif not line.strip():
            cur = None
    for k, h in _HELP.items():
        h["text"] = re.sub(r"\s+", " ", h["text"]).strip()
    return _HELP


# ---------------- slicing ----------------

# Per-extruder text options: an empty value must be written as one empty string (""), otherwise the engine reads
# an empty list and fails ("Invalid utf8 sequence") when it looks up the first extruder's entry.
_PER_EXTRUDER_TEXT = {"start_filament_gcode", "end_filament_gcode", "filament_notes", "filament_settings_id", "filament_colour",
                      "extruder_colour", "filament_type", "filament_vendor"}


def write_config(cfg: dict[str, str], path: Path) -> None:
    lines = []
    for k, v in cfg.items():
        if v == "" and k in _PER_EXTRUDER_TEXT:
            v = '""'
        lines.append(f"{k} = {v}\n")
    path.write_text("".join(lines), encoding="utf-8")


def slice_models(engine: Engine, cfg: dict[str, str], models: list[Path], out: Path,
                 progress: Callable[[int, str], None] = lambda p, s: None) -> dict[str, Any]:
    """Run the engine on STL files already placed in bed coordinates; returns statistics and warnings."""
    cfg = dict(cfg)
    cfg["binary_gcode"] = "0"  # plain text G-code: every firmware reads it, and the preview can parse it
    if not cfg.get("thumbnails_format"):
        cfg.pop("thumbnails_format", None)
    cfg["thumbnails"] = ""  # thumbnails need an OpenGL window, which the command line does not have
    ini = out.with_suffix(".ini")
    write_config(cfg, ini)
    warnings: list[str] = []

    def line(s: str):
        m = re.match(r"^\s*(\d+)\s*=>\s*(.*)$", s)
        if m:
            progress(int(m.group(1)), m.group(2))
        elif re.search(r"\b(warning|error)\b", s, re.I):
            warnings.append(re.sub(r"^(\[[^\]]*\]\s*){2}\[(\w+)\]\s*", r"\2: ", s.strip()))  # drop time and thread

    r = engine.run(["--export-gcode", "--load", str(ini), "--dont-arrange", "--output", str(out), *map(str, models)],
                   cwd=str(out.parent), on_line=line)
    if r.returncode != 0 or not out.exists():
        tail = [x for x in r.stdout.strip().splitlines() if x.strip()][-6:]
        raise SlicerError("slicing failed: " + (" | ".join(tail) or f"exit code {r.returncode}"))
    stats = gcode_stats(out)
    warnings = [w for w in warnings if "thumbnail" not in w.lower()]
    with open(out, "rb") as fh:  # the engine writes custom G-code (start/end/filament) errors into the file itself
        for raw in fh:
            if raw.startswith(b"!!!!! Failed to process the custom G-code template"):
                warnings.append(f"custom G-code error in {raw.decode(errors='replace').split()[-1]}: check the printer's start/end G-code")
    return {"stats": stats, "warnings": warnings[:20]}


def gcode_stats(path: Path) -> dict[str, Any]:
    """Time, filament and layer count from the comment block the engine writes at the end of the file."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 60000))
        tail = fh.read().decode("utf-8", "replace")
    def grab(pattern: str) -> str | None:
        m = re.search(pattern, tail, re.M)
        return m.group(1).strip() if m else None
    t = grab(r"^; estimated printing time \(normal mode\) = (.*)$")
    out: dict[str, Any] = {
        "time": t, "time_s": _duration(t) if t else None,
        "filament_mm": _num(grab(r"^; filament used \[mm\] = (.*)$") or 0),
        "filament_cm3": _num(grab(r"^; filament used \[cm3\] = (.*)$") or 0),
        "filament_g": _num(grab(r"^; (?:total )?filament used \[g\] = (.*)$") or 0),
        "cost": _num(grab(r"^; (?:total )?filament cost = (.*)$") or 0),
        "size_bytes": size,
    }
    layers = 0
    with open(path, "rb") as fh:
        for raw in fh:
            if raw.startswith(b";LAYER_CHANGE"):
                layers += 1
    out["layers"] = layers
    return out


def _duration(s: str) -> float:
    total = 0.0
    for n, u in re.findall(r"(\d+)\s*([dhms])", s):
        total += int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u]
    return total


# ---------------- G-code toolpaths (preview) ----------------

TYPES = ["Perimeter", "External perimeter", "Overhang perimeter", "Internal infill", "Solid infill", "Top solid infill",
         "Ironing", "Bridge infill", "Gap fill", "Skirt/Brim", "Support material", "Support material interface",
         "Wipe tower", "Custom", "Travel"]
_TYPE_ID = {t: i for i, t in enumerate(TYPES)}


def toolpaths(path: Path, max_segments: int = 4_000_000, travel: bool = False) -> dict[str, Any]:
    """Extrusion segments of a G-code file, per layer: positions (float32, 6 per segment), type ids (uint8),
    and the first segment index of every layer. Arcs (G2/G3) are split into short chords."""
    segs: list[tuple[float, float, float, float, float, float, int]] = []
    layer_start: list[int] = []
    layer_z: list[float] = []
    x = y = z = e = 0.0
    abs_e = True
    abs_xyz = True
    cur_type = _TYPE_ID["Custom"]
    pending_layer = False
    word = re.compile(rb"([XYZEIJF])(-?\d*\.?\d+)")
    with open(path, "rb") as fh:
        for raw in fh:
            if raw[:1] == b";":
                if raw.startswith(b";TYPE:"):
                    cur_type = _TYPE_ID.get(raw[6:].decode(errors="replace").strip(), _TYPE_ID["Custom"])
                elif raw.startswith(b";LAYER_CHANGE"):
                    pending_layer = True
                elif raw.startswith(b";Z:") and pending_layer:
                    layer_z.append(_num(raw[3:].decode()))
                    layer_start.append(len(segs))
                    pending_layer = False
                continue
            line = raw.split(b";", 1)[0].strip()
            if not line:
                continue
            cmd = line.split(None, 1)[0].upper()
            if cmd in (b"G0", b"G1", b"G2", b"G3"):
                w = {k.decode(): float(v) for k, v in word.findall(line.upper())}
                nx = (w["X"] if abs_xyz else x + w["X"]) if "X" in w else x
                ny = (w["Y"] if abs_xyz else y + w["Y"]) if "Y" in w else y
                nz = (w["Z"] if abs_xyz else z + w["Z"]) if "Z" in w else z
                de = 0.0
                if "E" in w:
                    de = w["E"] - e if abs_e else w["E"]
                    e = w["E"] if abs_e else e + w["E"]
                extruding = de > 1e-9
                if (extruding or travel) and (nx != x or ny != y) and len(segs) < max_segments:
                    t = cur_type if extruding else _TYPE_ID["Travel"]
                    if cmd in (b"G2", b"G3") and ("I" in w or "J" in w):
                        for a, b in _arc(x, y, nx, ny, w.get("I", 0.0), w.get("J", 0.0), cmd == b"G2"):
                            segs.append((a[0], a[1], z, b[0], b[1], nz, t))
                    else:
                        segs.append((x, y, z, nx, ny, nz, t))
                x, y, z = nx, ny, nz
            elif cmd == b"G92":
                w = {k.decode(): float(v) for k, v in word.findall(line.upper())}
                if "E" in w:
                    e = w["E"]
            elif cmd == b"M82":
                abs_e = True
            elif cmd == b"M83":
                abs_e = False
            elif cmd == b"G90":
                abs_xyz = True
            elif cmd == b"G91":
                abs_xyz = False
    arr = np.asarray(segs, dtype=np.float32).reshape(-1, 7)
    return {"positions": arr[:, :6].copy(), "types": arr[:, 6].astype(np.uint8), "layer_start": layer_start,
            "layer_z": layer_z, "truncated": len(segs) >= max_segments}


def _arc(x0, y0, x1, y1, i, j, cw):
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    if cw:
        while a1 >= a0:
            a1 -= 2 * math.pi
    else:
        while a1 <= a0:
            a1 += 2 * math.pi
    n = max(2, int(abs(a1 - a0) * r / 0.5) + 1)
    pts = [(cx + r * math.cos(a0 + (a1 - a0) * k / n), cy + r * math.sin(a0 + (a1 - a0) * k / n)) for k in range(n + 1)]
    pts[-1] = (x1, y1)
    return list(zip(pts[:-1], pts[1:]))


# ---------------- sending to a printer ----------------

def upload(host: dict[str, Any], gcode: Path, name: str, start: bool = False) -> str:
    """Send G-code to a network printer: OctoPrint, Moonraker (Klipper) or PrusaLink."""
    import httpx

    kind = host.get("type")
    url = str(host.get("url", "")).rstrip("/")
    if not re.match(r"^https?://", url):
        raise SlicerError("printer address must start with http:// or https://")
    key = str(host.get("api_key", ""))
    data = gcode.read_bytes()
    try:
        if kind == "octoprint":
            r = httpx.post(f"{url}/api/files/local", headers={"X-Api-Key": key},
                           files={"file": (name, data, "application/octet-stream")},
                           data={"select": "true", "print": "true" if start else "false"}, timeout=120)
        elif kind == "moonraker":
            r = httpx.post(f"{url}/server/files/upload", headers={"X-Api-Key": key} if key else {},
                           files={"file": (name, data, "application/octet-stream")},
                           data={"print": "true" if start else "false"}, timeout=120)
        elif kind == "prusalink":
            r = httpx.put(f"{url}/api/v1/files/usb/{name}", content=data, timeout=120,
                          headers={"X-Api-Key": key, "Content-Type": "text/x.gcode", "Overwrite": "?1",
                                   "Print-After-Upload": "?1" if start else "?0"})
        else:
            raise SlicerError("printer type must be octoprint, moonraker or prusalink")
    except httpx.HTTPError as e:
        raise SlicerError(f"could not reach the printer at {url}: {e}") from e
    if r.status_code >= 300:
        raise SlicerError(f"the printer refused the upload (HTTP {r.status_code}): {r.text[:200]}")
    return f"sent {name} to {url}" + (" and started the print" if start else "")


# ---------------- cached profile set ----------------

_PROFILES: dict[str, Profiles] = {}


def profiles(engine: Engine | None = None) -> Profiles:
    engine = engine or find_engine()
    if not engine:
        raise SlicerError(INSTALL_HINT)
    key = str(engine.profiles)
    if key not in _PROFILES:
        _PROFILES[key] = Profiles(engine.profiles)
    return _PROFILES[key]
