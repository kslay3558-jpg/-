import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime

import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk
from windows_topology import WindowsTopologyReader

try:
    import winreg
except ImportError as exc:  # pragma: no cover - runtime guard for non-Windows hosts
    raise RuntimeError("This application requires Windows (winreg module).") from exc


APP_VERSION = "1.0.0"
BACKUP_SCHEMA_VERSION = 1

# ── Color palette ──────────────────────────────────────────────────────────────
_TILE_NORMAL   = ("gray80", "gray25")
_TILE_SELECTED = ("#16a34a", "#22c55e")
_TILE_HOVER    = ("gray72", "gray32")
_RECOMMEND_BORDER = ("#ca8a04", "#fbbf24")

_TREE_TAG_COLORS = {
    "dark": {
        "tag_gpu":     "#a78bfa",
        "tag_root":    "#f472b6",
        "tag_audio":   "#60a5fa",
        "tag_storage": "#fb923c",
        "tag_nic":     "#4ade80",
        "tag_usb":     "#94a3b8",
    },
    "light": {
        "tag_gpu":     "#7c3aed",
        "tag_root":    "#be185d",
        "tag_audio":   "#1d4ed8",
        "tag_storage": "#c2410c",
        "tag_nic":     "#15803d",
        "tag_usb":     "#475569",
    },
}

_ROLE_TAG = {
    "gpu":            "tag_gpu",
    "gpu_root_port":  "tag_root",
    "audio":          "tag_audio",
    "storage":        "tag_storage",
    "nic":            "tag_nic",
    "usb_controller": "tag_usb",
}


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    params = subprocess.list2cmdline(sys.argv)
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)


class IRQOptimizerApp:
    MAX_GROUP_CORES = 64

    def __init__(self, root):
        self.root = root
        self.root.title("IRQ Optimizer (Ultimate Safe Edition)")

        self._theme_mode = "dark"
        self._core_tiles: list[ctk.CTkButton] = []
        self._topo_expanded = False

        self.core_vars = []
        self.checkbuttons = []
        self.recommended_cores = []
        self.current_instance_id = None
        self.device_entries = []
        self.device_roles = {}
        self.tree_item_by_instance = {}
        self._device_sort_desc = False
        self.preference_profiles = self.build_preference_profiles()
        self.powershell_executable = self._resolve_powershell_executable()

        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        backup_dir = os.path.join(program_data, "IRQOptimizer")
        os.makedirs(backup_dir, exist_ok=True)
        self.backup_file = os.path.join(backup_dir, "irq_backup.json")

        self.cpu_info = self.get_cpu_info()
        self.topology_snapshot = self.get_topology_snapshot()
        self.physical_cores, detected_logical_processors = self.get_core_counts()
        self.original_logical_processors = detected_logical_processors
        self.is_smt_enabled = self.original_logical_processors > self.physical_cores
        self.logical_processors = max(1, min(detected_logical_processors, self.MAX_GROUP_CORES))
        self.group_limit_active = self.original_logical_processors > self.MAX_GROUP_CORES
        self.cpu_arch = self.classify_cpu()
        self.locality_groups = self.build_locality_groups()
        self.recommendation_sets = self.derive_topology_recommendation()

        self.create_widgets()
        if self.group_limit_active:
            self.status_var.set(
                "Only processor group 0 is supported. Logical processors above 63 are hidden."
            )
        self.load_devices()

    def _resolve_powershell_executable(self):
        for candidate in ("powershell.exe", "pwsh.exe", "powershell", "pwsh"):
            if shutil.which(candidate):
                return candidate
        return "powershell.exe"

    def _run_powershell(self, script):
        flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW
        command = self.powershell_executable or "powershell.exe"
        try:
            return subprocess.run(
                [command, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True,
                text=True,
                creationflags=flags,
            )
        except FileNotFoundError:
            if command.lower() != "pwsh.exe":
                return subprocess.run(
                    ["pwsh.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                    capture_output=True,
                    text=True,
                    creationflags=flags,
                )
            raise

    def get_cpu_info(self):
        try:
            result = self._run_powershell("(Get-CimInstance Win32_Processor).Name")
            if result.returncode == 0:
                return result.stdout.strip() or "Unknown CPU"
        except Exception:
            pass
        return "Unknown CPU"

    def get_core_counts(self):
        topo = self.topology_snapshot or {}
        summary = topo.get("summary", {}) if isinstance(topo, dict) else {}
        if topo.get("api_available"):
            physical = int(
                summary.get("physical_core_count_group0")
                or summary.get("physical_core_count")
                or 0
            )
            logical_total = int(
                summary.get("logical_processor_count")
                or summary.get("logical_processor_count_group0")
                or 0
            )
            if physical > 0 and logical_total > 0:
                return physical, logical_total

        try:
            result = self._run_powershell(
                "Get-CimInstance Win32_Processor | "
                "Select-Object NumberOfCores,NumberOfLogicalProcessors | ConvertTo-Json"
            )
            if result.returncode != 0 or not result.stdout.strip():
                raise RuntimeError(result.stderr.strip() or "core query failed")
            data = json.loads(result.stdout.strip())
            if isinstance(data, list):
                physical = sum(int(x.get("NumberOfCores", 0) or 0) for x in data)
                logical = sum(int(x.get("NumberOfLogicalProcessors", 0) or 0) for x in data)
            else:
                physical = int(data.get("NumberOfCores", 0) or 0)
                logical = int(data.get("NumberOfLogicalProcessors", 0) or 0)
            if physical <= 0 or logical <= 0:
                raise ValueError("invalid topology")
            return physical, logical
        except Exception:
            logical = os.cpu_count() or 8
            return max(1, logical // 2), max(1, logical)

    def classify_cpu(self):
        s = self.cpu_info.lower()
        if "intel" in s or "core(tm)" in s or "core ultra" in s:
            if re.search(r"i[3579]-1[2-4]\d{3}", s) or "core ultra" in s:
                return "intel_hybrid"
            return "intel_legacy"
        if "amd" in s or "ryzen" in s or "epyc" in s or "threadripper" in s:
            if re.search(r"(79|99)[05]0x3d", s):
                return "amd_dual_x3d"
            if re.search(r"(5[678]|78|98)[05]0x3d", s):
                return "amd_single_x3d"
            return "amd_generic"
        return "unknown"

    @staticmethod
    def build_preference_profiles():
        return {
            "Balanced": {
                "role_order": ["gpu", "audio", "nic"],
                "target_roles": {"gpu", "audio", "nic"},
                "description": "GPU·오디오·네트워크 장치를 중심으로 한 기본 게임 환경 권장값입니다.",
            },
            "Low Latency": {
                "role_order": ["gpu", "nic", "audio"],
                "target_roles": {"gpu", "audio", "nic"},
                "description": "입력/네트워크 반응성을 우선해 GPU·NIC·오디오만 집중 배치합니다.",
            },
            "Streaming": {
                "role_order": ["gpu", "audio", "nic"],
                "target_roles": {"gpu", "audio", "nic"},
                "description": "방송/녹화 중 안정성을 위해 GPU·오디오·네트워크 장치 균형을 우선합니다.",
            },
        }

    def get_active_profile_name(self):
        if hasattr(self, "preference_profile_var"):
            selected = self.preference_profile_var.get()
            if selected in self.preference_profiles:
                return selected
        return "Balanced"

    def get_active_profile(self):
        return self.preference_profiles.get(self.get_active_profile_name(), self.preference_profiles["Balanced"])

    def get_active_role_order(self):
        return list(self.get_active_profile().get("role_order", []))

    def get_active_target_roles(self):
        return set(self.get_active_profile().get("target_roles", set()))

    def on_profile_change(self, _event=None):
        self.update_recommendation_text()
        self.status_var.set(f"Preference profile set to {self.get_active_profile_name()}")

    def build_locality_groups(self):
        topo = self.topology_snapshot or {}
        if topo.get("api_available"):
            groups = []
            for grp in topo.get("locality_groups", []):
                norm = [
                    int(x) for x in grp
                    if isinstance(x, int) and 0 <= int(x) < self.MAX_GROUP_CORES
                ]
                if norm:
                    groups.append(norm)
            if groups:
                return groups

        total = max(1, min(self.logical_processors, self.MAX_GROUP_CORES))
        if total <= 1:
            return [[0]]
        phys_limit = min(max(1, self.physical_cores), total)
        physical_cores = list(range(phys_limit))

        if self.cpu_arch == "amd_dual_x3d" and phys_limit >= 8:
            split = max(1, phys_limit // 2)
            return [physical_cores[:split], physical_cores[split:]]
        if self.cpu_arch == "intel_hybrid" and phys_limit >= 6:
            p_core_guess = max(2, phys_limit // 2)
            return [physical_cores[:p_core_guess], physical_cores[p_core_guess:]]
        return [physical_cores]

    def get_topology_snapshot(self):
        logical_guess = max(1, os.cpu_count() or 1)
        physical_guess = max(1, min(self.MAX_GROUP_CORES, logical_guess // 2 if logical_guess > 1 else 1))
        fallback = {
            "api_available": False,
            "topology_source": "Fallback heuristic",
            "source": "fallback heuristic",
            "groups": [
                {
                    "group_id": 0,
                    "maximum_processor_count": 64,
                    "active_processor_count": min(logical_guess, self.MAX_GROUP_CORES),
                    "active_mask": (1 << min(logical_guess, self.MAX_GROUP_CORES)) - 1,
                }
            ],
            "cores": [],
            "numa_nodes": [],
            "summary": {
                "group_count": 1,
                "physical_core_count": physical_guess,
                "physical_core_count_group0": physical_guess,
                "logical_processor_count": logical_guess,
                "logical_processor_count_group0": min(logical_guess, self.MAX_GROUP_CORES),
                "numa_node_count": 1,
                "efficiency_classes": [],
                "smt_widths": [],
            },
            "socket_count": 1,
            "numa_node_count": 1,
            "numa_logical_processors": [min(logical_guess, self.MAX_GROUP_CORES)],
            "locality_groups": [list(range(physical_guess))],
            "primary_group": [0],
            "secondary_group": [1] if logical_guess > 1 else [],
            "group0_representative_logical_processors": list(range(physical_guess)),
            "group0_efficiency_by_logical_processor": {},
            "performance_core_count": 0,
            "efficiency_core_count": 0,
            "error": None,
        }
        try:
            reader = WindowsTopologyReader()
            api_snapshot = reader.read_snapshot()
            if not api_snapshot.get("api_available"):
                return fallback

            summary = api_snapshot.get("summary", {})
            rep_lps = [
                int(x) for x in api_snapshot.get("group0_representative_logical_processors", [])
                if isinstance(x, int) and 0 <= int(x) < self.MAX_GROUP_CORES
            ]
            if not rep_lps:
                return fallback

            core_by_rep = {}
            for core in api_snapshot.get("cores", []):
                rep = WindowsTopologyReader.select_representative_group0_lp(core)
                if rep is None or rep >= self.MAX_GROUP_CORES:
                    continue
                lps = WindowsTopologyReader.filter_group0_logical_processors(core.get("logical_processors", []))
                core_by_rep[rep] = {
                    "logical_processors": lps,
                    "smt_width": max(1, len(lps)),
                    "efficiency_class": core.get("efficiency_class"),
                }

            numa_groups = []
            for node in api_snapshot.get("numa_nodes", []):
                node_lps = set(WindowsTopologyReader.filter_group0_logical_processors(node.get("logical_processors", [])))
                reps = [rep for rep in rep_lps if rep in node_lps]
                if reps:
                    numa_groups.append(sorted(set(reps)))
            if not numa_groups:
                numa_groups = [rep_lps]

            eff_map = {}
            for cls, reps in (api_snapshot.get("efficiency_class_map_group0", {}) or {}).items():
                key = int(cls)
                norm = sorted(set(int(x) for x in reps if isinstance(x, int) and 0 <= int(x) < self.MAX_GROUP_CORES))
                if norm:
                    eff_map[key] = norm

            perf_count = 0
            eff_count = 0
            if eff_map:
                perf_class = min(eff_map.keys())
                perf_count = len(eff_map.get(perf_class, []))
                eff_count = max(0, len(rep_lps) - perf_count)

            group0_active = int(
                summary.get("logical_processor_count_group0")
                or next((g.get("active_processor_count") for g in api_snapshot.get("groups", []) if g.get("group_id") == 0), 0)
                or 0
            )
            if group0_active <= 0:
                group0_active = min(self.MAX_GROUP_CORES, max((max(rep_lps) + 1), len(rep_lps)))

            snapshot = {
                "api_available": True,
                "topology_source": "Windows API",
                "source": "Windows API-derived core relationships",
                "groups": api_snapshot.get("groups", []),
                "cores": api_snapshot.get("cores", []),
                "numa_nodes": api_snapshot.get("numa_nodes", []),
                "summary": summary,
                "socket_count": 1,
                "numa_node_count": max(1, int(summary.get("numa_node_count", len(numa_groups) or 1))),
                "numa_logical_processors": [len(x) for x in numa_groups] or [len(rep_lps)],
                "locality_groups": numa_groups,
                "primary_group": list(numa_groups[0]) if numa_groups else [rep_lps[0]],
                "secondary_group": list(numa_groups[1][:2]) if len(numa_groups) > 1 else [],
                "group0_active_logical_processors": group0_active,
                "group0_representative_logical_processors": rep_lps,
                "group0_efficiency_by_logical_processor": {
                    rep: core_by_rep.get(rep, {}).get("efficiency_class") for rep in rep_lps
                },
                "group0_core_map": core_by_rep,
                "efficiency_classes": sorted(eff_map.keys()),
                "efficiency_class_map_group0": eff_map,
                "performance_core_count": perf_count,
                "efficiency_core_count": eff_count,
                "error": api_snapshot.get("error"),
            }
            return snapshot
        except Exception:
            return fallback

    def derive_topology_recommendation(self):
        total = self.logical_processors
        if total <= 1:
            return {
                "branch": "single_core_fallback",
                "base_cores": [0],
                "gpu_cores": [0],
                "gpu_root_cores": [0],
                "side_cores": [0],
                "reason": "논리 프로세서가 1개라서 모든 대상에 CPU 0을 사용합니다.",
            }

        topo = self.topology_snapshot or {}
        rep_lps = [
            int(x)
            for x in topo.get("group0_representative_logical_processors", [])
            if isinstance(x, int) and 0 <= int(x) < total
        ]
        if not rep_lps:
            phys_limit = min(max(1, self.physical_cores), total)
            rep_lps = list(range(phys_limit))

        locality_groups = []
        for grp in topo.get("locality_groups", self.locality_groups):
            norm = [x for x in grp if isinstance(x, int) and 0 <= int(x) < total and x in rep_lps]
            if norm:
                locality_groups.append(norm)
        if not locality_groups:
            locality_groups = [rep_lps]

        primary_locality = locality_groups[0]
        secondary_locality = locality_groups[1] if len(locality_groups) > 1 else []
        remaining = [x for x in rep_lps if x not in primary_locality]
        if not secondary_locality:
            secondary_locality = remaining

        gpu_cores = []
        gpu_root_cores = []
        side_cores = []
        audio_cores = []
        branch = self.cpu_arch
        reason = "기본 보수 배치 규칙이 적용되었습니다."

        def _tail_biased(values, limit, tail_ratio=0.5):
            ordered = []
            for v in values:
                if isinstance(v, int) and 0 <= v < total and v not in ordered:
                    ordered.append(v)
            if not ordered:
                return []
            if len(ordered) <= limit:
                return ordered
            start = min(len(ordered) - 1, max(0, int(len(ordered) * tail_ratio)))
            tail = ordered[start:]
            if len(tail) >= limit:
                return tail[:limit]
            need = limit - len(tail)
            prefix = ordered[max(0, start - need):start]
            return prefix + tail

        if branch == "intel_hybrid":
            eff_map = topo.get("efficiency_class_map_group0", {}) or {}
            if eff_map:
                perf_class = min(int(k) for k in eff_map.keys())
                p_cores = [x for x in eff_map.get(perf_class, []) if x in rep_lps]
                e_cores = [x for x in rep_lps if x not in p_cores]
            else:
                p_core_count = max(2, min(len(rep_lps), (len(rep_lps) * 2) // 3))
                p_cores = rep_lps[:p_core_count]
                e_cores = [x for x in rep_lps if x not in p_cores]
            p_primary = [x for x in primary_locality if x in p_cores]
            e_secondary = [x for x in secondary_locality if x in e_cores]
            gpu_cores = (
                _tail_biased(p_primary, min(6, len(p_primary)), tail_ratio=0.5)
                or _tail_biased(p_cores, min(4, len(p_cores)), tail_ratio=0.5)
                or _tail_biased(primary_locality, min(4, len(primary_locality)), tail_ratio=0.5)
            )
            gpu_root_cores = _tail_biased([x for x in p_cores if x not in gpu_cores], 2, tail_ratio=0.5) or gpu_cores[:1]
            side_cores = (
                _tail_biased(e_secondary, 3, tail_ratio=0.5)
                or _tail_biased(e_cores, 3, tail_ratio=0.5)
                or _tail_biased([x for x in p_cores if x not in gpu_cores], 3, tail_ratio=0.5)
            )
            audio_cores = (
                _tail_biased([x for x in p_primary if x not in gpu_cores], 3, tail_ratio=0.5)
                or _tail_biased([x for x in p_cores if x not in gpu_cores], 3, tail_ratio=0.5)
                or gpu_cores[:]
            )
            reason = "Intel 하이브리드 분기: 물리 P-코어 기반 배치를 우선하고, 오디오는 GPU 경로와 동일한 P-코어 영역을 먼저 사용합니다."
        elif branch == "amd_dual_x3d":
            ccd0 = primary_locality or rep_lps[: max(1, len(rep_lps) // 2)]
            ccd1 = secondary_locality or [x for x in rep_lps if x not in ccd0]
            gpu_cores = _tail_biased(ccd0, min(6, len(ccd0)), tail_ratio=0.5)
            gpu_root_cores = _tail_biased([x for x in ccd0 if x not in gpu_cores], 2, tail_ratio=0.5) or gpu_cores[:1]
            side_cores = _tail_biased(ccd1, 3, tail_ratio=0.5) or _tail_biased([x for x in ccd0 if x not in gpu_cores], 3, tail_ratio=0.5)
            audio_cores = (
                _tail_biased([x for x in ccd0 if x not in gpu_root_cores], min(4, len(ccd0)), tail_ratio=0.5)
                or gpu_cores[:]
            )
            reason = "AMD 듀얼 CCD/X3D 분기: GPU와 오디오를 CCD0에 우선 배치하고, NIC/스토리지는 CCD1 분리를 우선합니다."
        elif branch in {"amd_single_x3d", "amd_generic"}:
            gpu_span = 6 if branch == "amd_single_x3d" else 4
            gpu_cores = _tail_biased(primary_locality, min(gpu_span, len(primary_locality)), tail_ratio=0.5)
            gpu_root_cores = _tail_biased([x for x in primary_locality if x not in gpu_cores], 2, tail_ratio=0.5) or gpu_cores[:1]
            side_cores = _tail_biased([x for x in rep_lps if x not in gpu_cores], 3, tail_ratio=0.5)
            if not side_cores:
                side_cores = _tail_biased(primary_locality[::2], 3, tail_ratio=0.5)
            audio_cores = side_cores[:]
            reason = "AMD 분기: GPU 근접성을 유지하면서 초반 코어 과집중을 줄이도록 후반 코어 약우선 규칙을 적용했습니다."
        elif branch == "intel_legacy":
            gpu_cores = _tail_biased(primary_locality, min(4, len(primary_locality)), tail_ratio=0.5)
            gpu_root_cores = _tail_biased([x for x in primary_locality if x not in gpu_cores], 2, tail_ratio=0.5) or gpu_cores[:1]
            side_cores = _tail_biased([x for x in rep_lps if x not in gpu_cores], 3, tail_ratio=0.5)
            if not side_cores:
                side_cores = gpu_root_cores[:]
            audio_cores = side_cores[:]
            reason = "Intel 레거시 분기: 주 로컬리티의 균일 물리 코어에 후반 코어 약우선 규칙으로 분산 배치했습니다."
        else:
            gpu_cores = _tail_biased(primary_locality, min(4, len(primary_locality)), tail_ratio=0.5)
            gpu_root_cores = _tail_biased([x for x in primary_locality if x not in gpu_cores], 2, tail_ratio=0.5) or gpu_cores[:1]
            side_cores = _tail_biased([x for x in rep_lps if x not in gpu_cores], 3, tail_ratio=0.5)
            if not side_cores:
                side_cores = gpu_root_cores[:]
            audio_cores = side_cores[:]
            reason = "미확인 CPU: 로컬리티 우선의 보수적 배치와 후반 코어 약우선 규칙을 적용했습니다."

        def _sanitize(values, fallback):
            clean = []
            for v in values:
                if isinstance(v, int) and 0 <= v < total and v not in clean:
                    clean.append(v)
            if clean:
                return clean
            return [fallback]

        gpu_cores = _sanitize(gpu_cores, 0)
        gpu_root_cores = _sanitize(gpu_root_cores, gpu_cores[0])
        side_cores = _sanitize(side_cores, gpu_root_cores[0])
        audio_cores = _sanitize(audio_cores, gpu_cores[0])

        base_cores = []
        for core in gpu_cores + audio_cores:
            if core not in base_cores:
                base_cores.append(core)

        return {
            "branch": branch,
            "base_cores": _sanitize(base_cores, gpu_cores[0]),
            "gpu_cores": gpu_cores,
            "gpu_root_cores": gpu_root_cores,
            "side_cores": side_cores,
            "audio_cores": audio_cores,
            "reason": reason,
        }

    def analyze_cpu_topology(self):
        smt = "On" if self.is_smt_enabled else "Off"
        topo = self.topology_snapshot or {}
        rec = self.recommendation_sets or {}
        summary = topo.get("summary", {}) if isinstance(topo, dict) else {}
        groups = topo.get("groups", []) if isinstance(topo, dict) else []
        group0 = next((g for g in groups if int(g.get("group_id", -1)) == 0), {})
        smt_widths = summary.get("smt_widths", [])
        smt_summary = ", ".join(str(x) for x in smt_widths) if smt_widths else ("2" if self.is_smt_enabled else "1")
        eff_classes = topo.get("efficiency_classes") or summary.get("efficiency_classes", [])
        eff_text = ", ".join(str(x) for x in eff_classes) if eff_classes else "Unavailable"
        source = topo.get("topology_source", "Fallback heuristic")
        guide = (
            f"CPU: {self.cpu_info}\n"
            f"Cores: Physical {self.physical_cores} / Logical {self.logical_processors}"
            f"{f' (detected {self.original_logical_processors}, group 0 view)' if self.group_limit_active else ''}\n"
            f"SMT/HT: {smt}\n"
            f"Architecture profile: {self.cpu_arch}\n"
            f"Recommendation branch: {rec.get('branch', self.cpu_arch)}\n"
            f"Topology source: {source}\n"
            f"Processor groups: {summary.get('group_count', len(groups) or 1)}, "
            f"Group 0 active LPs: {topo.get('group0_active_logical_processors', group0.get('active_processor_count', self.logical_processors))}\n"
            f"Physical cores (topology): {summary.get('physical_core_count_group0', self.physical_cores)}, "
            f"SMT width set: {smt_summary}\n"
            f"Sockets: {topo.get('socket_count', 1)}, NUMA nodes: {topo.get('numa_node_count', summary.get('numa_node_count', 1))}\n"
            f"Reported P-cores: {topo.get('performance_core_count', 0)}, "
            f"E-cores: {topo.get('efficiency_core_count', 0)}\n"
            f"Detected efficiency classes: {eff_text}\n"
            f"Locality groups: {topo.get('locality_groups', self.locality_groups)}\n\n"
        )
        msg = {
            "intel_hybrid": "Topology-aware heuristic: efficiency classes and physical-core mapping are preferred.",
            "intel_legacy": "Recommended cores spread evenly across uniform physical cores.",
            "amd_dual_x3d": "Topology-aware heuristic: NUMA/locality split is preferred for side devices.",
            "amd_single_x3d": "Recommended cores spread across available cores with X3D cache proximity.",
            "amd_generic": "Recommended cores spread across physical cores.",
            "single_core_fallback": "Single logical processor: CPU 0 is used for all targets.",
            "unknown": "Default conservative spread recommendation is used.",
        }
        return (
            guide
            + msg.get(self.cpu_arch, msg["unknown"])
            + "\nWindows API-derived core relationships are used when available.\n"
            + "Not a guarantee of optimal IRQ placement."
        )

    def get_reg_path(self, instance_id):
        return (
            fr"SYSTEM\CurrentControlSet\Enum\{instance_id}\Device Parameters\Interrupt Management\Affinity Policy"
        )

    def read_affinity_values(self, instance_id):
        reg_path = self.get_reg_path(instance_id)
        assignment = None
        device_policy = None
        had_affinity_key = False
        had_device_policy_key = False

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path, 0, winreg.KEY_READ) as key:
                try:
                    assignment, _ = winreg.QueryValueEx(key, "AssignmentSetOverride")
                    had_affinity_key = True
                except FileNotFoundError:
                    pass
                try:
                    device_policy, _ = winreg.QueryValueEx(key, "DevicePolicy")
                    had_device_policy_key = True
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass

        return {
            "assignment": assignment,
            "device_policy": device_policy,
            "had_affinity_key": had_affinity_key,
            "had_device_policy_key": had_device_policy_key,
        }

    def write_affinity_values(self, instance_id, assignment_bytes, device_policy=4):
        reg_path = self.get_reg_path(instance_id)
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, reg_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "DevicePolicy", 0, winreg.REG_DWORD, int(device_policy))
            winreg.SetValueEx(key, "AssignmentSetOverride", 0, winreg.REG_BINARY, assignment_bytes)

    def delete_value(self, instance_id, value_name):
        reg_path = self.get_reg_path(instance_id)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)

    def load_backup(self):
        if not os.path.exists(self.backup_file):
            return {
                "schema_version": BACKUP_SCHEMA_VERSION,
                "app_version": APP_VERSION,
                "updated_at": None,
                "entries": {},
            }
        try:
            with open(self.backup_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("invalid backup root")
            entries = data.get("entries")
            if not isinstance(entries, dict):
                data["entries"] = {}
            data.setdefault("schema_version", BACKUP_SCHEMA_VERSION)
            data.setdefault("app_version", APP_VERSION)
            data.setdefault("updated_at", datetime.utcnow().isoformat() + "Z")
            return data
        except Exception:
            return {
                "schema_version": BACKUP_SCHEMA_VERSION,
                "app_version": APP_VERSION,
                "updated_at": None,
                "entries": {},
            }

    def save_backup(self, data):
        data["schema_version"] = BACKUP_SCHEMA_VERSION
        data["app_version"] = APP_VERSION
        data["updated_at"] = datetime.utcnow().isoformat() + "Z"

        folder = os.path.dirname(self.backup_file)
        os.makedirs(folder, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="irq_backup_", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(data, tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self.backup_file)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def backup_current_state(self, instance_id, current_state, applied_mask_int=None, applied_cores=None):
        backup = self.load_backup()
        entries = backup.setdefault("entries", {})

        assignment_bytes = current_state.get("assignment")
        assignment_hex = assignment_bytes.hex() if isinstance(assignment_bytes, (bytes, bytearray)) else None

        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "had_affinity_key": bool(current_state.get("had_affinity_key", False)),
            "had_device_policy_key": bool(current_state.get("had_device_policy_key", False)),
            "previous_assignment_hex": assignment_hex,
            "previous_assignment_len": len(assignment_bytes) if isinstance(assignment_bytes, (bytes, bytearray)) else 0,
            "previous_device_policy": current_state.get("device_policy"),
            "applied_mask_int": int(applied_mask_int) if applied_mask_int is not None else None,
            "applied_mask_hex": hex(applied_mask_int) if applied_mask_int is not None else None,
            "applied_cores": list(applied_cores) if applied_cores else [],
            "applied_group": 0,
        }
        entries[instance_id] = entry
        self.save_backup(backup)

    def get_recommended_cores(self, instance_id=None):
        rec = self.recommendation_sets or {}

        def _normalize(values):
            if not isinstance(values, list):
                return []
            return [x for x in values if isinstance(x, int) and 0 <= x < self.logical_processors]

        if instance_id:
            iid = self._normalize_instance_id(instance_id)
            roles = self.device_roles.get(iid, set())
            role_to_key = [
                ("gpu", "gpu_cores"),
                ("gpu_root_port", "gpu_root_cores"),
                ("audio", "audio_cores"),
                ("nic", "side_cores"),
                ("storage", "side_cores"),
                ("usb_controller", "side_cores"),
                ("pcie_root_port", "gpu_root_cores"),
            ]
            for role, key in role_to_key:
                if role in roles:
                    picked = _normalize(rec.get(key))
                    if picked:
                        return picked

        base = _normalize(rec.get("base_cores"))
        if base:
            return base
        return [0]

    @staticmethod
    def _normalize_instance_id(instance_id):
        return (instance_id or "").strip().lower()

    def classify_device_base_roles(self, name, pnp_class, instance_id):
        name_l = (name or "").lower()
        cls_l = (pnp_class or "").lower()
        inst_l = self._normalize_instance_id(instance_id)
        roles = set()

        gpu_keys = ("nvidia", "geforce", "rtx", "gtx", "quadro", "radeon", "rx ", "arc")
        if cls_l == "display" or any(k in name_l for k in gpu_keys):
            roles.add("gpu")

        root_port_keys = ("root port", "root complex", "pcie root", "pci express root")
        if cls_l == "system" and "pci" in name_l and any(k in name_l for k in root_port_keys):
            roles.add("pcie_root_port")

        audio_keys = ("audio", "hd audio", "high definition audio", "realtek", "sound")
        if cls_l in {"media", "audioendpoint"} or any(k in name_l for k in audio_keys):
            roles.add("audio")

        storage_classes = {"diskdrive", "scsiadapter", "hdc", "ide", "storage"}
        if cls_l in storage_classes or inst_l.startswith(("nvme\\", "scsi\\", "stor", "sata")):
            roles.add("storage")

        nic_keys = ("ethernet", "network", "wireless", "wi-fi", "wifi", "2.5gbe", "10gbe", "lan")
        if cls_l == "net" or any(k in name_l for k in nic_keys):
            roles.add("nic")

        usb_controller_keys = ("xhci", "ehci", "host controller", "extensible host controller")
        if (cls_l == "usb" and any(k in name_l for k in usb_controller_keys)) or (
            any(k in name_l for k in usb_controller_keys) and "controller" in name_l
        ):
            roles.add("usb_controller")

        return roles

    def build_device_role_map(self, entries):
        role_map = {}
        by_id = {self._normalize_instance_id(x["instance"]): x for x in entries}

        for entry in entries:
            iid = self._normalize_instance_id(entry["instance"])
            role_map[iid] = self.classify_device_base_roles(entry["name"], entry["class"], entry["instance"])

        root_port_ids = {iid for iid, roles in role_map.items() if "pcie_root_port" in roles}
        gpu_ids = [iid for iid, roles in role_map.items() if "gpu" in roles]

        for gpu_iid in gpu_ids:
            current = gpu_iid
            for _ in range(16):
                parent = self._normalize_instance_id(by_id.get(current, {}).get("parent"))
                if not parent:
                    break
                if parent in root_port_ids:
                    role_map[parent].add("gpu_root_port")
                    break
                if parent == current:
                    break
                current = parent

        return role_map

    @staticmethod
    def role_label(roles):
        order = ["gpu", "gpu_root_port", "usb_controller", "audio", "storage", "nic", "pcie_root_port"]
        labels = {
            "gpu": "GPU",
            "gpu_root_port": "GPU-ROOT",
            "usb_controller": "USB",
            "audio": "AUDIO",
            "storage": "STORAGE",
            "nic": "NIC",
            "pcie_root_port": "ROOT",
        }
        selected = [labels[x] for x in order if x in roles]
        return "|".join(selected)

    def build_target_strategy_text(self):
        role_order = self.get_active_role_order()
        role_groups = {role: [] for role in role_order}
        for entry in self.device_entries:
            iid = self._normalize_instance_id(entry["instance"])
            roles = self.device_roles.get(iid, set())
            for role in role_groups:
                if role in roles:
                    role_groups[role].append(entry["name"])

        topo = self.topology_snapshot or {}
        rec = self.recommendation_sets or {}
        base = self.get_recommended_cores()
        gpu_group = rec.get("gpu_cores", base)[: min(4, len(rec.get("gpu_cores", base)))] or [0]
        root_group = rec.get("gpu_root_cores", gpu_group[:1])[:2] or [gpu_group[0]]
        side_group = rec.get("side_cores", [x for x in base if x not in gpu_group][:2])[:3] or [gpu_group[0]]
        audio_group = rec.get("audio_cores", side_group)[: min(4, len(rec.get("audio_cores", side_group)))] or [gpu_group[0]]

        labels = {
            "gpu": "GPU",
            "gpu_root_port": "GPU 루트 포트",
            "usb_controller": "USB 컨트롤러",
            "audio": "오디오",
            "storage": "스토리지",
            "nic": "NIC",
        }
        profile_labels = {
            "Balanced": "균형형",
            "Low Latency": "저지연",
            "Streaming": "스트리밍",
        }
        active_profile = self.get_active_profile_name()
        active_profile_ko = profile_labels.get(active_profile, active_profile)
        summary = ", ".join(f"{labels.get(role, role)}: {len(role_groups.get(role, []))}" for role in role_order)
        priority_text = " → ".join(labels.get(role, role) for role in role_order)
        profile = self.get_active_profile()

        lines = [
            f"대상 장치 추천 프로필: {active_profile_ko} ({active_profile})",
            f"프로필 설명: {profile.get('description', '')}",
            f"대상 우선순위: {priority_text}",
            f"감지된 장치 수: {summary}",
            f"토폴로지 요약: 소스 {topo.get('topology_source', '휴리스틱 추정')}, "
            f"그룹 {topo.get('summary', {}).get('group_count', 1)}, "
            f"NUMA 노드 {topo.get('numa_node_count', 1)}, "
            f"주 로컬리티 {topo.get('primary_group', [])}",
            f"분기 정책: {rec.get('branch', self.cpu_arch)}",
            "",
            "코어 배치 정책:",
            f"- GPU: 1차 근접 물리 코어 그룹 {gpu_group} 우선",
            f"- GPU 루트 포트: 동일 로컬리티 인접 코어 {root_group} 우선 (강제 단일 코어 중첩 회피)",
            f"- USB 컨트롤러: 경합이 낮은 인접 사이드 코어 {side_group} 우선",
            f"- 오디오: 저지연 경로 우선 코어 {audio_group} 사용",
            f"- 스토리지/NIC: 가능하면 분리된 인접 사이드 코어 {side_group} 사용",
            "",
            f"근거: {rec.get('reason', '토폴로지 인식 휴리스틱 적용')} "
            "GPU와 PCIe 루트 포트를 동일 코어에 강제로 겹치면 IRQ 경합이 증가할 수 있습니다.",
        ]
        if self.group_limit_active:
            lines.append(
                "주의: Processor Group 0만 지원하며, 63번 초과 논리 프로세서는 숨겨집니다."
            )
        return "\n".join(lines)

    def update_recommendation_text(self):
        self.recommend_text.configure(state="normal")
        self.recommend_text.delete("1.0", tk.END)
        self.recommend_text.insert("1.0", self.build_target_strategy_text())
        self.recommend_text.configure(state="disabled")

    def select_target_devices(self):
        self.tree.selection_remove(*self.tree.selection())
        selected_ids = []
        target_roles = self.get_active_target_roles()
        for instance_id, item_id in self.tree_item_by_instance.items():
            roles = self.device_roles.get(self._normalize_instance_id(instance_id), set())
            if roles & target_roles:
                selected_ids.append(item_id)
        for item_id in selected_ids:
            self.tree.selection_add(item_id)
        if selected_ids:
            self.tree.focus(selected_ids[0])
            self.tree.see(selected_ids[0])
            self.on_device_select()
        self.status_var.set(
            f"Selected {len(selected_ids)} recommended target devices ({self.get_active_profile_name()})"
        )

    # ── Widget construction ────────────────────────────────────────────────────

    def create_widgets(self):
        self.root.geometry("1260x900")
        self.root.minsize(1000, 720)
        self.root.resizable(True, True)

        # ── Header bar ────────────────────────────────────────────────────────
        self.header = ctk.CTkFrame(self.root, height=52, corner_radius=0, fg_color="#0f172a")
        self.header.pack(fill=tk.X, side=tk.TOP)
        self.header.pack_propagate(False)

        ctk.CTkLabel(
            self.header,
            text=f"  🔧  IRQ Optimizer  —  Ultimate Safe Edition   v{APP_VERSION}",
            font=ctk.CTkFont("Segoe UI", 15, "bold"),
            text_color="#f1f5f9",
        ).pack(side=tk.LEFT, padx=14, pady=10)

        self.theme_btn = ctk.CTkButton(
            self.header,
            text="☀️  Light",
            width=96,
            height=32,
            command=self._toggle_theme,
            fg_color="#1e293b",
            hover_color="#334155",
            text_color="#f1f5f9",
            corner_radius=8,
        )
        self.theme_btn.pack(side=tk.RIGHT, padx=14, pady=10)

        # ── Body (sidebar | main) ─────────────────────────────────────────────
        self.body = ctk.CTkFrame(self.root, corner_radius=0, fg_color="transparent")
        self.body.pack(fill=tk.BOTH, expand=True, side=tk.TOP)
        self.body.columnconfigure(1, weight=1)
        self.body.rowconfigure(0, weight=1)

        # Sidebar
        self.sidebar = ctk.CTkScrollableFrame(self.body, width=280, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self._build_sidebar()

        # Main panel
        self.main_panel = ctk.CTkFrame(self.body, corner_radius=0, fg_color="transparent")
        self.main_panel.grid(row=0, column=1, sticky="nsew")
        self.main_panel.columnconfigure(0, weight=1)
        self.main_panel.rowconfigure(0, weight=2)   # device list
        self.main_panel.rowconfigure(1, weight=3)   # core grid
        self.main_panel.rowconfigure(2, weight=0)   # toolbar (fixed)
        self._build_device_list()
        self._build_core_grid()
        self._build_toolbar()

        # ── Status bar ────────────────────────────────────────────────────────
        self._build_status_bar()

        self.build_core_selector()
        self._apply_treeview_style()

    # ── Sidebar ────────────────────────────────────────────────────────────────

    def _build_sidebar(self):
        _FONT_SECTION = ctk.CTkFont("Segoe UI", 12, "bold")
        _FONT_DESC    = ctk.CTkFont("Segoe UI", 10)

        # ── Target Profile radio cards ─────────────────────────────────────
        ctk.CTkLabel(
            self.sidebar, text="Target Profile", font=_FONT_SECTION,
        ).pack(anchor="w", padx=10, pady=(14, 4))

        self.preference_profile_var = tk.StringVar(value="Balanced")
        _profile_info = {
            "Balanced":    "게임·작업 혼용 환경 추천",
            "Low Latency": "경쟁 게임 / 낮은 입력 지연 우선",
            "Streaming":   "방송·녹화 병행 환경 추천",
        }
        for name, desc in _profile_info.items():
            card = ctk.CTkFrame(self.sidebar, corner_radius=8)
            card.pack(fill=tk.X, padx=8, pady=3)
            ctk.CTkRadioButton(
                card,
                text=name,
                variable=self.preference_profile_var,
                value=name,
                command=self.on_profile_change,
                font=_FONT_SECTION,
            ).pack(anchor="w", padx=10, pady=(8, 2))
            ctk.CTkLabel(
                card, text=desc, font=_FONT_DESC, text_color=("gray40", "gray65"),
            ).pack(anchor="w", padx=28, pady=(0, 8))

        # ── CPU Topology accordion ─────────────────────────────────────────
        ctk.CTkFrame(self.sidebar, height=1, fg_color=("gray80", "gray30")).pack(
            fill=tk.X, padx=8, pady=(16, 0)
        )
        self.topo_toggle_btn = ctk.CTkButton(
            self.sidebar,
            text="▶  CPU Topology",
            anchor="w",
            command=self._toggle_topology,
            fg_color="transparent",
            hover_color=("gray85", "gray25"),
            text_color=("gray10", "gray90"),
            font=_FONT_SECTION,
            height=34,
        )
        self.topo_toggle_btn.pack(fill=tk.X, padx=8, pady=(4, 0))

        self.topo_textbox = ctk.CTkTextbox(
            self.sidebar, height=160, font=ctk.CTkFont("Consolas", 10), wrap="word"
        )
        self.topo_textbox.insert("1.0", self.analyze_cpu_topology())
        self.topo_textbox.configure(state="disabled")
        # Not packed yet — revealed on toggle

        # ── Recommendation text ────────────────────────────────────────────
        ctk.CTkFrame(self.sidebar, height=1, fg_color=("gray80", "gray30")).pack(
            fill=tk.X, padx=8, pady=(12, 0)
        )
        ctk.CTkLabel(
            self.sidebar, text="Target Recommendation", font=_FONT_SECTION,
        ).pack(anchor="w", padx=10, pady=(8, 4))

        self.recommend_text = ctk.CTkTextbox(
            self.sidebar, height=240, font=ctk.CTkFont("Consolas", 10), wrap="word"
        )
        self.recommend_text.pack(fill=tk.X, padx=8, pady=(0, 14))
        self.recommend_text.insert("1.0", "장치 스캔 후 추천 전략이 여기에 표시됩니다.")
        self.recommend_text.configure(state="disabled")

    def _toggle_topology(self):
        if self._topo_expanded:
            self.topo_textbox.pack_forget()
            self.topo_toggle_btn.configure(text="▶  CPU Topology")
            self._topo_expanded = False
        else:
            self.topo_textbox.pack(fill=tk.X, padx=8, pady=(0, 4))
            self.topo_toggle_btn.configure(text="▼  CPU Topology")
            self._topo_expanded = True

    # ── Device list ────────────────────────────────────────────────────────────

    def _build_device_list(self):
        device_frame = ctk.CTkFrame(self.main_panel, corner_radius=8)
        device_frame.grid(row=0, column=0, sticky="nsew", padx=(6, 10), pady=(10, 4))
        device_frame.rowconfigure(1, weight=1)
        device_frame.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            device_frame,
            text="Device List",
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 4))

        tree_wrap = tk.Frame(device_frame, bg=device_frame.cget("fg_color")[1]
                             if isinstance(device_frame.cget("fg_color"), (list, tuple))
                             else device_frame.cget("fg_color"))
        tree_wrap.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        tree_wrap.columnconfigure(0, weight=1)
        tree_wrap.rowconfigure(0, weight=1)

        columns = ("Name", "Class", "InstanceID")
        self.tree = ttk.Treeview(tree_wrap, columns=columns, show="headings", height=12, selectmode="extended")
        self.tree.heading("Name", text="Device (Type Sort)", command=self.sort_devices_by_type)
        self.tree.heading("Class", text="Class", command=self.sort_devices_by_type)
        self.tree.heading("InstanceID", text="InstanceID")
        self.tree.column("Name", width=580)
        self.tree.column("Class", width=130)
        self.tree.column("InstanceID", width=0, stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self.on_device_select)
        self.tree.bind("<Double-1>", self.on_device_double_click)

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vsb.set)

    # ── Core tile grid ─────────────────────────────────────────────────────────

    def _build_core_grid(self):
        core_outer = ctk.CTkFrame(self.main_panel, corner_radius=8)
        core_outer.grid(row=1, column=0, sticky="nsew", padx=(6, 10), pady=4)
        core_outer.rowconfigure(1, weight=1)
        core_outer.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            core_outer,
            text="Core Selection  (Processor Group 0, up to 64 logical CPUs)",
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 4))

        self.core_scroll = ctk.CTkScrollableFrame(core_outer, corner_radius=0)
        self.core_scroll.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.core_tile_frame = self.core_scroll

    # ── Button toolbar ─────────────────────────────────────────────────────────

    def _build_toolbar(self):
        toolbar = ctk.CTkFrame(self.main_panel, height=58, corner_radius=8)
        toolbar.grid(row=2, column=0, sticky="ew", padx=(6, 10), pady=(4, 10))
        toolbar.pack_propagate(False)

        _BTN = dict(height=36, corner_radius=6, font=ctk.CTkFont("Segoe UI", 12))

        left = ctk.CTkFrame(toolbar, fg_color="transparent")
        left.pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ctk.CTkButton(left, text="🔄  Refresh",        command=self.load_devices,           width=118, **_BTN).pack(side=tk.LEFT, padx=3, pady=11)
        ctk.CTkButton(left, text="🎯  Select Targets", command=self.select_target_devices,  width=134, **_BTN).pack(side=tk.LEFT, padx=3, pady=11)
        ctk.CTkButton(left, text="⭐  Use Recommended", command=self.select_recommended_cores, width=148, **_BTN).pack(side=tk.LEFT, padx=3, pady=11)

        right = ctk.CTkFrame(toolbar, fg_color="transparent")
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=8)
        ctk.CTkButton(
            right, text="↩  Undo Focused",     command=self.undo_last_change,
            width=118, fg_color=("#64748b", "#475569"), hover_color=("#475569", "#334155"), **_BTN,
        ).pack(side=tk.LEFT, padx=3, pady=11)
        ctk.CTkButton(
            right, text="🗑  Reset Focused", command=self.factory_reset,
            width=130, fg_color=("#dc2626", "#ef4444"), hover_color=("#b91c1c", "#dc2626"), **_BTN,
        ).pack(side=tk.LEFT, padx=3, pady=11)
        ctk.CTkButton(
            right, text="✅  Apply to Selected", command=self.apply_affinity,
            width=170, fg_color=("#16a34a", "#22c55e"), hover_color=("#15803d", "#16a34a"),
            font=ctk.CTkFont("Segoe UI", 13, "bold"), height=36, corner_radius=6,
        ).pack(side=tk.LEFT, padx=3, pady=11)

    # ── Status bar ─────────────────────────────────────────────────────────────

    def _build_status_bar(self):
        self.status_bar = ctk.CTkFrame(self.root, height=36, corner_radius=0)
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_bar.pack_propagate(False)

        self.status_var = tk.StringVar(value="Ready")
        self.status_label = ctk.CTkLabel(
            self.status_bar,
            text="Ready",
            font=ctk.CTkFont("Segoe UI", 11),
            anchor="w",
        )
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=12)
        self.status_var.trace_add(
            "write",
            lambda *_: self.status_label.configure(text=self.status_var.get()),
        )

        self.progress_bar = ctk.CTkProgressBar(self.status_bar, width=150, mode="indeterminate")
        self.progress_bar.pack(side=tk.RIGHT, padx=10, pady=7)
        self.progress_bar.set(0)

    def _start_busy(self):
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()

    def _stop_busy(self):
        self.progress_bar.stop()
        self.progress_bar.set(0)

    # ── Theme toggle ───────────────────────────────────────────────────────────

    def _toggle_theme(self):
        if self._theme_mode == "dark":
            ctk.set_appearance_mode("light")
            self._theme_mode = "light"
            self.theme_btn.configure(text="🌙  Dark")
        else:
            ctk.set_appearance_mode("dark")
            self._theme_mode = "dark"
            self.theme_btn.configure(text="☀️  Light")
        self._apply_treeview_style()
        self.highlight_recommendations()

    def _apply_treeview_style(self):
        mode = self._theme_mode
        style = ttk.Style()
        style.theme_use("clam")
        if mode == "dark":
            style.configure("Treeview",
                background="#1e293b", foreground="#f1f5f9",
                rowheight=26, fieldbackground="#1e293b",
                font=("Segoe UI", 10))
            style.configure("Treeview.Heading",
                background="#0f172a", foreground="#94a3b8",
                font=("Segoe UI", 10, "bold"))
            style.map("Treeview", background=[("selected", "#1d4ed8")])
        else:
            style.configure("Treeview",
                background="#ffffff", foreground="#0f172a",
                rowheight=26, fieldbackground="#ffffff",
                font=("Segoe UI", 10))
            style.configure("Treeview.Heading",
                background="#f1f5f9", foreground="#475569",
                font=("Segoe UI", 10, "bold"))
            style.map("Treeview", background=[("selected", "#bfdbfe")])

        colors = _TREE_TAG_COLORS[mode]
        for tag, color in colors.items():
            self.tree.tag_configure(tag, foreground=color)

    @staticmethod
    def _get_device_tag(roles):
        """Return the treeview tag for the most significant role."""
        for role in ("gpu", "gpu_root_port", "audio", "storage", "nic", "usb_controller"):
            if role in roles:
                return _ROLE_TAG.get(role)
        return None

    # ── Core selector (tile-based) ─────────────────────────────────────────────

    def build_core_selector(self):
        for tile in self._core_tiles:
            tile.destroy()
        self._core_tiles.clear()
        self.core_vars.clear()
        self.checkbuttons.clear()

        # Prefer topology-derived efficiency classes for Intel hybrid label hints.
        pe_labels = {}
        eff_map = (self.topology_snapshot or {}).get("efficiency_class_map_group0", {})
        if self.cpu_arch == "intel_hybrid" and eff_map:
            perf_class = min(int(k) for k in eff_map.keys())
            perf_set = set(int(x) for x in eff_map.get(perf_class, []))
            for lp in range(self.logical_processors):
                pe_labels[lp] = "P" if lp in perf_set else "E"

        _cols = 8
        for i in range(self.logical_processors):
            var = tk.BooleanVar(value=False)
            self.core_vars.append(var)

            badge = pe_labels.get(i)
            if badge:
                label = f"CPU {i}\n[{badge}]"
            else:
                label = f"CPU {i}"

            btn = ctk.CTkButton(
                self.core_tile_frame,
                text=label,
                width=72,
                height=52,
                corner_radius=6,
                border_width=2,
                border_color=_TILE_NORMAL,
                fg_color=_TILE_NORMAL,
                hover_color=_TILE_HOVER,
                command=lambda idx=i: self._toggle_core(idx),
                font=ctk.CTkFont("Segoe UI", 10),
            )
            btn.grid(row=i // _cols, column=i % _cols, padx=4, pady=4)
            self._core_tiles.append(btn)
            self.checkbuttons.append(btn)

        self.recommended_cores = self.get_recommended_cores(self.current_instance_id)
        self.highlight_recommendations()

    def _toggle_core(self, idx: int):
        self.core_vars[idx].set(not self.core_vars[idx].get())
        self._refresh_tile(idx)

    def _refresh_tile(self, idx: int):
        btn = self._core_tiles[idx]
        selected      = self.core_vars[idx].get()
        is_recommended = idx in self.recommended_cores

        tile_color = _TILE_SELECTED if selected else _TILE_NORMAL
        btn.configure(fg_color=tile_color)
        if is_recommended:
            btn.configure(border_color=_RECOMMEND_BORDER, border_width=2)
        else:
            btn.configure(border_color=tile_color, border_width=2)

    def highlight_recommendations(self):
        for i in range(len(self._core_tiles)):
            self._refresh_tile(i)

    def _device_query_script(self):
        return r"""
$devices = Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPDeviceID -and $_.Name }
$output = foreach ($dev in $devices) {
  $parent = $null
  try {
    $parent = (Get-PnpDeviceProperty -InstanceId $dev.PNPDeviceID -KeyName 'DEVPKEY_Device_Parent' -ErrorAction Stop).Data
  } catch {}
  [PSCustomObject]@{
    Name = $dev.Name
    PNPClass = $dev.PNPClass
    PNPDeviceID = $dev.PNPDeviceID
    ParentInstanceId = $parent
  }
}
$output | Sort-Object Name | ConvertTo-Json -Depth 3
""".strip()

    def load_devices(self):
        self._start_busy()
        self.tree.delete(*self.tree.get_children())
        self.device_entries = []
        self.device_roles = {}
        self.tree_item_by_instance = {}
        result_queue: queue.Queue = queue.Queue()

        def _worker():
            try:
                result = self._run_powershell(self._device_query_script())
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or "Failed to query devices")
                payload = result.stdout.strip()
                if not payload:
                    raise RuntimeError("No devices returned")
                data = json.loads(payload)
                if isinstance(data, dict):
                    data = [data]
                entries = []
                for item in data:
                    name = item.get("Name") or "(Unnamed Device)"
                    cls = item.get("PNPClass") or "Unknown"
                    instance = item.get("PNPDeviceID")
                    parent = item.get("ParentInstanceId")
                    if not instance:
                        continue
                    entries.append(
                        {"name": name, "class": cls, "instance": instance, "parent": parent}
                    )
                result_queue.put(("ok", entries))
            except Exception as exc:
                result_queue.put(("err", exc))

        def _on_done():
            try:
                kind, value = result_queue.get_nowait()
            except queue.Empty:
                self.root.after(50, _on_done)
                return
            if kind == "err":
                self.status_var.set("Failed to load devices")
                messagebox.showerror("Device load failed", str(value))
            else:
                self.device_entries = value
                self.device_roles = self.build_device_role_map(self.device_entries)
                for entry in self.device_entries:
                    instance = entry["instance"]
                    roles = self.device_roles.get(self._normalize_instance_id(instance), set())
                    label = self.role_label(roles)
                    disp_name = f"[{label}] {entry['name']}" if label else entry["name"]
                    tag = self._get_device_tag(roles)
                    item_id = self.tree.insert(
                        "", "end",
                        values=(disp_name, entry["class"], instance),
                        tags=(tag,) if tag else (),
                    )
                    self.tree_item_by_instance[instance] = item_id
                self._device_sort_desc = False
                self.sort_devices_by_type(toggle=False)
                self.update_recommendation_text()
                status = f"Loaded {len(self.tree.get_children())} devices"
                if self.group_limit_active:
                    status += " (Group 0 only; LP > 63 hidden)"
                self.status_var.set(status)
            self._stop_busy()

        threading.Thread(target=_worker, daemon=True).start()
        self.root.after(100, _on_done)

    def on_device_select(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        selected_item = self.tree.focus() if self.tree.focus() in selected else selected[0]
        values = self.tree.item(selected_item, "values")
        if len(values) < 3:
            return
        self.current_instance_id = values[2]
        self.update_core_display_for_device(self.current_instance_id)

    def _find_entry_by_instance_id(self, instance_id):
        target = self._normalize_instance_id(instance_id)
        for entry in self.device_entries:
            if self._normalize_instance_id(entry.get("instance")) == target:
                return entry
        return None

    def _get_primary_role_priority(self, roles):
        ordered = self.get_active_role_order() + ["pcie_root_port"]
        for idx, role in enumerate(ordered):
            if role in roles:
                return idx
        return len(ordered)

    def sort_devices_by_type(self, toggle=True):
        items = list(self.tree.get_children(""))
        if not items:
            return
        if toggle:
            self._device_sort_desc = not self._device_sort_desc

        sortable = []
        for item_id in items:
            values = self.tree.item(item_id, "values")
            if len(values) < 3:
                continue
            disp_name, cls_name, instance = values[0], values[1], values[2]
            roles = self.device_roles.get(self._normalize_instance_id(instance), set())
            primary_priority = self._get_primary_role_priority(roles)
            sortable.append(
                (
                    primary_priority,
                    cls_name.lower(),
                    disp_name.lower(),
                    instance.lower(),
                    item_id,
                )
            )

        sortable.sort(reverse=self._device_sort_desc)
        for idx, row in enumerate(sortable):
            self.tree.move(row[-1], "", idx)

        order_text = "descending" if self._device_sort_desc else "ascending"
        self.status_var.set(f"Sorted devices by type ({order_text})")

    def on_device_double_click(self, event):
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return
        self.tree.selection_set(item_id)
        self.tree.focus(item_id)
        self.on_device_select()

        values = self.tree.item(item_id, "values")
        if len(values) < 3:
            return
        instance_id = values[2]
        entry = self._find_entry_by_instance_id(instance_id)
        roles = self.device_roles.get(self._normalize_instance_id(instance_id), set())
        role_text = self.role_label(roles) or "UNCLASSIFIED"

        state = self.read_affinity_values(instance_id)
        assignment = state.get("assignment")
        if isinstance(assignment, (bytes, bytearray)):
            affinity_mask = hex(int.from_bytes(assignment, byteorder="little", signed=False))
        else:
            affinity_mask = "Not set"
        device_policy = state.get("device_policy")
        policy_text = str(device_policy) if device_policy is not None else "Not set"

        detail_lines = [
            f"Name: {(entry or {}).get('name', values[0])}",
            f"Type: {role_text}",
            f"Class: {(entry or {}).get('class', values[1])}",
            f"InstanceID: {instance_id}",
            f"Parent InstanceID: {(entry or {}).get('parent') or 'N/A'}",
            f"Current IRQ Mask: {affinity_mask}",
            f"DevicePolicy: {policy_text}",
        ]
        messagebox.showinfo("Device Details", "\n".join(detail_lines))

    def get_selected_instance_ids(self):
        selected_items = self.tree.selection()
        selected_instances = []
        for item in selected_items:
            values = self.tree.item(item, "values")
            if len(values) >= 3 and values[2] and values[2] not in selected_instances:
                selected_instances.append(values[2])
        return selected_instances

    def update_core_display_for_device(self, instance_id):
        for var in self.core_vars:
            var.set(False)

        self.recommended_cores = self.get_recommended_cores(instance_id)
        self.highlight_recommendations()

        state = self.read_affinity_values(instance_id)
        assignment = state.get("assignment")
        if not isinstance(assignment, (bytes, bytearray)):
            self.status_var.set("Device selected (no custom IRQ mask set)")
            return

        unusual_len_notice = ""
        if len(assignment) not in (4, 8):
            unusual_len_notice = f", unusual affinity length={len(assignment)} bytes"

        mask = int.from_bytes(assignment, byteorder="little", signed=False)
        for i, var in enumerate(self.core_vars):
            var.set(bool(mask & (1 << i)))

        for i in range(len(self._core_tiles)):
            self._refresh_tile(i)

        self.status_var.set(f"Device selected (current mask: {hex(mask)}{unusual_len_notice})")

    def get_selected_cores(self):
        selected = [i for i, var in enumerate(self.core_vars) if var.get()]
        if not selected:
            raise ValueError("Select at least one CPU core.")
        if any(i < 0 or i >= self.logical_processors for i in selected):
            raise ValueError("Selected core index is out of range.")
        return selected

    def mask_from_cores(self, cores):
        mask = 0
        for core in cores:
            if core >= self.MAX_GROUP_CORES:
                raise ValueError("Only processor group 0 (up to 64 cores) is supported.")
            mask |= 1 << core
        return mask

    def apply_affinity(self):
        selected_instances = self.get_selected_instance_ids()
        if not selected_instances:
            messagebox.showwarning("No device", "Select at least one device first.")
            return

        if not messagebox.askyesno(
            "Confirm IRQ affinity change",
            "This will modify HKLM registry IRQ affinity settings for the selected device(s).\n\n"
            "Incorrect settings may cause USB/audio/network/storage/GPU instability until reverted.\n"
            "Use Undo Focused or Reset Focused if needed.\n\n"
            f"Selected device count: {len(selected_instances)}\n\n"
            "Continue?",
        ):
            return

        self._start_busy()
        try:
            cores = self.get_selected_cores()
            mask_int = self.mask_from_cores(cores)
            assignment_bytes = mask_int.to_bytes(8, "little")
            succeeded = []
            for instance_id in selected_instances:
                try:
                    current_state = self.read_affinity_values(instance_id)
                    self.backup_current_state(instance_id, current_state, mask_int, cores)
                    self.write_affinity_values(instance_id, assignment_bytes, device_policy=4)

                    verify_state = self.read_affinity_values(instance_id)
                    verify_assignment = verify_state.get("assignment")
                    verify_policy = verify_state.get("device_policy")
                    if not isinstance(verify_assignment, (bytes, bytearray)):
                        raise RuntimeError("AssignmentSetOverride missing after write.")
                    if int.from_bytes(verify_assignment, "little") != mask_int:
                        raise RuntimeError("Written IRQ mask does not match.")
                    if int(verify_policy) != 4:
                        raise RuntimeError("DevicePolicy is not 4.")
                    succeeded.append(instance_id)
                except Exception as exc:
                    applied_count = len(succeeded)
                    total_count = len(selected_instances)
                    self.status_var.set(
                        f"Apply partially failed ({applied_count}/{total_count}) at device {instance_id}"
                    )
                    messagebox.showerror(
                        "Apply partially failed" if applied_count else "Apply failed",
                        f"Applied: {applied_count} / {total_count} device(s)\n"
                        f"Failed at: {instance_id}\n"
                        f"Reason: {exc}",
                    )
                    return

            if self.current_instance_id and self.current_instance_id in selected_instances:
                self.update_core_display_for_device(self.current_instance_id)
            self.status_var.set(
                f"Applied IRQ mask {hex(mask_int)} to {len(selected_instances)} selected device(s)"
            )
            messagebox.showinfo(
                "Success",
                f"IRQ affinity applied to {len(selected_instances)} device(s). Mask: {hex(mask_int)}\n\n"
                "⚠️ 변경 사항을 실제로 적용하려면 시스템을 재부팅하세요.",
            )
        except Exception as exc:
            messagebox.showerror("Apply failed", str(exc))
        finally:
            self._stop_busy()

    def factory_reset(self):
        if not self.current_instance_id:
            messagebox.showwarning("No device", "Select a device first.")
            return

        if not messagebox.askyesno(
            "Confirm factory reset",
            "This will remove custom IRQ affinity values for the focused/current device only. Continue?",
        ):
            return

        self._start_busy()
        try:
            current_state = self.read_affinity_values(self.current_instance_id)
            self.backup_current_state(self.current_instance_id, current_state, None, None)

            try:
                self.delete_value(self.current_instance_id, "AssignmentSetOverride")
            except FileNotFoundError:
                pass
            try:
                self.delete_value(self.current_instance_id, "DevicePolicy")
            except FileNotFoundError:
                pass

            verify_state = self.read_affinity_values(self.current_instance_id)
            if verify_state.get("had_affinity_key"):
                raise RuntimeError("Factory reset verification failed: AssignmentSetOverride still exists.")
            if verify_state.get("had_device_policy_key"):
                raise RuntimeError("Factory reset verification failed: DevicePolicy still exists.")

            self.update_core_display_for_device(self.current_instance_id)
            self.status_var.set("Reset completed for focused/current device")
            messagebox.showinfo(
                "Reset focused device",
                "Custom IRQ affinity was removed for the focused/current device.\n\n"
                "⚠️ 변경 사항을 실제로 적용하려면 시스템을 재부팅하세요.",
            )
        except Exception as exc:
            messagebox.showerror("Factory reset failed", str(exc))
        finally:
            self._stop_busy()

    def undo_last_change(self):
        if not self.current_instance_id:
            messagebox.showwarning("No device", "Select a device first.")
            return

        backup = self.load_backup()
        entry = backup.get("entries", {}).get(self.current_instance_id)
        if not isinstance(entry, dict):
            messagebox.showwarning("Undo unavailable", "No backup found for focused/current device.")
            return

        self._start_busy()
        try:
            had_affinity_key = bool(entry.get("had_affinity_key", False))
            had_device_policy_key = bool(entry.get("had_device_policy_key", False))
            previous_policy = entry.get("previous_device_policy")
            previous_assignment_hex = entry.get("previous_assignment_hex")

            if had_affinity_key:
                if not previous_assignment_hex:
                    raise RuntimeError("Backup is corrupted: missing previous assignment bytes.")
                previous_assignment = bytes.fromhex(previous_assignment_hex)
                policy_to_restore = int(previous_policy) if previous_policy is not None else 4
                self.write_affinity_values(self.current_instance_id, previous_assignment, policy_to_restore)
            else:
                try:
                    self.delete_value(self.current_instance_id, "AssignmentSetOverride")
                except FileNotFoundError:
                    pass

                if had_device_policy_key and previous_policy is not None:
                    reg_path = self.get_reg_path(self.current_instance_id)
                    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, reg_path, 0, winreg.KEY_SET_VALUE) as key:
                        winreg.SetValueEx(key, "DevicePolicy", 0, winreg.REG_DWORD, int(previous_policy))
                else:
                    try:
                        self.delete_value(self.current_instance_id, "DevicePolicy")
                    except FileNotFoundError:
                        pass

            verify_state = self.read_affinity_values(self.current_instance_id)
            if had_affinity_key:
                restored = verify_state.get("assignment")
                if not isinstance(restored, (bytes, bytearray)):
                    raise RuntimeError("Undo verification failed: affinity value not restored.")
                if restored.hex() != previous_assignment_hex.lower():
                    raise RuntimeError("Undo verification failed: restored affinity differs from backup.")
            else:
                if verify_state.get("had_affinity_key"):
                    raise RuntimeError("Undo verification failed: affinity key should not exist.")

            self.update_core_display_for_device(self.current_instance_id)
            self.status_var.set("Undo completed for focused/current device")
            messagebox.showinfo(
                "Undo focused device",
                "Previous state was restored successfully for the focused/current device.\n\n"
                "⚠️ 변경 사항을 실제로 적용하려면 시스템을 재부팅하세요.",
            )
        except Exception as exc:
            messagebox.showerror("Undo failed", str(exc))
        finally:
            self._stop_busy()

    def select_recommended_cores(self):
        for i, var in enumerate(self.core_vars):
            var.set(i in self.recommended_cores)
        for i in range(len(self._core_tiles)):
            self._refresh_tile(i)
        self.status_var.set("Recommended cores selected")


def main():
    if not is_admin():
        relaunch_as_admin()
        sys.exit(0)

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    app = IRQOptimizerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
