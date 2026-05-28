import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime

import tkinter as tk
from tkinter import messagebox, ttk

try:
    import winreg
except ImportError as exc:  # pragma: no cover - runtime guard for non-Windows hosts
    raise RuntimeError("This application requires Windows (winreg module).") from exc


APP_VERSION = "1.0.0"
BACKUP_SCHEMA_VERSION = 1


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
        self.root.geometry("920x860")
        self.root.resizable(False, False)

        self.core_vars = []
        self.checkbuttons = []
        self.recommended_cores = []
        self.current_instance_id = None
        self.device_entries = []
        self.device_roles = {}
        self.tree_item_by_instance = {}

        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        backup_dir = os.path.join(program_data, "IRQOptimizer")
        os.makedirs(backup_dir, exist_ok=True)
        self.backup_file = os.path.join(backup_dir, "irq_backup.json")

        self.cpu_info = self.get_cpu_info()
        self.physical_cores, self.logical_processors = self.get_core_counts()
        self.logical_processors = max(1, min(self.logical_processors, self.MAX_GROUP_CORES))
        self.is_smt_enabled = self.logical_processors > self.physical_cores
        self.cpu_arch = self.classify_cpu()
        self.locality_groups = self.build_locality_groups()
        self.topology_snapshot = self.get_topology_snapshot()

        self.create_widgets()
        self.load_devices()

    def _run_powershell(self, script):
        flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW
        return subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            creationflags=flags,
        )

    def get_cpu_info(self):
        try:
            result = self._run_powershell("(Get-CimInstance Win32_Processor).Name")
            if result.returncode == 0:
                return result.stdout.strip() or "Unknown CPU"
        except Exception:
            pass
        return "Unknown CPU"

    def get_core_counts(self):
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

    def build_locality_groups(self):
        total = self.logical_processors
        if total <= 1:
            return [[0]]

        phys_limit = min(max(1, self.physical_cores), total)
        physical_cores = list(range(phys_limit))

        if self.cpu_arch == "amd_dual_x3d" and phys_limit >= 8:
            split = max(1, phys_limit // 2)
            groups = [physical_cores[:split], physical_cores[split:]]
            return [g for g in groups if g]

        if self.cpu_arch == "intel_hybrid" and phys_limit >= 6:
            p_core_guess = max(2, phys_limit // 2)
            groups = [physical_cores[:p_core_guess], physical_cores[p_core_guess:]]
            return [g for g in groups if g]

        return [physical_cores]

    def get_topology_snapshot(self):
        fallback = {
            "socket_count": 1,
            "numa_node_count": 1,
            "numa_logical_processors": [self.logical_processors],
            "locality_groups": [list(g) for g in self.locality_groups],
            "primary_group": list(self.locality_groups[0]) if self.locality_groups else [0],
            "secondary_group": list(self.locality_groups[1][:2]) if len(self.locality_groups) > 1 else [],
        }
        try:
            script = r"""
$processors = Get-CimInstance Win32_Processor | Select-Object Name,Manufacturer,NumberOfCores,NumberOfLogicalProcessors
$numa = Get-CimInstance Win32_NumaNode | Select-Object NodeNumber,NumberOfLogicalProcessors
[PSCustomObject]@{
  Processors = $processors
  NumaNodes = $numa
} | ConvertTo-Json -Depth 5
""".strip()
            result = self._run_powershell(script)
            if result.returncode != 0 or not result.stdout.strip():
                return fallback

            data = json.loads(result.stdout.strip())
            procs = data.get("Processors") if isinstance(data, dict) else None
            numa = data.get("NumaNodes") if isinstance(data, dict) else None

            if isinstance(procs, dict):
                procs = [procs]
            if isinstance(numa, dict):
                numa = [numa]

            socket_count = len(procs) if isinstance(procs, list) and procs else 1
            numa_nodes = (
                [int(x.get("NumberOfLogicalProcessors", 0) or 0) for x in numa]
                if isinstance(numa, list)
                else []
            )
            numa_nodes = [x for x in numa_nodes if x > 0]
            numa_node_count = max(1, len(numa_nodes))

            snapshot = dict(fallback)
            snapshot["socket_count"] = socket_count
            snapshot["numa_node_count"] = numa_node_count
            snapshot["numa_logical_processors"] = (
                numa_nodes if numa_nodes else [self.logical_processors]
            )
            return snapshot
        except Exception:
            return fallback

    def analyze_cpu_topology(self):
        smt = "On" if self.is_smt_enabled else "Off"
        topo = self.topology_snapshot or {}
        guide = (
            f"CPU: {self.cpu_info}\n"
            f"Cores: Physical {self.physical_cores} / Logical {self.logical_processors}\n"
            f"SMT/HT: {smt}\n"
            f"Architecture profile: {self.cpu_arch}\n"
            f"Sockets: {topo.get('socket_count', 1)}, NUMA nodes: {topo.get('numa_node_count', 1)}\n"
            f"Locality groups: {topo.get('locality_groups', self.locality_groups)}\n\n"
        )
        msg = {
            "intel_hybrid": "Recommended cores focus on likely P-cores (typically lower index cores).",
            "intel_legacy": "Recommended cores spread across physical cores.",
            "amd_dual_x3d": "Recommended cores bias first CCD range (verify with your BIOS topology).",
            "amd_single_x3d": "Recommended cores spread across available cores.",
            "amd_generic": "Recommended cores spread across physical cores.",
            "unknown": "Default conservative spread recommendation is used.",
        }
        return guide + msg.get(self.cpu_arch, msg["unknown"])

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
        winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path, 0, winreg.KEY_SET_VALUE) as key:
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

    def get_recommended_cores(self):
        topo = self.topology_snapshot or {}
        primary_group = [x for x in topo.get("primary_group", []) if 0 <= x < self.logical_processors]
        if primary_group:
            if self.is_smt_enabled and len(primary_group) >= 4:
                reduced = primary_group[::2]
                return reduced or [primary_group[0]]
            return primary_group

        total = self.logical_processors
        if total <= 2:
            return list(range(total))

        phys_limit = min(max(1, self.physical_cores), total)

        if self.cpu_arch in {"intel_hybrid", "amd_dual_x3d"}:
            return list(range(min(phys_limit, max(2, total // 2))))

        step = 2 if self.is_smt_enabled and total >= 4 else 1
        rec = list(range(0, phys_limit, step))
        return rec or [0]

    @staticmethod
    def _normalize_instance_id(instance_id):
        return (instance_id or "").strip().lower()

    def classify_device_base_roles(self, name, pnp_class, instance_id):
        name_l = (name or "").lower()
        cls_l = (pnp_class or "").lower()
        inst_l = self._normalize_instance_id(instance_id)
        roles = set()

        gpu_keys = ("nvidia", "geforce", "rtx", "gtx", "quadro", "radeon", "amd", "arc")
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
        if cls_l == "usb" and any(k in name_l for k in usb_controller_keys):
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
        role_groups = {"gpu": [], "gpu_root_port": [], "usb_controller": [], "audio": [], "storage": [], "nic": []}
        for entry in self.device_entries:
            iid = self._normalize_instance_id(entry["instance"])
            roles = self.device_roles.get(iid, set())
            for role in role_groups:
                if role in roles:
                    role_groups[role].append(entry["name"])

        topo = self.topology_snapshot or {}
        base = self.get_recommended_cores()
        primary = [x for x in topo.get("primary_group", []) if x in base]
        gpu_group = (primary or base)[: min(4, len(primary or base))]
        if not gpu_group:
            gpu_group = [0]

        adjacent_core = None
        if self.logical_processors > 1:
            candidate = gpu_group[-1] + 1
            adjacent_core = candidate if candidate < self.logical_processors else max(0, gpu_group[0] - 1)

        secondary = [x for x in topo.get("secondary_group", []) if 0 <= x < self.logical_processors]
        side_group = secondary or [x for x in base if x not in gpu_group][:2]
        if not side_group:
            side_group = [adjacent_core] if adjacent_core is not None else [gpu_group[0]]

        lines = [
            "Target device recommendation (priority): GPU → GPU Root Port → USB Controller → Audio → Storage → NIC",
            f"Detected GPU: {len(role_groups['gpu'])}, GPU Root Port: {len(role_groups['gpu_root_port'])}, "
            f"USB Controller: {len(role_groups['usb_controller'])}, Audio: {len(role_groups['audio'])}, "
            f"Storage: {len(role_groups['storage'])}, NIC: {len(role_groups['nic'])}",
            f"Topology summary: Sockets {topo.get('socket_count', 1)}, NUMA nodes {topo.get('numa_node_count', 1)}, "
            f"Primary locality {topo.get('primary_group', [])}",
            "",
            "Core placement policy:",
            f"- GPU: use primary nearby physical-core group {gpu_group}",
            "- GPU Root Port: prefer adjacent core(s) in same locality (not forced to exact same single core)",
            f"  Suggested adjacent core: {adjacent_core if adjacent_core is not None else gpu_group[0]}",
            f"- USB Controller: prefer low-contention nearby side core(s) {side_group}",
            f"- Audio/Storage/NIC: place on nearby but separate side cores when possible {side_group}",
            "",
            "Reasoning: forcing GPU and PCIe Root Port onto one identical core can increase IRQ contention.",
            "Using adjacent physical cores in the same locality usually balances cache/topology proximity and contention.",
        ]
        return "\n".join(lines)

    def update_recommendation_text(self):
        self.recommend_text.configure(state="normal")
        self.recommend_text.delete("1.0", tk.END)
        self.recommend_text.insert("1.0", self.build_target_strategy_text())
        self.recommend_text.configure(state="disabled")

    def select_target_devices(self):
        self.tree.selection_remove(*self.tree.selection())
        selected_ids = []
        for instance_id, item_id in self.tree_item_by_instance.items():
            roles = self.device_roles.get(self._normalize_instance_id(instance_id), set())
            if roles & {"gpu", "gpu_root_port", "usb_controller", "audio", "storage", "nic"}:
                selected_ids.append(item_id)
        for item_id in selected_ids:
            self.tree.selection_add(item_id)
        if selected_ids:
            self.tree.focus(selected_ids[0])
            self.tree.see(selected_ids[0])
            self.on_device_select()
        self.status_var.set(f"Selected {len(selected_ids)} recommended target devices")

    def create_widgets(self):
        top_frame = tk.Frame(self.root)
        top_frame.pack(fill=tk.X, padx=14, pady=(10, 6))

        info = tk.Label(
            top_frame,
            text="Select a device, choose CPU cores, then Apply IRQ Affinity.",
            fg="#1d4ed8",
            font=("Segoe UI", 10, "bold"),
        )
        info.pack(anchor="w")

        columns = ("Name", "Class", "InstanceID")
        self.tree = ttk.Treeview(self.root, columns=columns, show="headings", height=10)
        self.tree.heading("Name", text="Device")
        self.tree.heading("Class", text="Class")
        self.tree.heading("InstanceID", text="InstanceID")
        self.tree.column("Name", width=600)
        self.tree.column("Class", width=140)
        self.tree.column("InstanceID", width=0, stretch=False)
        self.tree.pack(fill=tk.X, padx=14)
        self.tree.bind("<<TreeviewSelect>>", self.on_device_select)

        controls = tk.Frame(self.root)
        controls.pack(fill=tk.X, padx=14, pady=8)
        tk.Button(controls, text="Refresh Devices", command=self.load_devices, width=18).pack(side=tk.LEFT, padx=4)
        tk.Button(controls, text="Select Target Devices", command=self.select_target_devices, width=20).pack(side=tk.LEFT, padx=4)
        tk.Button(controls, text="Use Recommended", command=self.select_recommended_cores, width=18).pack(side=tk.LEFT, padx=4)
        tk.Button(controls, text="Apply", command=self.apply_affinity, width=12, bg="#16a34a", fg="white").pack(side=tk.LEFT, padx=4)
        tk.Button(controls, text="Factory Reset", command=self.factory_reset, width=14, bg="#dc2626", fg="white").pack(side=tk.LEFT, padx=4)
        tk.Button(controls, text="Undo Last", command=self.undo_last_change, width=12).pack(side=tk.LEFT, padx=4)

        cpu_box = tk.LabelFrame(self.root, text="CPU Topology")
        cpu_box.pack(fill=tk.X, padx=14, pady=6)
        self.cpu_text = tk.Text(cpu_box, height=6, wrap="word", font=("Consolas", 10))
        self.cpu_text.pack(fill=tk.X, padx=6, pady=6)
        self.cpu_text.insert("1.0", self.analyze_cpu_topology())
        self.cpu_text.configure(state="disabled")

        recommend_box = tk.LabelFrame(self.root, text="Target Recommendation")
        recommend_box.pack(fill=tk.X, padx=14, pady=6)
        self.recommend_text = tk.Text(recommend_box, height=9, wrap="word", font=("Consolas", 10))
        self.recommend_text.pack(fill=tk.X, padx=6, pady=6)
        self.recommend_text.insert("1.0", "Recommendation will be generated after device scan.")
        self.recommend_text.configure(state="disabled")

        core_box = tk.LabelFrame(self.root, text="Core Selection (group 0)")
        core_box.pack(fill=tk.BOTH, expand=True, padx=14, pady=6)
        self.core_canvas = tk.Canvas(core_box, height=300)
        self.core_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(core_box, orient="vertical", command=self.core_canvas.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.core_canvas.configure(yscrollcommand=scrollbar.set)

        self.core_frame = tk.Frame(self.core_canvas)
        self.core_canvas.create_window((0, 0), window=self.core_frame, anchor="nw")
        self.core_frame.bind(
            "<Configure>",
            lambda e: self.core_canvas.configure(scrollregion=self.core_canvas.bbox("all")),
        )

        self.status_var = tk.StringVar(value="Ready")
        tk.Label(self.root, textvariable=self.status_var, anchor="w").pack(fill=tk.X, padx=14, pady=(2, 8))

        self.build_core_selector()

    def build_core_selector(self):
        for cb in self.checkbuttons:
            cb.destroy()
        self.core_vars.clear()
        self.checkbuttons.clear()

        for i in range(self.logical_processors):
            var = tk.BooleanVar(value=False)
            cb = tk.Checkbutton(self.core_frame, text=f"CPU {i}", variable=var)
            row = i // 8
            col = i % 8
            cb.grid(row=row, column=col, padx=8, pady=6, sticky="w")
            self.core_vars.append(var)
            self.checkbuttons.append(cb)

        self.recommended_cores = self.get_recommended_cores()
        self.highlight_recommendations()

    def highlight_recommendations(self):
        for i, cb in enumerate(self.checkbuttons):
            if i in self.recommended_cores:
                cb.configure(bg="#fef08a")
            else:
                cb.configure(bg=self.root.cget("bg"))

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
        self.tree.delete(*self.tree.get_children())
        self.device_entries = []
        self.device_roles = {}
        self.tree_item_by_instance = {}
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

            for item in data:
                name = item.get("Name") or "(Unnamed Device)"
                cls = item.get("PNPClass") or "Unknown"
                instance = item.get("PNPDeviceID")
                parent = item.get("ParentInstanceId")
                if not instance:
                    continue
                self.device_entries.append(
                    {"name": name, "class": cls, "instance": instance, "parent": parent}
                )

            self.device_roles = self.build_device_role_map(self.device_entries)
            for entry in self.device_entries:
                instance = entry["instance"]
                roles = self.device_roles.get(self._normalize_instance_id(instance), set())
                label = self.role_label(roles)
                disp_name = f"[{label}] {entry['name']}" if label else entry["name"]
                item_id = self.tree.insert("", "end", values=(disp_name, entry["class"], instance))
                self.tree_item_by_instance[instance] = item_id

            self.update_recommendation_text()

            self.status_var.set(f"Loaded {len(self.tree.get_children())} devices")
        except Exception as exc:
            self.status_var.set("Failed to load devices")
            messagebox.showerror("Device load failed", str(exc))

    def on_device_select(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        values = self.tree.item(selected[0], "values")
        if len(values) < 3:
            return
        self.current_instance_id = values[2]
        self.update_core_display_for_device(self.current_instance_id)

    def update_core_display_for_device(self, instance_id):
        for var in self.core_vars:
            var.set(False)

        self.highlight_recommendations()

        state = self.read_affinity_values(instance_id)
        assignment = state.get("assignment")
        if not isinstance(assignment, (bytes, bytearray)):
            self.status_var.set("Device selected (no custom IRQ mask set)")
            return

        mask = int.from_bytes(assignment, byteorder="little", signed=False)
        for i, var in enumerate(self.core_vars):
            var.set(bool(mask & (1 << i)))
        self.status_var.set(f"Device selected (current mask: {hex(mask)})")

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
        if not self.current_instance_id:
            messagebox.showwarning("No device", "Select a device first.")
            return

        try:
            cores = self.get_selected_cores()
            mask_int = self.mask_from_cores(cores)
            assignment_bytes = mask_int.to_bytes(max(1, (mask_int.bit_length() + 7) // 8), "little")

            current_state = self.read_affinity_values(self.current_instance_id)
            self.backup_current_state(self.current_instance_id, current_state, mask_int, cores)

            self.write_affinity_values(self.current_instance_id, assignment_bytes, device_policy=4)

            verify_state = self.read_affinity_values(self.current_instance_id)
            verify_assignment = verify_state.get("assignment")
            verify_policy = verify_state.get("device_policy")
            if not isinstance(verify_assignment, (bytes, bytearray)):
                raise RuntimeError("Verification failed: AssignmentSetOverride missing after write.")
            if int.from_bytes(verify_assignment, "little") != mask_int:
                raise RuntimeError("Verification failed: written IRQ mask does not match.")
            if int(verify_policy) != 4:
                raise RuntimeError("Verification failed: DevicePolicy is not 4.")

            self.update_core_display_for_device(self.current_instance_id)
            self.status_var.set(f"Applied IRQ mask {hex(mask_int)} to selected device")
            messagebox.showinfo("Success", f"IRQ affinity applied. Mask: {hex(mask_int)}")
        except Exception as exc:
            messagebox.showerror("Apply failed", str(exc))

    def factory_reset(self):
        if not self.current_instance_id:
            messagebox.showwarning("No device", "Select a device first.")
            return

        if not messagebox.askyesno(
            "Confirm factory reset",
            "This will remove custom IRQ affinity values for the selected device. Continue?",
        ):
            return

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

            self.update_core_display_for_device(self.current_instance_id)
            self.status_var.set("Factory reset completed for selected device")
            messagebox.showinfo("Factory reset", "Custom IRQ affinity was removed for the selected device.")
        except Exception as exc:
            messagebox.showerror("Factory reset failed", str(exc))

    def undo_last_change(self):
        if not self.current_instance_id:
            messagebox.showwarning("No device", "Select a device first.")
            return

        backup = self.load_backup()
        entry = backup.get("entries", {}).get(self.current_instance_id)
        if not isinstance(entry, dict):
            messagebox.showwarning("Undo unavailable", "No backup found for selected device.")
            return

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
                    winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path, 0, winreg.KEY_SET_VALUE) as key:
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
            self.status_var.set("Undo completed for selected device")
            messagebox.showinfo("Undo", "Previous state was restored successfully.")
        except Exception as exc:
            messagebox.showerror("Undo failed", str(exc))

    def select_recommended_cores(self):
        for i, var in enumerate(self.core_vars):
            var.set(i in self.recommended_cores)
        self.status_var.set("Recommended cores selected")


def main():
    if not is_admin():
        relaunch_as_admin()
        sys.exit(0)

    root = tk.Tk()
    app = IRQOptimizerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
