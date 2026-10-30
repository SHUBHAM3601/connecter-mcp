"""Run MAXScript for the 3ds Max tools.

Every 3ds Max tool builds one MAXScript and runs it one of two ways:

* ``mode="script"`` (default): nothing is started. The script is returned so it can be run in the
  open 3ds Max (for example with a 3ds Max MCP's execute_maxscript). It evaluates to a JSON string.
* ``mode="batch"``: the script runs headless in 3dsmaxbatch.exe at below-normal priority. It is
  refused while 3ds Max is busy (e.g. rendering) unless ``force=True``.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import winreg
from pathlib import Path

import psutil

from .client import ConnecterError
from .config import Settings

MODES = ("script", "batch")

# A tiny JSON writer, so every script returns structured results.
PRELUDE = r'''
global mcpOutFile
struct mcpRaw (s)
fn mcpEsc s = (
    s = s as string
    s = substituteString s "\\" "\\\\"
    s = substituteString s "\"" "\\\""
    s = substituteString s "\n" "\\n"
    s = substituteString s "\r" ""
    substituteString s "\t" " "
)
fn mcpJ v = (
    case of (
        (v == undefined): "null"
        (isStruct v): v.s
        (classOf v == BooleanClass): (if v then "true" else "false")
        (isKindOf v Float): (formattedPrint v format:".4f")
        (isKindOf v Number): (v as string)
        (isKindOf v Point3): ("[" + mcpJ v.x + "," + mcpJ v.y + "," + mcpJ v.z + "]")
        (isKindOf v Array): (
            local t = "["
            for i = 1 to v.count do (if i > 1 do t += ","; t += mcpJ v[i])
            t + "]")
        default: ("\"" + mcpEsc v + "\"")
    )
)
fn mcpO pairs = (
    local t = "{"
    for i = 1 to pairs.count do (if i > 1 do t += ","; t += "\"" + mcpEsc pairs[i][1] + "\":" + mcpJ pairs[i][2])
    mcpRaw (t + "}")
)
fn mcpToCm v = v * units.SystemScale * (case units.SystemType of (
    #inches: 2.54; #feet: 30.48; #miles: 160934.4; #millimeters: 0.1
    #centimeters: 1.0; #meters: 100.0; #kilometers: 100000.0; default: 1.0))
fn mcpBBox nodes = (
    local lo = [1e9, 1e9, 1e9], hi = [-1e9, -1e9, -1e9]
    for o in nodes where isValidNode o and superClassOf o == GeometryClass and classOf o != TargetObject do (
        local b = nodeGetBoundingBox o (matrix3 1)
        lo = [amin lo.x b[1].x, amin lo.y b[1].y, amin lo.z b[1].z]
        hi = [amax hi.x b[2].x, amax hi.y b[2].y, amax hi.z b[2].z]
    )
    if lo.x > hi.x then undefined else #(lo, hi)
)
fn mcpRelinkProps obj fromList toList = (
    -- Rewrite every file-name property of obj that matches one of fromList (case-insensitive).
    local n = 0
    for p in (getPropNames obj) do (
        local v = try (getProperty obj p) catch undefined
        if isKindOf v String and v != "" do (
            local i = findItem fromList (toLower v)
            if i > 0 do (try (setProperty obj p toList[i]; n += 1) catch ())
        )
    )
    n
)
fn mcpFileOwners = (
    -- Every texture map, proxy and light, the things that hold external file paths.
    local out = #()
    for c in (textureMap.classes + light.classes) do try (join out (getClassInstances c)) catch ()
    for c in geometryClass.classes where matchPattern (c as string) pattern:"*proxy*" do try (join out (getClassInstances c)) catch ()
    out
)
mcpResult = undefined
'''

EPILOGUE = r'''
mcpText = mcpJ mcpResult
if mcpOutFile != undefined do (
    mcpF = createFile mcpOutFile encoding:#utf8 writeBOM:false
    format "%" mcpText to:mcpF
    close mcpF
)
mcpText
'''


def ms(s: str | Path) -> str:
    """A MAXScript string literal."""
    s = str(s)
    return f'@"{s}"' if '"' not in s else '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ms_array(items) -> str:
    return "#(" + ", ".join(ms(i) if isinstance(i, (str, Path)) else str(i) for i in items) + ")"


def wrap(body: str) -> str:
    """Prelude + body (which sets mcpResult) inside try/catch + epilogue."""
    return (PRELUDE + "\ntry (\n" + body.strip() + "\n) catch (\n"
            "    mcpResult = mcpO #(#(\"error\", getCurrentException()))\n)\n" + EPILOGUE)


def find_max() -> Path | None:
    s = Settings.load()
    if s.max_dir and (s.max_dir / "3dsmaxbatch.exe").exists():
        return s.max_dir
    found = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Autodesk\3dsMax") as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                ver = winreg.EnumKey(k, i)
                try:
                    with winreg.OpenKey(k, ver) as vk:
                        d = Path(winreg.QueryValueEx(vk, "Installdir")[0])
                        if (d / "3dsmaxbatch.exe").exists():
                            found.append(d)
                except OSError:
                    pass
    except OSError:
        pass
    found += [d for d in Path(r"C:\Program Files\Autodesk").glob("3ds Max 20*") if (d / "3dsmaxbatch.exe").exists()]
    return sorted(set(found), key=lambda d: d.name)[-1] if found else None


def max_activity(sample: float = 1.0) -> dict:
    """CPU use of running 3ds Max processes (in % of one core) and free memory."""
    procs = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "3dsmax.exe"]
    for p in procs:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(sample)
    cpu = 0.0
    for p in procs:
        try:
            cpu += p.cpu_percent(None)
        except psutil.Error:
            pass
    return {"max_processes": len(procs), "max_cpu_percent": round(cpu),
            "free_memory_gb": round(psutil.virtual_memory().available / 2**30, 1)}


def run_batch(body: str, scene: Path | None = None, timeout: int = 1800, force: bool = False) -> dict:
    """Run a script body headless in 3dsmaxbatch.exe and return its JSON result."""
    max_dir = find_max()
    if not max_dir:
        raise ConnecterError("3ds Max (3dsmaxbatch.exe) was not found. Set max_dir in the config.")
    s = Settings.load()
    if not force:
        act = max_activity()
        if act["max_cpu_percent"] > s.max_busy_cpu:
            raise ConnecterError(
                f"3ds Max is busy (using {act['max_cpu_percent']}% CPU, probably rendering), so the headless job "
                "was not started. Try again when it is idle, use mode='script', or pass force=True.")
        if act["free_memory_gb"] < 3:
            raise ConnecterError(f"Only {act['free_memory_gb']} GB of memory is free; not starting 3ds Max. Pass force=True to run anyway.")
    work = Path(tempfile.mkdtemp(prefix="connecter-mcp-max-"))
    out, log, script = work / "result.json", work / "batch.log", work / "job.ms"
    script.write_text(f"mcpOutFile = {ms(out)}\n" + wrap(body), "utf-8-sig")
    cmd = [str(max_dir / "3dsmaxbatch.exe"), str(script), "-v", "3", "-log", str(log)]
    if scene:
        cmd += ["-sceneFile", str(scene)]
    flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        raise ConnecterError(f"3ds Max batch job timed out after {timeout}s. Log: {log}")
    if not out.exists():
        tail = log.read_text("utf-8", "replace")[-1500:] if log.exists() else proc.stdout[-1500:]
        raise ConnecterError(f"3ds Max batch job produced no result (exit {proc.returncode}). Log tail:\n{tail}")
    result = json.loads(out.read_text("utf-8-sig", "replace"))
    if isinstance(result, dict) and result.get("error"):
        raise ConnecterError(f"MAXScript error: {result['error']}")
    return {"mode": "batch", "seconds": round(time.monotonic() - started), "result": result}


def execute(body: str, mode: str = "script", scene: Path | None = None, force: bool = False, timeout: int = 1800) -> dict:
    if mode not in MODES:
        raise ConnecterError(f"mode must be one of {MODES}")
    if mode == "batch":
        return run_batch(body, scene, timeout, force)
    # A file tool run in the user's 3ds Max opens another file: never over unsaved work.
    load = ('if getSaveRequired() do throw "The open scene has unsaved changes. Save it first: this script opens another file."\n'
            f"loadMaxFile {ms(scene)} quiet:true useFileUnits:true\n") if scene else ""
    script = wrap(load + body)
    return {"mode": "script", "maxscript": script,
            "how_to_run": "Run this in 3ds Max (e.g. with the 3ds Max MCP's execute_maxscript). "
                          "It returns a JSON string with the result." +
                          (" It opens the scene file first, replacing the open scene." if scene else "")}
