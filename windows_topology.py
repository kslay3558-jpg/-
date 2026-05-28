import ctypes
from ctypes import wintypes


ERROR_INSUFFICIENT_BUFFER = 122

RelationProcessorCore = 0
RelationNumaNode = 1
RelationGroup = 4
RelationNumaNodeEx = 7


KAFFINITY = ctypes.c_size_t


class GROUP_AFFINITY(ctypes.Structure):
    _fields_ = [
        ("Mask", KAFFINITY),
        ("Group", wintypes.WORD),
        ("Reserved", wintypes.WORD * 3),
    ]


class SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX_HEADER(ctypes.Structure):
    _fields_ = [
        ("Relationship", wintypes.DWORD),
        ("Size", wintypes.DWORD),
    ]


class PROCESSOR_RELATIONSHIP_HEAD(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_byte),
        ("EfficiencyClass", ctypes.c_byte),
        ("Reserved", ctypes.c_byte * 20),
        ("GroupCount", wintypes.WORD),
    ]


class NUMA_NODE_RELATIONSHIP_HEAD(ctypes.Structure):
    _fields_ = [
        ("NodeNumber", wintypes.DWORD),
        ("Reserved", ctypes.c_byte * 20),
        ("GroupMask", GROUP_AFFINITY),
    ]


class NUMA_NODE_RELATIONSHIP_EX_HEAD(ctypes.Structure):
    _fields_ = [
        ("NodeNumber", wintypes.DWORD),
        ("Reserved", ctypes.c_byte * 18),
        ("GroupCount", wintypes.WORD),
    ]


class GROUP_RELATIONSHIP_HEAD(ctypes.Structure):
    _fields_ = [
        ("MaximumGroupCount", wintypes.WORD),
        ("ActiveGroupCount", wintypes.WORD),
        ("Reserved", ctypes.c_byte * 20),
    ]


class PROCESSOR_GROUP_INFO(ctypes.Structure):
    _fields_ = [
        ("MaximumProcessorCount", ctypes.c_byte),
        ("ActiveProcessorCount", ctypes.c_byte),
        ("Reserved", ctypes.c_byte * 38),
        ("ActiveProcessorMask", KAFFINITY),
    ]


class WindowsTopologyReader:
    """Reads CPU topology from GetLogicalProcessorInformationEx and normalizes it."""

    def __init__(self):
        # use_last_error=True is important for reliable ERROR_INSUFFICIENT_BUFFER handling.
        self._kernel32 = None
        if hasattr(ctypes, "WinDLL"):
            try:
                self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            except Exception:
                self._kernel32 = None

    @staticmethod
    def mask_to_logical_processors(mask, max_bits=64):
        logical = []
        m = int(mask or 0)
        for i in range(max_bits):
            if (m >> i) & 1:
                logical.append(i)
        return logical

    @staticmethod
    def expand_group_masks(group_masks):
        out = []
        for gm in group_masks or []:
            group = int(gm.get("group", 0) or 0)
            mask = int(gm.get("mask", 0) or 0)
            for number in WindowsTopologyReader.mask_to_logical_processors(mask):
                out.append({"group": group, "number": number})
        return out

    @staticmethod
    def filter_group0_logical_processors(logical_processors):
        return sorted(
            {int(lp.get("number", -1)) for lp in (logical_processors or []) if int(lp.get("group", -1)) == 0 and int(lp.get("number", -1)) >= 0}
        )

    @staticmethod
    def select_representative_group0_lp(core):
        lps = [
            int(lp.get("number", -1))
            for lp in (core or {}).get("logical_processors", [])
            if int(lp.get("group", -1)) == 0 and int(lp.get("number", -1)) >= 0
        ]
        return min(lps) if lps else None

    @staticmethod
    def classify_efficiency_classes(cores):
        by_class = {}
        for core in cores or []:
            cls = core.get("efficiency_class")
            if cls is None:
                continue
            rep = WindowsTopologyReader.select_representative_group0_lp(core)
            if rep is None:
                continue
            by_class.setdefault(int(cls), []).append(rep)
        for cls in by_class:
            by_class[cls] = sorted(set(by_class[cls]))
        return by_class

    def _read_relation_buffer(self, relationship):
        if self._kernel32 is None:
            return None, "kernel32 unavailable"

        fn = self._kernel32.GetLogicalProcessorInformationEx
        fn.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        fn.restype = wintypes.BOOL

        length = wintypes.DWORD(0)
        ok = fn(relationship, None, ctypes.byref(length))
        if ok:
            return b"", None

        err = ctypes.get_last_error()
        if err != ERROR_INSUFFICIENT_BUFFER or length.value <= 0:
            return None, f"GetLogicalProcessorInformationEx failed (relation={relationship}, err={err})"

        buf = (ctypes.c_byte * length.value)()
        ok = fn(relationship, ctypes.byref(buf), ctypes.byref(length))
        if not ok:
            err = ctypes.get_last_error()
            return None, f"GetLogicalProcessorInformationEx failed 2nd call (relation={relationship}, err={err})"
        return bytes(buf[: length.value]), None

    @staticmethod
    def _iter_records(raw):
        offset = 0
        total = len(raw)
        head_size = ctypes.sizeof(SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX_HEADER)
        while offset + head_size <= total:
            header = SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX_HEADER.from_buffer_copy(raw, offset)
            size = int(header.Size)
            if size <= 0 or offset + size > total:
                break
            yield int(header.Relationship), raw[offset : offset + size]
            offset += size

    @staticmethod
    def _parse_processor_cores(raw):
        # PROCESSOR_RELATIONSHIP is variable length (GroupMask[ANYSIZE_ARRAY]),
        # so parse the fixed head first and then read GroupCount masks manually.
        cores = []
        head_size = ctypes.sizeof(SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX_HEADER)
        rel_head_size = ctypes.sizeof(PROCESSOR_RELATIONSHIP_HEAD)
        ga_size = ctypes.sizeof(GROUP_AFFINITY)

        for relation, rec in WindowsTopologyReader._iter_records(raw):
            if relation != RelationProcessorCore or len(rec) < head_size + rel_head_size:
                continue
            rel = PROCESSOR_RELATIONSHIP_HEAD.from_buffer_copy(rec, head_size)
            group_count = int(rel.GroupCount)
            gms = []
            gm_offset = head_size + rel_head_size
            for i in range(max(0, group_count)):
                off = gm_offset + i * ga_size
                if off + ga_size > len(rec):
                    break
                gm = GROUP_AFFINITY.from_buffer_copy(rec, off)
                gms.append({"group": int(gm.Group), "mask": int(gm.Mask)})
            lps = WindowsTopologyReader.expand_group_masks(gms)
            cores.append(
                {
                    "core_index": len(cores),
                    "efficiency_class": int(rel.EfficiencyClass),
                    "flags": int(rel.Flags),
                    "group_masks": gms,
                    "logical_processors": lps,
                    "smt_width": max(1, len(lps)),
                }
            )
        return cores

    @staticmethod
    def _parse_group_relation(raw):
        # GROUP_RELATIONSHIP also has a trailing array of PROCESSOR_GROUP_INFO entries.
        groups = []
        head_size = ctypes.sizeof(SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX_HEADER)
        rel_head_size = ctypes.sizeof(GROUP_RELATIONSHIP_HEAD)
        info_size = ctypes.sizeof(PROCESSOR_GROUP_INFO)

        for relation, rec in WindowsTopologyReader._iter_records(raw):
            if relation != RelationGroup or len(rec) < head_size + rel_head_size:
                continue
            rel = GROUP_RELATIONSHIP_HEAD.from_buffer_copy(rec, head_size)
            active = int(rel.ActiveGroupCount)
            base = head_size + rel_head_size
            for i in range(max(0, active)):
                off = base + i * info_size
                if off + info_size > len(rec):
                    break
                info = PROCESSOR_GROUP_INFO.from_buffer_copy(rec, off)
                groups.append(
                    {
                        "group_id": i,
                        "maximum_processor_count": int(info.MaximumProcessorCount),
                        "active_processor_count": int(info.ActiveProcessorCount),
                        "active_mask": int(info.ActiveProcessorMask),
                    }
                )
            break
        return groups

    @staticmethod
    def _parse_numa_nodes_legacy(raw):
        # Legacy NUMA relation has a single GROUP_AFFINITY mask.
        nodes = []
        head_size = ctypes.sizeof(SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX_HEADER)
        rel_size = ctypes.sizeof(NUMA_NODE_RELATIONSHIP_HEAD)

        for relation, rec in WindowsTopologyReader._iter_records(raw):
            if relation != RelationNumaNode or len(rec) < head_size + rel_size:
                continue
            rel = NUMA_NODE_RELATIONSHIP_HEAD.from_buffer_copy(rec, head_size)
            gms = [{"group": int(rel.GroupMask.Group), "mask": int(rel.GroupMask.Mask)}]
            nodes.append(
                {
                    "node_number": int(rel.NodeNumber),
                    "group_masks": gms,
                    "logical_processors": WindowsTopologyReader.expand_group_masks(gms),
                }
            )
        return nodes

    @staticmethod
    def _parse_numa_nodes_ex(raw):
        # NUMA_NODE_RELATIONSHIP_EX is variable length (GroupMasks[GroupCount]).
        nodes = []
        head_size = ctypes.sizeof(SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX_HEADER)
        rel_head_size = ctypes.sizeof(NUMA_NODE_RELATIONSHIP_EX_HEAD)
        ga_size = ctypes.sizeof(GROUP_AFFINITY)

        for relation, rec in WindowsTopologyReader._iter_records(raw):
            if relation != RelationNumaNodeEx or len(rec) < head_size + rel_head_size:
                continue
            rel = NUMA_NODE_RELATIONSHIP_EX_HEAD.from_buffer_copy(rec, head_size)
            group_count = int(rel.GroupCount)
            gms = []
            base = head_size + rel_head_size
            for i in range(max(0, group_count)):
                off = base + i * ga_size
                if off + ga_size > len(rec):
                    break
                gm = GROUP_AFFINITY.from_buffer_copy(rec, off)
                gms.append({"group": int(gm.Group), "mask": int(gm.Mask)})
            nodes.append(
                {
                    "node_number": int(rel.NodeNumber),
                    "group_masks": gms,
                    "logical_processors": WindowsTopologyReader.expand_group_masks(gms),
                }
            )
        return nodes

    @staticmethod
    def _build_summary(groups, cores, numa_nodes):
        logical_all = set()
        logical_group0 = set()
        for core in cores:
            for lp in core.get("logical_processors", []):
                group = int(lp.get("group", 0))
                num = int(lp.get("number", -1))
                if num < 0:
                    continue
                logical_all.add((group, num))
                if group == 0:
                    logical_group0.add(num)

        smt_widths = sorted({int(c.get("smt_width", 1) or 1) for c in cores})
        eff = sorted({int(c.get("efficiency_class")) for c in cores if c.get("efficiency_class") is not None})

        group0_core_count = sum(1 for c in cores if any(int(lp.get("group", -1)) == 0 for lp in c.get("logical_processors", [])))

        return {
            "group_count": len(groups),
            "physical_core_count": len(cores),
            "physical_core_count_group0": group0_core_count,
            "logical_processor_count": len(logical_all),
            "logical_processor_count_group0": len(logical_group0),
            "numa_node_count": len(numa_nodes),
            "efficiency_classes": eff,
            "smt_widths": smt_widths,
        }

    def read_snapshot(self):
        snapshot = {
            "api_available": False,
            "source": "fallback heuristic",
            "groups": [],
            "cores": [],
            "numa_nodes": [],
            "efficiency_class_map_group0": {},
            "group0_representative_logical_processors": [],
            "summary": {
                "group_count": 0,
                "physical_core_count": 0,
                "physical_core_count_group0": 0,
                "logical_processor_count": 0,
                "logical_processor_count_group0": 0,
                "numa_node_count": 0,
                "efficiency_classes": [],
                "smt_widths": [],
            },
            "error": None,
        }

        try:
            core_raw, core_err = self._read_relation_buffer(RelationProcessorCore)
            group_raw, group_err = self._read_relation_buffer(RelationGroup)
            numa_raw, numa_err = self._read_relation_buffer(RelationNumaNode)
            numa_ex_raw, numa_ex_err = self._read_relation_buffer(RelationNumaNodeEx)

            if core_raw is None or group_raw is None:
                snapshot["error"] = core_err or group_err or "topology relation read failed"
                return snapshot

            cores = self._parse_processor_cores(core_raw)
            groups = self._parse_group_relation(group_raw)
            nodes = self._parse_numa_nodes_legacy(numa_raw or b"")
            if not nodes:
                nodes = self._parse_numa_nodes_ex(numa_ex_raw or b"")

            if not cores or not groups:
                snapshot["error"] = "topology parse incomplete"
                return snapshot

            summary = self._build_summary(groups, cores, nodes)
            rep_group0 = []
            for core in cores:
                rep = self.select_representative_group0_lp(core)
                if rep is not None:
                    rep_group0.append(rep)
            rep_group0 = sorted(set(rep_group0))

            eff_map = self.classify_efficiency_classes(cores)

            snapshot.update(
                {
                    "api_available": True,
                    "source": "Windows API",
                    "groups": groups,
                    "cores": cores,
                    "numa_nodes": nodes,
                    "efficiency_class_map_group0": eff_map,
                    "group0_representative_logical_processors": rep_group0,
                    "summary": summary,
                    "error": numa_err or numa_ex_err,
                }
            )
            return snapshot
        except Exception as exc:
            snapshot["error"] = str(exc)
            return snapshot
