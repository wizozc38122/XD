"""Generate the OCP cluster risk overview Grafana dashboard.

It ranks every workload and node in one cluster so the risky ones can be spotted first,
then opens the per-workload detail dashboard (gen_dashboard.py) for the evidence.

Usage: python gen_overview_dashboard.py ocp-cluster-risk-overview-dashboard.json
"""
import json
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "ocp-cluster-risk-overview-dashboard.json"
DS = {"type": "prometheus", "uid": "${datasource}"}
DETAIL_UID = "ocp-loadtest-impact"

NSF = 'namespace=~"$namespace", namespace!~"$exclude"'
W5 = "5m"                # tables: the last 5 minutes before the end of the time range
WR = "$__rate_interval"  # trend panels
DEV = 'device=~"sd[a-z]+|nvme[0-9]+n[0-9]+|vd[a-z]+|xvd[a-z]+"'
NIC = 'device=~"(ens|eth|eno|enp|bond)[a-z0-9]*"'
FS = 'mountpoint=~"/sysroot|/var|/", fstype!~"tmpfs|overlay|squashfs"'

WIN = "$waste_window"    # capacity / waste sections: averages and peaks over this many days before the end of the time range


def pod_to_wl(seen=lambda sel: sel):
    """Pod -> workload mapping: Deployments through their ReplicaSet, StatefulSets and DaemonSets directly.
    seen wraps each owner selector, e.g. to also map pods that were deleted earlier in a window."""
    rs_pods = seen(f'kube_pod_owner{{owner_kind="ReplicaSet", {NSF}}}')
    rs_deploy = seen(f'kube_replicaset_owner{{owner_kind="Deployment", {NSF}}}')
    other_pods = seen(f'kube_pod_owner{{owner_kind=~"StatefulSet|DaemonSet", {NSF}}}')
    return ('max by (namespace, pod, workload, workload_type) ('
            'label_replace(label_replace('
            f'label_replace({rs_pods}, "replicaset", "$1", "owner_name", "(.*)")'
            ' * on (namespace, replicaset) group_left (owner_deploy) '
            'max by (namespace, replicaset, owner_deploy) ('
            f'label_replace({rs_deploy}, "owner_deploy", "$1", "owner_name", "(.*)"))'
            ', "workload", "$1", "owner_deploy", "(.*)"), "workload_type", "Deployment", "namespace", ".*")'
            ' or label_replace(label_replace('
            f'{other_pods}, "workload", "$1", "owner_name", "(.*)")'
            ', "workload_type", "$1", "owner_kind", "(.*)"))')


WL = pod_to_wl()
# Every pod that existed at any time in the waste window, so pods replaced by a rollout still count
WL_WIN = pod_to_wl(lambda sel: f'max_over_time({sel}[{WIN}])')


def by_wl(per_pod, agg="sum"):
    """Rolls a per-pod vector (labels: namespace, pod) up to its workload."""
    return (f'{agg} by (namespace, workload, workload_type) ('
            f'({per_pod}) * on (namespace, pod) group_left (workload, workload_type) {WL})')


# ---- per-pod building blocks (result labels: namespace, pod)
CT = f'container!="", container!="POD", {NSF}'
BY_CT = "namespace, pod, container"
BY_DEV = "namespace, pod, container, device"
BY_IF = "namespace, pod, interface"


def one(expr, by=BY_CT):
    """Keeps one series per container (or per `by`) before summing. Some clusters scrape the same
    container metric from two kubelet endpoints (/metrics/cadvisor and /metrics/resource), which
    would otherwise double every sum and break one-to-one divisions."""
    return f'max by ({by}) ({expr})'


def per_pod(expr, by=BY_CT):
    return f'sum by (namespace, pod) ({one(expr, by)})'


def cpu_ct(w):
    """CPU used per container (cores)."""
    return one(f'rate(container_cpu_usage_seconds_total{{{CT}}}[{w}])')


def mem_ct():
    """Memory working set per container (bytes)."""
    return one(f'container_memory_working_set_bytes{{{CT}}}')


def spec(kind, resource):
    """Request or limit per container. Containers without one have no series at all; a value of 0 also
    means "not set" to Kubernetes, and would divide by zero, so it is dropped too."""
    return one(f'kube_pod_container_resource_{kind}{{resource="{resource}", {NSF}}} > 0')


def cpu(w):
    return f'sum by (namespace, pod) ({cpu_ct(w)})'


def mem():
    return f'sum by (namespace, pod) ({mem_ct()})'


def total(kind, resource):
    """Sum of requests or limits per pod (only containers that set one)."""
    return f'sum by (namespace, pod) ({spec(kind, resource)})'


def used_over_limit(used_ct, resource):
    """Usage ÷ limit of the container closest to its limit. Containers without a limit are skipped."""
    return f'max by (namespace, pod) ({used_ct} / on (namespace, pod, container) {spec("limits", resource)})'


def no_limit(used_ct, resource):
    """Running containers per pod that have no limit for this resource."""
    return f'count by (namespace, pod) ({used_ct} unless on (namespace, pod, container) {spec("limits", resource)})'


def unlimited(used_ct, resource):
    """Usage of the containers that have no limit for this resource (per container)."""
    return f'({used_ct} unless on (namespace, pod, container) {spec("limits", resource)})'


def with_request(used_ct, resource):
    """Usage per pod, counting only containers that set a request."""
    return f'sum by (namespace, pod) (({used_ct} and on (namespace, pod, container) {spec("requests", resource)}))'


def over_request(used_ct, resource):
    """Usage ÷ request for the whole workload, counting only containers that set a request, so a
    container without one does not inflate the ratio."""
    return f'{by_wl(with_request(used_ct, resource))} / {by_wl(total("requests", resource))}'


K_CT = "namespace, workload, workload_type, container"


def by_wl_ct(per_ct, agg="sum"):
    """Rolls a per-container vector up to (workload, container): the same container across all pods."""
    return f'{agg} by ({K_CT}) (({per_ct}) * on (namespace, pod) group_left (workload, workload_type) {WL})'


def limit_state(used_ct, resource):
    """Per workload: 0 = every container sets a limit, 1 = some containers do not, 2 = none do.
    Pods of a workload share one template, so containers are counted per pod (max over pods)."""
    miss = by_wl(no_limit(used_ct, resource), "max")
    tot = by_wl(f'count by (namespace, pod) ({used_ct})', "max")
    return f'((({miss}) > bool 0) + (({miss}) == bool ({tot}))) or ({tot} * 0)'


def skew(per_pod_expr, min_total):
    """How unevenly a workload's pods share the load: (busiest pod ÷ all pods, busiest pod ÷ average pod).
    Only for workloads whose total is at least min_total, so near-idle workloads do not look skewed."""
    p = f'({per_pod_expr} * on (namespace, pod) group_left (workload, workload_type) {WL})'
    keys = "namespace, workload, workload_type"
    top, total, n = f'max by ({keys}) ({p})', f'sum by ({keys}) ({p})', f'count by ({keys}) ({p})'
    busy = f'and on ({keys}) ({total} >= {min_total})'
    return f'({top} / {total} {busy})', f'({top} / ({total} / {n}) {busy})'


def throttle(w):
    throttled = per_pod(f'rate(container_cpu_cfs_throttled_periods_total{{{CT}}}[{w}])')
    periods = per_pod(f'rate(container_cpu_cfs_periods_total{{{CT}}}[{w}])')
    return f'({throttled} / {periods})'


def mem_limit():
    return used_over_limit(mem_ct(), "memory")


def cpu_limit(w):
    return used_over_limit(cpu_ct(w), "cpu")


def restarts():
    return f'sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total{{{NSF}}}[1h]))'


def net(w):
    tx = per_pod(f'rate(container_network_transmit_bytes_total{{{NSF}}}[{w}])', BY_IF)
    rx = per_pod(f'rate(container_network_receive_bytes_total{{{NSF}}}[{w}])', BY_IF)
    return f'({tx} + {rx})'


def drops(w):
    tx = per_pod(f'rate(container_network_transmit_packets_dropped_total{{{NSF}}}[{w}])', BY_IF)
    rx = per_pod(f'rate(container_network_receive_packets_dropped_total{{{NSF}}}[{w}])', BY_IF)
    return f'({tx} + {rx})'


def disk_write(w):
    """Same fallback order as the detail dashboard: container fs stats, pod fs stats, pod blkio."""
    ctr = per_pod(f'rate(container_fs_writes_bytes_total{{{CT}}}[{w}])', BY_DEV)
    pod = per_pod(f'rate(container_fs_writes_bytes_total{{{NSF}, container="", pod!=""}}[{w}])', BY_DEV)
    blkio = per_pod(f'rate(container_blkio_device_usage_total{{{NSF}, operation="Write", container="", pod!=""}}[{w}])',
                    "namespace, pod, device, operation")
    return f'({ctr} or {pod} or {blkio})'


def fs_usage():
    return per_pod(f'container_fs_usage_bytes{{{CT}}}', BY_DEV)


# ---- per-node building blocks (result label: instance = node name)
def n_cpu(w):
    return f'1 - avg by (instance) (rate(node_cpu_seconds_total{{mode="idle"}}[{w}]))'


def n_iowait(w):
    return f'avg by (instance) (rate(node_cpu_seconds_total{{mode="iowait"}}[{w}]))'


def n_softirq(w):
    return f'avg by (instance) (rate(node_cpu_seconds_total{{mode="softirq"}}[{w}]))'


def n_mem():
    return 'max by (instance) (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'


N_CORES = 'count by (instance) (node_cpu_seconds_total{mode="idle"})'
N_MEM_TOTAL = 'max by (instance) (node_memory_MemTotal_bytes)'
# Pod -> 所在 Node (instance = node 名稱,跟 node-exporter 一致)
POD_NODE = (f'label_replace(max by (namespace, pod, node) (kube_pod_info{{node!="", {NSF}}}),'
            ' "instance", "$1", "node", "(.*)")')


def node_share(per_pod, node_total):
    """Share of a node's total taken by the workload: its pods on the same node are added up, then
    divided by that node's total; the node where it takes the most is shown."""
    on_node = (f'sum by (namespace, workload, workload_type, instance) (({per_pod})'
               f' * on (namespace, pod) group_left (workload, workload_type) {WL}'
               f' * on (namespace, pod) group_left (instance) {POD_NODE})')
    return f'max by (namespace, workload, workload_type) ({on_node} / on (instance) group_left () ({node_total}))'


def n_conntrack():
    return 'max by (instance) (node_nf_conntrack_entries / node_nf_conntrack_entries_limit)'


def n_disk_latency(w):
    return (f'max by (instance) ((rate(node_disk_read_time_seconds_total{{{DEV}}}[{w}]) + rate(node_disk_write_time_seconds_total{{{DEV}}}[{w}]))'
            f' / (rate(node_disk_reads_completed_total{{{DEV}}}[{w}]) + rate(node_disk_writes_completed_total{{{DEV}}}[{w}])))')


def n_fs():
    return f'max by (instance) (1 - node_filesystem_avail_bytes{{{FS}}} / node_filesystem_size_bytes{{{FS}}})'


def n_drops(w):
    return (f'sum by (instance) (rate(node_network_receive_drop_total{{{NIC}}}[{w}])'
            f' + rate(node_network_transmit_drop_total{{{NIC}}}[{w}]))')


def n_pods():
    return 'max by (instance) (label_replace(count by (node) (kube_pod_info{node!=""}), "instance", "$1", "node", "(.*)"))'


def n_pressure():
    return ('max by (instance) (label_replace(kube_node_status_condition{condition=~"DiskPressure|MemoryPressure|PIDPressure", status="true"}'
            ', "instance", "$1", "node", "(.*)"))')


# ---------------------------------------------------------------- layout helpers
panels = []
_id = [0]
_y = [0]


def nid():
    _id[0] += 1
    return _id[0]


def row(title):
    panels.append({"id": nid(), "type": "row", "title": title, "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": _y[0]}, "panels": []})
    _y[0] += 1


class Line:
    """Places panels left to right, wrapping at 24 columns."""

    def __init__(self):
        self.x = 0
        self.h = 0

    def add(self, panel, w, h):
        if self.x + w > 24:
            self.newline()
        panel["gridPos"] = {"h": h, "w": w, "x": self.x, "y": _y[0]}
        panels.append(panel)
        self.x += w
        self.h = max(self.h, h)

    def newline(self):
        _y[0] += self.h
        self.x = 0
        self.h = 0


def desc(view, what, how):
    return f"**視角**：{view}\n\n**這是什麼**：{what}\n\n**怎麼看**：{how}"


def steps(*pairs, base="green"):
    out = [{"color": base, "value": None}]
    for color, v in pairs:
        out.append({"color": color, "value": v})
    return out


G, Y, RD = "green", "yellow", "red"


def text(line, content, w=24, h=8):
    line.add({"id": nid(), "type": "text", "title": "",
              "options": {"mode": "markdown", "content": content}}, w, h)


def ts(line, title, expr, legend, unit, description, w=12, h=8):
    """expr / legend can also be lists, one line per query."""
    exprs, legends = (expr, legend) if isinstance(expr, list) else ([expr], [legend])
    line.add({
        "id": nid(), "type": "timeseries", "title": title, "description": description,
        "datasource": DS,
        "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": lg,
                     "range": True, "instant": False} for i, (e, lg) in enumerate(zip(exprs, legends))],
        "fieldConfig": {"defaults": {"unit": unit, "min": 0,
                                     "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 10,
                                                "showPoints": "never", "spanNulls": True}},
                        "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["max"],
                               "sortBy": "Max", "sortDesc": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }, w, h)


def stat(line, title, expr, unit, thr, description, w=4, h=4, legend=None, decimals=None, no_value=None):
    """expr can also be a list (one value per query); unit / decimals then can be lists too."""
    exprs = expr if isinstance(expr, list) else [expr]
    units = unit if isinstance(unit, list) else [unit] * len(exprs)
    decs = decimals if isinstance(decimals, list) else [decimals] * len(exprs)
    overrides = []
    for i, (u, d) in enumerate(zip(units, decs)):
        props = [{"id": "unit", "value": u}] + ([{"id": "decimals", "value": d}] if d is not None else [])
        overrides.append({"matcher": {"id": "byFrameRefID", "options": chr(65 + i)}, "properties": props})
    defaults = {"color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": thr or steps(base="text")}}
    if no_value:
        defaults["noValue"] = no_value
    line.add({
        "id": nid(), "type": "stat", "title": title, "description": description, "datasource": DS,
        "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": lg,
                     "range": False, "instant": True}
                    for i, (e, lg) in enumerate(zip(exprs, legend or [""] * len(exprs)))],
        "fieldConfig": {"defaults": defaults, "overrides": overrides},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background" if thr else "none", "graphMode": "none",
                    "textMode": "value_and_name" if len(exprs) > 1 else "value",
                    "justifyMode": "center", "orientation": "horizontal"},
    }, w, h)


KEY_WIDTH = {"namespace": 120, "workload": 150, "workload_type": 100, "instance": 140, "container": 130, "pod": 220}


def table(line, title, key_cols, columns, description, sort_by, w=24, h=12, key_rename=None, key_links=None, base=None,
          hide=()):
    """columns: list of (name, expr, unit, thresholds or None, decimals, value mappings or None[, text when empty]).

    base: a query that returns exactly the rows the table should show. Each column is limited to those
    rows, and falls back to -1 for rows it has no value for, shown as the given text (default "—").
    Without it Grafana drops a whole column when its query returns nothing. -1 rather than NaN so that
    sorting a column from high to low puts these rows last (Grafana sorts NaN first); every column here
    is a count, usage or ratio, never negative, so -1 cannot be a real value."""
    targets, rename, index, overrides = [], {}, {}, []
    for i, k in enumerate(key_cols):
        index[k] = i
    for i, col in enumerate(columns):
        name, expr, unit, thr, decimals, mappings = col[:6]
        na = col[6] if len(col) > 6 else "—"
        if base:
            keys = ", ".join(key_cols)
            expr = f"(({expr}) and on ({keys}) ({base})) or ({base} - 1)"
            mappings = (mappings or []) + [{"type": "value", "options": {
                "-1": {"text": na, "color": "#8e8e8e", "index": 99}}}]
        ref = chr(65 + i)
        targets.append({"refId": ref, "datasource": DS, "expr": expr, "legendFormat": "",
                        "range": False, "instant": True, "format": "table"})
        rename[f"Value #{ref}"] = name
        index[f"Value #{ref}"] = len(key_cols) + i
        props = [{"id": "unit", "value": unit}]
        if decimals is not None:
            props.append({"id": "decimals", "value": decimals})
        if thr:
            props += [{"id": "thresholds", "value": {"mode": "absolute", "steps": thr}},
                      {"id": "color", "value": {"mode": "thresholds"}},
                      {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
                      {"id": "custom.displayMode", "value": "color-background"}]
        if mappings:
            props.append({"id": "mappings", "value": mappings})
        overrides.append({"matcher": {"id": "byName", "options": name}, "properties": props})
    for k, new in (key_rename or {}).items():
        rename[k] = new
    for field, links in (key_links or {}).items():
        overrides.append({"matcher": {"id": "byName", "options": field},
                          "properties": [{"id": "links", "value": links}]})
    line.add({
        "id": nid(), "type": "table", "title": title, "description": description,
        "datasource": DS, "targets": targets,
        "transformations": [
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {"excludeByName": {"Time": True, **{k: True for k in hide}},
                                           "renameByName": rename, "indexByName": index}},
        ],
        # 欄位多時讓欄寬可以縮、欄名換行,不要整欄被擠出畫面或欄名被截斷
        "fieldConfig": {"defaults": {"custom": {"filterable": False, "align": "auto", "minWidth": 80,
                                                "wrapHeaderText": True}},
                        # 名稱欄固定寬度、可以篩選,剩下的寬度留給數字欄
                        "overrides": overrides + [{"matcher": {"id": "byName", "options": rename.get(k, k)},
                                                   "properties": [{"id": "custom.filterable", "value": True},
                                                                  {"id": "custom.width", "value": KEY_WIDTH.get(k, 120)}]}
                                                  for k in key_cols]},
        "options": {"showHeader": True, "sortBy": [{"displayName": sort_by, "desc": True}],
                    "footer": {"show": False}},
    }, w, h)


V_CLUSTER_NODE = "叢集內每一台 Node（該 Node 上所有 Pod 加總）"
V_CLUSTER_WL = "叢集內每一個 Workload（該 Workload 所有 Pod 合計）"

# ---------------------------------------------------------------- guide
L = Line()
text(L, """**叢集風險總覽**：先找出嫌疑的 Node 與 Workload，再點 workload 名稱進細節 Dashboard 查證。上方切換資料來源 = 切換叢集。

1. **哪台 Node 吃緊** → 第 1 區
2. **哪個 Workload 用量大、快碰到 Limit、Pod 之間不平均** → 第 2 區（點表頭排序、名稱欄的漏斗篩選，紅 / 黃格要注意）
3. **是什麼時候飆起來的** → 第 3、4 區 Top 10 趨勢
4. **Request 是不是真的快滿、有沒有 Pod 排不進來、該加 Node 還是該砍 Request** → 第 5 區（上方「Node」選一台看每個 Pod 的明細）
5. **誰長期用很少、Request 卻設很高（浪費）** → 第 6 區

上方「長期分析區間」選幾天（預設 7 天），第 5 區的平均 / 尖峰和第 6 區都用它。表格是時間範圍**結束前 5 分鐘**的值（另有寫「過去 N 天」的除外）；百分比都是 **用量 ÷ 欄名寫的分母**；預設排除 `openshift*`、`kube-*` namespace（第 5 區的 Node 層級不排除）。""", h=8)
L.newline()

# ---------------------------------------------------------------- 1 nodes
row("1. 【Node】叢集各 Node 狀態：哪台機器正在變吃緊")
L = Line()
table(L, "【Node】各 Node 狀態（時間範圍結束前 5 分鐘）", ["instance"], [
    ("Pod 數", n_pods(), "none", None, 0, None),
    ("CPU 使用率", n_cpu(W5), "percentunit", steps((Y, 0.8), (RD, 0.9)), 1, None),
    ("記憶體使用率", n_mem(), "percentunit", steps((Y, 0.8), (RD, 0.9)), 1, None),
    ("conntrack", n_conntrack(), "percentunit", steps((Y, 0.5), (RD, 0.7)), 1, None),
    ("磁碟延遲", n_disk_latency(W5), "s", steps((Y, 0.01), (RD, 0.02)), None, None),
    ("磁碟空間", n_fs(), "percentunit", steps((Y, 0.8), (RD, 0.85)), 1, None),
    ("網路丟包", n_drops(W5), "pps", steps((Y, 1), (RD, 50)), 1, None),
    ("iowait", n_iowait(W5), "percentunit", steps((Y, 0.05), (RD, 0.1)), 1, None),
    ("封包處理 CPU", n_softirq(W5), "percentunit", steps((Y, 0.05), (RD, 0.1)), 1, None),
    ("壓力狀態", n_pressure(), "none", steps((RD, 1)), 0,
     [{"type": "value", "options": {"0": {"text": "正常"}, "1": {"text": "發生中"}}}]),
], desc(V_CLUSTER_NODE,
        "每台 Node 共用資源的目前狀態。conntrack、磁碟、網卡都是每台 Node 一份，所有 Pod 共用。"
        "**百分比都是用量 ÷ 容量**：CPU ÷ 總核數、記憶體 ÷ 總記憶體、conntrack ÷ 表格上限、磁碟空間 ÷ 磁碟容量（最滿的那個）；"
        "磁碟延遲取最慢的磁碟；iowait、封包處理 CPU 是佔 CPU 時間的比例。"
        "「網路封包處理 CPU」是核心處理網路封包（softirq）的 CPU，不算在 Pod 的 CPU limit 裡。",
        "紅色、黃色就是正在吃緊的 Node。記下 Node 名稱，再到第 2 區找跑在上面的大戶，或進細節 Dashboard 看該 Node 的 Top 10 鄰居。"),
    sort_by="conntrack", h=10, key_rename={"instance": "Node"},
    base="max by (instance) (node_memory_MemTotal_bytes) * 0")
L.newline()
L = Line()
NODE_HOW = "看哪台 Node 在什麼時間往上飆，再對照第 4 區 Workload 的 Top 10 趨勢，找同一時間上升的是誰。"
ts(L, "【Node】conntrack（÷ 表格上限）Top 10", f'topk(10, {n_conntrack()})', "{{instance}}", "percentunit",
   desc(V_CLUSTER_NODE, "連線追蹤表用了幾成。滿了之後該 Node 所有 Pod 的新連線都會被丟掉。", NODE_HOW), w=8)
ts(L, "【Node】磁碟延遲 Top 10", f'topk(10, {n_disk_latency(WR)})', "{{instance}}", "s",
   desc(V_CLUSTER_NODE, "Node 本機磁碟每次讀寫平均要等多久，所有 Pod 共用。", NODE_HOW), w=8)
ts(L, "【Node】網路丟包 Top 10", f'topk(10, {n_drops(WR)})', "{{instance}}", "pps",
   desc(V_CLUSTER_NODE, "實體網卡每秒丟掉的封包數，正常應接近 0。", NODE_HOW), w=8)
ts(L, "【Node】iowait（÷ CPU 時間）Top 10", f'topk(10, {n_iowait(WR)})', "{{instance}}", "percentunit",
   desc(V_CLUSTER_NODE, "CPU 閒著在等磁碟的時間比例，往上飆代表磁碟變慢。", NODE_HOW), w=8)
ts(L, "【Node】磁碟空間（÷ 容量）Top 10", f'topk(10, {n_fs()})', "{{instance}}", "percentunit",
   desc(V_CLUSTER_NODE, "Node 本機磁碟空間用了幾成，太高會觸發 DiskPressure 驅逐 Pod。", NODE_HOW), w=8)
ts(L, "【Node】CPU 使用率（÷ 總核數）Top 10", f'topk(10, {n_cpu(WR)})', "{{instance}}", "percentunit",
   desc(V_CLUSTER_NODE, "整台 Node 的 CPU 有多少時間在工作。", NODE_HOW), w=8)
L.newline()

# ---------------------------------------------------------------- 2 workloads
row("2. 【Workload】風險排行：點 workload 名稱進細節 Dashboard")
DETAIL_LINK = [{"title": "開啟此 Workload 的細節 Dashboard",
                "url": "/d/" + DETAIL_UID + "/?var-datasource=${datasource}"
                       "&var-namespace=${__data.fields.namespace}"
                       "&var-workload=${__data.fields.workload}&${__url_time_range}"}]
WL_KEYS = ["namespace", "workload", "workload_type"]
WL_RENAME = {"workload_type": "類型"}
WL_ROWS = f'count by (namespace, workload, workload_type) ({WL}) * 0'
NOT_SET = "沒設"
POD_COUNT = ("Pod 數", f'count by (namespace, workload, workload_type) ({WL})', "none", None, 0, None)
OVER_REQ = steps((Y, 1.0), (RD, 2.0))
LIMIT_STATE = [{"type": "value", "options": {
    "0": {"text": "都有設", "color": "green", "index": 0},
    "1": {"text": "部分容器沒設", "color": "orange", "index": 1},
    "2": {"text": "全部沒設", "color": "orange", "index": 2}}}]
REQ_LIMIT_HOW = ("用量 ÷ Request 超過 100% → 正在用 Node 上別人沒用到的資源，Node 忙起來會互搶；"
                 "用量 ÷ Limit 接近 100% → {limit_hit}。「沒設」= 沒有容器設（設 0 也算沒設）。")
L = Line()
table(L, "【Workload】CPU：用量 → Request → Limit（時間範圍結束前 5 分鐘）", WL_KEYS, [
    POD_COUNT,
    ("用量（核）", by_wl(cpu(W5)), "none", None, 2, None),
    ("佔 Node", node_share(cpu(W5), N_CORES), "percentunit", steps((Y, 0.3), (RD, 0.5)), 0, None),
    ("Request（核）", by_wl(total("requests", "cpu")), "none", None, 2, None, NOT_SET),
    ("用量 ÷ Request", over_request(cpu_ct(W5), "cpu"), "percentunit", OVER_REQ, 0, None, NOT_SET),
    ("Limit（核）", by_wl(total("limits", "cpu")), "none", None, 2, None, NOT_SET),
    ("Limit 設定", limit_state(cpu_ct(W5), "cpu"), "none", None, 0, LIMIT_STATE),
    ("用量 ÷ Limit", by_wl(cpu_limit(W5), "max"), "percentunit", steps((Y, 0.8), (RD, 0.95)), 0, None, NOT_SET),
    ("throttling", by_wl(throttle(W5), "max"), "percentunit", steps((Y, 0.25), (RD, 0.5)), 0, None),
], desc(V_CLUSTER_WL,
        "每個 Deployment / StatefulSet / DaemonSet 的 CPU。用量、Request、Limit 是所有 Pod 加總；"
        "**佔 Node** = 他在某台 Node 上的用量（同一台的 Pod 加總）÷ 那台 Node 的總核數，取佔最多的那台，"
        "用來判斷他是不是那台 Node 的大戶，那台 Node 還剩多少看第 1 區；"
        "**用量 ÷ Request** = 有設 Request 的容器的用量 ÷ 它們的 Request（全部 Pod 加總）；"
        "**用量 ÷ Limit** = 最接近 Limit 的那個容器（Limit 是每個容器各自的上限，加總會把快爆的平均掉）；"
        "**Limit 設定**：都有設 / 部分容器沒設（Limit 欄只加總有設的容器）/ 全部沒設，沒設的是哪個容器看下方「沒設 Limit 的容器」；"
        "**throttling** = 被節流最嚴重的 Pod。",
        REQ_LIMIT_HOW.format(limit_hit="會被 throttling 變慢")),
    sort_by="用量（核）", h=10, key_links={"workload": DETAIL_LINK}, key_rename=WL_RENAME, base=WL_ROWS)
L.newline()
L = Line()
table(L, "【Workload】記憶體：用量 → Request → Limit（時間範圍結束前 5 分鐘）", WL_KEYS, [
    POD_COUNT,
    ("用量", by_wl(mem()), "bytes", None, 1, None),
    ("佔 Node", node_share(mem(), N_MEM_TOTAL), "percentunit", steps((Y, 0.3), (RD, 0.5)), 0, None),
    ("Request", by_wl(total("requests", "memory")), "bytes", None, 1, None, NOT_SET),
    ("用量 ÷ Request", over_request(mem_ct(), "memory"), "percentunit", OVER_REQ, 0, None, NOT_SET),
    ("Limit", by_wl(total("limits", "memory")), "bytes", None, 1, None, NOT_SET),
    ("Limit 設定", limit_state(mem_ct(), "memory"), "none", None, 0, LIMIT_STATE),
    ("用量 ÷ Limit", by_wl(mem_limit(), "max"), "percentunit", steps((Y, 0.8), (RD, 0.9)), 0, None, NOT_SET),
], desc(V_CLUSTER_WL,
        "每個 Workload 的記憶體（working set）。欄位算法同上方 CPU 表：用量、Request、Limit 是所有 Pod 加總；"
        "**佔 Node** = 他在某台 Node 上的用量 ÷ 那台 Node 的總記憶體，取佔最多的那台；"
        "**用量 ÷ Limit** = 最接近 Limit 的那個容器。",
        REQ_LIMIT_HOW.format(limit_hit="會被 OOMKilled")),
    sort_by="用量", h=10, key_links={"workload": DETAIL_LINK}, key_rename=WL_RENAME, base=WL_ROWS)
L.newline()

L = Line()
# 每列一個容器 (同一個 Workload 的 Pod 共用一份設定,所以容器只列一次,用量是所有 Pod 加總)
CT_ALL = f'max by ({K_CT}) ({cpu_ct(W5)} * on (namespace, pod) group_left (workload, workload_type) {WL}) * 0'
PODS_CT = f'count by ({K_CT}) ({cpu_ct(W5)} * on (namespace, pod) group_left (workload, workload_type) {WL})'
UNL_CPU, UNL_MEM = unlimited(cpu_ct(W5), "cpu"), unlimited(mem_ct(), "memory")
MISS = (f'(({by_wl_ct(f"({UNL_CPU}) * 0 + 1", "max")}) or {CT_ALL})'
        f' + 2 * (({by_wl_ct(f"({UNL_MEM}) * 0 + 1", "max")}) or {CT_ALL})')
def ct_over_request(used_ct, resource):
    used = f'({used_ct} and on (namespace, pod, container) {spec("requests", resource)})'
    return f'{by_wl_ct(used)} / {by_wl_ct(spec("requests", resource))}'
table(L, "【Workload】沒設 Limit 的容器：用量沒有上限，會吃掉 Node 的資源（時間範圍結束前 5 分鐘）",
      ["namespace", "workload", "workload_type", "container"], [
    ("沒設的 Limit", MISS, "none", None, 0, [{"type": "value", "options": {
        "1": {"text": "CPU", "color": "orange", "index": 0},
        "2": {"text": "記憶體", "color": "orange", "index": 1},
        "3": {"text": "CPU、記憶體", "color": "red", "index": 2}}}]),
    ("Pod 數", PODS_CT, "none", None, 0, None),
    ("CPU 用量（核）", by_wl_ct(cpu_ct(W5)), "none", None, 2, None),
    ("CPU Request（核）", by_wl_ct(spec("requests", "cpu")), "none", None, 2, None, NOT_SET),
    ("CPU ÷ Request", ct_over_request(cpu_ct(W5), "cpu"), "percentunit", OVER_REQ, 0, None, NOT_SET),
    ("記憶體用量", by_wl_ct(mem_ct()), "bytes", None, 1, None),
    ("記憶體 Request", by_wl_ct(spec("requests", "memory")), "bytes", None, 1, None, NOT_SET),
    ("記憶體 ÷ Request", ct_over_request(mem_ct(), "memory"), "percentunit", OVER_REQ, 0, None, NOT_SET),
], desc(V_CLUSTER_WL,
        "沒設 Limit（或設 0）的容器，每列一個容器。同一個 Workload 的 Pod 共用一份設定，所以容器只列一次；"
        "用量、Request 是這個容器在所有 Pod 加總，Pod 數是有幾個 Pod 跑這個容器。"
        "沒設 Limit 的資源可以一直用到 Node 沒有為止，所以只能跟 Request（排程時保證的量）比。",
        "**沒設的 Limit** 寫的那一項沒有上限。÷ Request 遠超過 100% → 建議補上 Limit，或把 Request 調到接近實際用量；"
        "「沒設」= 連 Request 也沒設，排程時完全沒有幫它保留資源。"),
    sort_by="CPU 用量（核）", h=8, key_links={"workload": DETAIL_LINK},
    key_rename={**WL_RENAME, "container": "容器"}, base=f'(({MISS}) > 0) * 0', hide=("workload_type",))
L.newline()

L = Line()
SKEW_ROWS = f'(count by (namespace, workload, workload_type) ({WL}) >= 2) * 0'
SKEW_TIMES = steps((Y, 1.5), (RD, 2.5))
CPU_SHARE, CPU_TIMES = skew(cpu(W5), 0.05)
MEM_SHARE, MEM_TIMES = skew(mem(), 0)
NET_SHARE, NET_TIMES = skew(net(W5), 10240)
table(L, "【Workload】Pod 之間分配不均：可能是 LB、key 分配或架構問題（時間範圍結束前 5 分鐘）", WL_KEYS, [
    POD_COUNT,
    ("CPU 最忙佔比", CPU_SHARE, "percentunit", None, 0, None, "用量太少"),
    ("CPU 最忙 ÷ 平均", CPU_TIMES, "none", SKEW_TIMES, 1, None, "用量太少"),
    ("網路最忙佔比", NET_SHARE, "percentunit", None, 0, None, "流量太少"),
    ("網路最忙 ÷ 平均", NET_TIMES, "none", SKEW_TIMES, 1, None, "流量太少"),
    ("記憶體最多 ÷ 平均", MEM_TIMES, "none", SKEW_TIMES, 1, None),
], desc(V_CLUSTER_WL,
        "同一個 Workload 的 Pod 理論上應該分到差不多的量（N 個 Pod 各約 1/N）。只列 2 個 Pod 以上的 Workload。"
        "**最忙佔比** = 最忙的 Pod 佔全部幾 %；**最忙 ÷ 平均** = 最忙的 Pod 是平均的幾倍，1 = 很平均。"
        "例：3 個 Pod，一個佔 90% → 佔全部 90%、是平均的 2.7 倍。用量太少（CPU < 0.05 核、網路 < 10 KB/s）不算。",
        "CPU、網路 ÷ 平均 > 2.5（紅）→ 流量集中在少數 Pod：檢查 Service / LB 設定、長連線（gRPC、HTTP keep-alive）"
        "是否都黏在同一個 Pod、message key 或分片是否集中；記憶體只有一個 Pod 特別高 → 那個 Pod 可能有快取或記憶體洩漏。"
        "點 workload 名稱進細節 Dashboard 看是哪個 Pod。"),
    sort_by="CPU 最忙 ÷ 平均", h=8, key_links={"workload": DETAIL_LINK}, key_rename=WL_RENAME, base=SKEW_ROWS,
    hide=("workload_type",))
L.newline()

L = Line()
table(L, "【Workload】共用資源與重啟：limit 管不到，會影響同 Node 鄰居（時間範圍結束前 5 分鐘）", WL_KEYS, [
    POD_COUNT,
    ("重啟（1 小時）", by_wl(restarts()), "none", steps((Y, 1), (RD, 5)), 0, None, "0"),
    ("網路流量", by_wl(net(W5)), "Bps", None, 1, None),
    ("網路丟包", by_wl(drops(W5)), "pps", steps((Y, 1), (RD, 50)), 1, None),
    ("磁碟寫入", by_wl(disk_write(W5)), "Bps", None, 1, None, "沒收集"),
    ("可寫層用量", by_wl(fs_usage()), "bytes", None, 1, None, "沒收集"),
], desc(V_CLUSTER_WL,
        "網路、本機磁碟這些整台 Node 共用、limit 管不到的資源，以及重啟次數。都是所有 Pod 加總。",
        "點表頭排序找大戶；網路丟包、重啟是紅 / 黃格要注意。磁碟寫入、可寫層「沒收集」= 叢集沒有收集 Pod 層級的磁碟指標。"),
    sort_by="網路流量", h=10, key_links={"workload": DETAIL_LINK}, key_rename=WL_RENAME, base=WL_ROWS,
    hide=("workload_type",))
L.newline()

# ---------------------------------------------------------------- 3/4 workload trends
WL_HOW = "看誰在什麼時間往上飆，對照第 1 區 Node 變吃緊的時間點；右側圖例依最大值排序。"
row("3. 【Workload】Limit 相關 Top 10 趨勢（主要影響他自己）")
L = Line()
ts(L, "【Workload】CPU 用量 ÷ Limit（最高的容器）Top 10",
   f'topk(10, {by_wl(cpu_limit(WR), "max")})', "{{namespace}}/{{workload}}", "percentunit",
   desc(V_CLUSTER_WL, "最接近 CPU Limit 的容器用了 Limit 的幾 %，接近 100% 會被 throttling。沒設 Limit 的不在這裡。", WL_HOW))
ts(L, "【Workload】CPU throttling（最高的 Pod）Top 10",
   f'topk(10, {by_wl(throttle(WR), "max")})', "{{namespace}}/{{workload}}", "percentunit",
   desc(V_CLUSTER_WL, "該 Workload 中被 CPU Limit 擋最多的 Pod 的節流比例。只影響他自己。", WL_HOW))
ts(L, "【Workload】記憶體用量 ÷ Limit（最高的容器）Top 10",
   f'topk(10, {by_wl(mem_limit(), "max")})', "{{namespace}}/{{workload}}", "percentunit",
   desc(V_CLUSTER_WL, "最接近記憶體 Limit 的容器用了 Limit 的幾 %，到 100% 會被 OOMKilled。沒設 Limit 的不在這裡。", WL_HOW))
ts(L, "【Workload】重啟次數 Top 10（每 1 小時）",
   f'topk(10, {by_wl(restarts())})', "{{namespace}}/{{workload}}", "none",
   desc(V_CLUSTER_WL, "過去 1 小時的重啟次數。", WL_HOW))
L.newline()
row("4. 【Workload】共用資源 Top 10 趨勢（limit 管不到，會影響同 Node 鄰居）")
L = Line()
# 只算沒設 Limit 的容器:這些容器的用量沒有上限 (有設 Limit 的被自己的 Limit 管住,在第 3 區)
NOLIM_CPU = f'sum by (namespace, pod) ({unlimited(cpu_ct(WR), "cpu")})'
NOLIM_MEM = f'sum by (namespace, pod) ({unlimited(mem_ct(), "memory")})'
ts(L, "【Workload】沒設 CPU Limit 的容器 CPU 用量（核）Top 10",
   f'topk(10, {by_wl(NOLIM_CPU)})', "{{namespace}}/{{workload}}", "none",
   desc(V_CLUSTER_WL, "沒設 CPU Limit 的容器用了幾核。這些容器沒有上限，Node 忙的時候會跟鄰居搶 CPU。", WL_HOW))
ts(L, "【Workload】沒設記憶體 Limit 的容器記憶體用量 Top 10",
   f'topk(10, {by_wl(NOLIM_MEM)})', "{{namespace}}/{{workload}}", "bytes",
   desc(V_CLUSTER_WL, "沒設記憶體 Limit 的容器用了多少記憶體。這些容器沒有上限，一直長會讓 Node 記憶體不足，"
        "觸發 MemoryPressure 驅逐同 Node 的 Pod，或讓鄰居被 OOMKilled。", WL_HOW))
ts(L, "【Workload】網路流量 Top 10",
   f'topk(10, {by_wl(net(WR))})', "{{namespace}}/{{workload}}", "Bps",
   desc(V_CLUSTER_WL, "送出加接收的網路流量，所在 Node 的網卡由所有 Pod 共用。", WL_HOW))
ts(L, "【Workload】網路丟包 Top 10",
   f'topk(10, {by_wl(drops(WR))})', "{{namespace}}/{{workload}}", "pps",
   desc(V_CLUSTER_WL, "Pod 網路介面上每秒丟掉的封包數，正常應接近 0。", WL_HOW))
ts(L, "【Workload】磁碟寫入 Top 10",
   f'topk(10, {by_wl(disk_write(WR))})', "{{namespace}}/{{workload}}", "Bps",
   desc(V_CLUSTER_WL, "寫入 Node 本機磁碟的量（log、暫存檔、容器內寫檔）。沒資料代表叢集沒有收集 Pod 層級磁碟 IO。", WL_HOW))
ts(L, "【Workload】可寫層用量 Top 10",
   f'topk(10, {by_wl(fs_usage())})', "{{namespace}}/{{workload}}", "bytes",
   desc(V_CLUSTER_WL, "容器在 Node 本機磁碟上佔用的空間，持續成長可能把 Node 磁碟寫滿。", WL_HOW))
L.newline()

# ---------------------------------------------------------------- 5 capacity
row("5. 【容量】Request 是不是真的快滿：有沒有 Pod 排不進來、該加 Node 還是該砍 Request")


# Node 層級不套 namespace 篩選:不管誰的 Request,都會佔住 Node 的可分配量
def on_node(metric):
    """kube-state-metrics node series, relabelled to instance = node name (same as node-exporter)."""
    return f'max by (instance) (label_replace({metric}, "instance", "$1", "node", "(.*)"))'


def n_alloc(resource):
    return on_node(f'kube_node_status_allocatable{{resource="{resource}"}}')


# 還沒結束的 Pod (Pending / Running) 才會佔住 Request
LIVE = 'max by (namespace, pod) (kube_pod_status_phase{phase=~"Pending|Running"}) == 1'


def pods_on(node):
    """Live pods on the nodes matching `node` (labels: namespace, pod, instance; value 1)."""
    return (f'(max by (namespace, pod, instance) (label_replace(kube_pod_info{{node=~"{node}"}},'
            f' "instance", "$1", "node", "(.*)")) and on (namespace, pod) ({LIVE}))')


PODS_ON_ANY = pods_on(".+")


def n_requested(resource, kind="requests"):
    """Requests (or limits) of the live pods on each node; 0 on a node without any."""
    spec_ct = f'max by (namespace, pod, container) (kube_pod_container_resource_{kind}{{resource="{resource}"}})'
    return (f'(sum by (instance) ({spec_ct} * on (namespace, pod) group_left (instance) {PODS_ON_ANY})'
            f' or {n_alloc(resource)} * 0)')


N_LIVE_PODS = f'(count by (instance) ({PODS_ON_ANY}) or {n_alloc("pods")} * 0)'
CORDON = on_node('kube_node_spec_unschedulable')
TAINT = on_node('kube_node_spec_taint{effect=~"NoSchedule|NoExecute"}')
# 0 可排程、1 有 NoSchedule / NoExecute taint (一般 Pod 排不上)、2 被 cordon
SCHED = f'((({CORDON}) == 1) * 2 or ({TAINT}) * 0 + 1 or ({CORDON}) * 0)'
SCHED_OK = f'({SCHED} == 0)'
SCHED_MAP = [{"type": "value", "options": {"0": {"text": "可排程"}, "1": {"text": "有 taint"},
                                           "2": {"text": "停止排程", "color": "yellow"}}}]

N_CPU_NOW = f'({N_CORES}) * ({n_cpu(W5)})'
N_CPU_AVG = f'({N_CORES}) * ({n_cpu(WIN)})'
N_CPU_PEAK = f'max_over_time((({N_CORES}) * ({n_cpu("5m")}))[{WIN}:5m])'
N_MEM_NOW = 'max by (instance) (node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes)'
N_MEM_AVG = (f'max by (instance) (avg_over_time(node_memory_MemTotal_bytes[{WIN}])'
             f' - avg_over_time(node_memory_MemAvailable_bytes[{WIN}]))')
N_MEM_PEAK = f'max_over_time(({N_MEM_NOW})[{WIN}:5m])'


def n_free(resource):
    """Allocatable not yet taken by requests: what a new pod can still ask for on this node."""
    return f'clamp_min(({n_alloc(resource)}) - {n_requested(resource)}, 0)'


FULL = steps((Y, 0.8), (RD, 0.9))
HELD = steps((Y, 0.3), (RD, 0.5))
LOW_USE = steps((Y, 0.2), (G, 0.5), base=RD)
PEAK_USE = steps((Y, 0.5), (G, 1.0), base=RD)
VERDICT = steps((Y, 1), ("orange", 2), (RD, 3))
VERDICT_MAP = [{"type": "value", "options": {
    "0": {"text": "還有空間"}, "1": {"text": "快滿"},
    "2": {"text": "Request 滿、沒在用 → 砍 Request"}, "3": {"text": "真的滿 → 加 Node"}}}]


def capacity_cols(resource, now, avg, peak, unit, decimals, suffix, limit_thr):
    alloc = n_alloc(resource)

    def r(e):
        return f'({e}) / ({alloc})'

    req, peak_r = r(n_requested(resource)), r(peak)
    # 3 = Request 滿而且尖峰也高 (真的滿)、2 = Request 滿但尖峰不高 (佔住沒用)、1 = Request 快滿 (或沒有用量資料)
    verdict = (f'(({req} >= 0.9) and ({peak_r} >= 0.7)) * 0 + 3'
               f' or (({req} >= 0.9) and ({peak_r} < 0.7)) * 0 + 2'
               f' or ({req} >= 0.8) * 0 + 1 or {req} * 0')
    return [
        ("可排程", SCHED, "none", None, 0, SCHED_MAP),
        ("Pod 數 ÷ 上限", f'({N_LIVE_PODS}) / ({n_alloc("pods")})', "percentunit", FULL, 0, None),
        (f"可分配{suffix}", alloc, unit, None, decimals, None),
        ("Request ÷ 可分配", req, "percentunit", FULL, 0, None),
        ("目前用量 ÷ 可分配", r(now), "percentunit", FULL, 0, None),
        ("平均用量 ÷ 可分配", r(avg), "percentunit", None, 0, None),
        ("尖峰 ÷ 可分配", peak_r, "percentunit", FULL, 0, None),
        ("Limit ÷ 可分配", r(n_requested(resource, "limits")), "percentunit", limit_thr, 0, None),
        ("佔住沒用", f'clamp_min({req} - {r(avg)}, 0)', "percentunit", HELD, 0, None),
        (f"剩餘可排{suffix}", n_free(resource), unit, None, decimals, None),
        ("判定", verdict, "none", VERDICT, 0, VERDICT_MAP),
    ]


V_NODE_ALL = "叢集內每一台 Node（所有 namespace 的 Pod 都算，不受上方 namespace 篩選影響）"
CAP_WHAT = ("排程器只看 Request：Request ÷ 可分配滿了，新的 Pod 就排不進這台 Node，就算機器其實很閒。"
            "**可分配** = Node 扣掉系統保留後給 Pod 用的量（排程的真實上限）；"
            "**Request ÷ 可分配** = 這台 Node 上還沒結束（Pending / Running）的 Pod 的 Request 加總 ÷ 可分配（目前）；"
            "**目前 / 平均 / 尖峰用量** = 整台 Node 結束前 5 分鐘 / 過去 $waste_window 平均 / 過去 $waste_window 最高{peak}"
            "（含系統程序，所以可能超過 100%）；"
            "**Limit ÷ 可分配** = Limit 加總（只算有設的容器），超過 100% 代表大家同時衝到 Limit 時 Node 會不夠{limit}；"
            "**佔住沒用** = Request ÷ 可分配 − 平均用量 ÷ 可分配；**剩餘可排** = 可分配 − Request，新 Pod 的 Request 要小於它才排得進來。")
CAP_HOW = ("看「判定」：**真的滿 → 加 Node**（Request ≥ 90% 而且尖峰 ≥ 70%）；"
           "**Request 滿、沒在用 → 砍 Request**（Request ≥ 90% 但尖峰 < 70%），到第 6 區找是哪些容器，或在下方選這台 Node 看明細；"
           "**快滿** = Request ≥ 80%。只有一兩台滿、上方「排不進來的 Pod」是 0、還有 Node 排得下最大的 Pod → 排程器會放到別台，不急著砍；"
           "很多台滿而且開始有 Pod 排不進來，才是要動手的時候。")
L = Line()
stat(L, "排不進來的 Pod（目前）", f'count(max by (namespace, pod) (kube_pod_status_unschedulable) == 1) or vector(0)',
     "none", steps((RD, 1)),
     desc("叢集內所有 namespace", "排程器找不到任何一台 Node 放得下的 Pod（kube-state-metrics 的 PodScheduled=False、reason=Unschedulable）。"
          "原因可能是 Request 不夠、taint、affinity、PVC 所在區域。",
          "大於 0 就看下方「排不進來的 Pod」表。原因文字（例如 Insufficient cpu）只在事件裡，不是指標："
          "`oc get events -A --field-selector reason=FailedScheduling`"))
stat(L, "排程失敗次數（時間範圍內）",
     'sum(increase(scheduler_schedule_attempts_total{result="unschedulable"}[$__range]))', "none", steps((Y, 1)),
     desc("叢集的排程器（openshift-kube-scheduler）",
          "排程器嘗試排 Pod 卻找不到 Node 的次數。同一個 Pod 會一直重試，所以次數會比 Pod 數多。",
          "時間範圍拉長（例如 7 天）看最近有沒有發生過，「排不進來的 Pod」只看得到現在。沒資料代表這個資料來源沒有收集排程器指標，改看下方趨勢圖的 kube-state-metrics 那條線。"),
     no_value="沒收集")
MAX_POD = {r: f'max(sum by (namespace, pod) ({spec("requests", r)} and on (namespace, pod) ({LIVE})))'
           for r in ("cpu", "memory")}
FIT = (f'count(({n_free("cpu")} >= scalar({MAX_POD["cpu"]})) and on (instance) ({n_free("memory")} >= scalar({MAX_POD["memory"]}))'
       f' and on (instance) {SCHED_OK}) or vector(0)')
stat(L, "排得下最大 Pod 的 Node 數", FIT, "none", steps((Y, 1), (G, 3), base=RD),
     desc("可排程（沒有 cordon、沒有 NoSchedule taint）的 Node",
          "剩餘可排的 CPU 和記憶體都大於右邊「最大的 Pod」的 Node 有幾台。",
          "0 台 → 目前最大的 Workload 一 rollout 或擴容，新 Pod 就會排不進來；1–2 台 → 快沒位置了。"))
stat(L, "最大 Pod 的 Request", [MAX_POD["cpu"], MAX_POD["memory"]], ["none", "bytes"], None,
     desc("上方 namespace 篩選內，還沒結束的 Pod", "單一 Pod 的 Request（所有容器加總）最大是多少，CPU 和記憶體分開取。",
          "跟下方 Node 表的「剩餘可排」比：剩餘可排比它小的 Node，排不下這種大小的 Pod。"),
     legend=["CPU（核）", "記憶體"], decimals=[2, 1])
CL_REQ = {r: f'sum({n_requested(r)} and on (instance) {SCHED_OK}) / sum({n_alloc(r)} and on (instance) {SCHED_OK})'
          for r in ("cpu", "memory")}
stat(L, "叢集 Request ÷ 可分配", [CL_REQ["cpu"], CL_REQ["memory"]], "percentunit", FULL,
     desc("可排程的 Node 加總", "所有可排程 Node 的 Request 加總 ÷ 可分配加總。", "接近 80% 以上，代表整個叢集都快排不下，不只是單一 Node。"),
     legend=["CPU", "記憶體"], decimals=0, w=8)
L.newline()

L = Line()
table(L, "【Node】CPU：Request vs 用量 vs 可分配（Request、目前是結束前 5 分鐘；平均、尖峰是過去 $waste_window）", ["instance"],
      capacity_cols("cpu", N_CPU_NOW, N_CPU_AVG, N_CPU_PEAK, "none", 1, "（核）", steps((Y, 2), (RD, 3))),
      desc(V_NODE_ALL, CAP_WHAT.format(peak="（每 5 分鐘的平均）", limit="，CPU 只會變慢、被 throttling"), CAP_HOW),
      sort_by="判定", h=9, key_rename={"instance": "Node"}, base=f'({n_alloc("pods")}) * 0')
L.newline()
L = Line()
table(L, "【Node】記憶體：Request vs 用量 vs 可分配（Request、目前是結束前 5 分鐘；平均、尖峰是過去 $waste_window）", ["instance"],
      capacity_cols("memory", N_MEM_NOW, N_MEM_AVG, N_MEM_PEAK, "bytes", 1, "", steps((Y, 1.0), (RD, 1.5))),
      desc(V_NODE_ALL, CAP_WHAT.format(peak="", limit="，記憶體會 OOMKilled 或被驅逐"),
           CAP_HOW + "記憶體不像 CPU 可以借，尖峰 ÷ 可分配很高的 Node 不要再砍 Request 塞更多 Pod。"),
      sort_by="判定", h=9, key_rename={"instance": "Node"}, base=f'({n_alloc("pods")}) * 0')
L.newline()

ALL_CT = 'container!="", container!="POD"'


def pod_spec_all(kind, resource):
    """Request or limit per pod in every namespace (only containers that set one)."""
    return 'sum by (namespace, pod) (%s)' % one(f'kube_pod_container_resource_{kind}{{resource="{resource}"}} > 0')


UNSCHED = 'max by (namespace, pod) (kube_pod_status_unschedulable) == 1'
L = Line()
table(L, "【Pod】排不進來的 Pod（目前）", ["namespace", "pod"], [
    ("已經等了", f'time() - max by (namespace, pod) (kube_pod_created)', "s", None, 0, None),
    ("CPU Request（核）", pod_spec_all("requests", "cpu"), "none", None, 2, None, NOT_SET),
    ("記憶體 Request", pod_spec_all("requests", "memory"), "bytes", None, 1, None, NOT_SET),
], desc("叢集內所有 namespace", "排程器找不到 Node 放得下的 Pod，跟它要的 Request。",
        "跟上方 Node 表的「剩餘可排」比：每台都比它小 → 是 Request 不夠；有 Node 夠大卻還是排不進來 → 是 taint、affinity、PVC 區域這類限制，"
        "用 `oc describe pod` 看 Events。沒有資料就是沒有排不進來的 Pod。"),
    sort_by="已經等了", w=12, h=8, base=f'({UNSCHED}) * 0')
ts(L, "【叢集】排不進來的 Pod 與排程失敗",
   [f'count({UNSCHED}) or vector(0)',
    'sum(scheduler_pending_pods{queue="unschedulable"})',
    f'sum(rate(scheduler_schedule_attempts_total{{result="unschedulable"}}[{WR}])) * 60'],
   ["排不進來的 Pod（kube-state-metrics）", "排程器 unschedulable 佇列", "排程失敗次數／分鐘"], "none",
   desc("叢集內所有 namespace",
        "前兩條是當下排不進來的 Pod 數（兩個來源，應該差不多），第三條是排程器每分鐘找不到 Node 的次數。",
        "時間範圍拉長看最近有沒有發生過、發生在什麼時候（rollout、擴容、Node 維護）。排程器的兩條線沒資料代表沒有收集排程器指標。"))
L.newline()

L = Line()
NODE_TREND_HOW = "時間範圍拉長看是一直很滿，還是只有 rollout 時短暫衝高（rollout 時新舊 Pod 同時存在，Request 會暫時變多）。"
for name, res in (("CPU", "cpu"), ("記憶體", "memory")):
    ts(L, f"【Node】{name} Request ÷ 可分配 Top 10",
       f'topk(10, ({n_requested(res)}) / ({n_alloc(res)}))', "{{instance}}", "percentunit",
       desc(V_NODE_ALL, "每台 Node 上還沒結束的 Pod 的 Request 加總 ÷ 可分配。", NODE_TREND_HOW))
L.newline()

# 選定 Node 上每個 Pod 的明細 (所有 namespace,加起來才會等於 Node 表的數字)
NODE_SEL = pods_on("$node")


def on_sel(per_pod_expr):
    return f'(({per_pod_expr}) * on (namespace, pod) group_left (instance) {NODE_SEL})'


def node_pod_cols(resource, now, peak5m, unit, decimals, prefix):
    req, lim = on_sel(pod_spec_all("requests", resource)), on_sel(pod_spec_all("limits", resource))
    peak = f'max_over_time({on_sel(peak5m)}[{WIN}:5m])'
    return [
        (f"{prefix} Request", req, unit, None, decimals, None, NOT_SET),
        (f"{prefix} 目前用量", on_sel(now), unit, None, decimals, None),
        (f"{prefix} 尖峰", peak, unit, None, decimals, None),
        (f"{prefix} Limit", lim, unit, None, decimals, None, NOT_SET),
        (f"{prefix} 尖峰 ÷ Request", f'{peak} / {req}', "percentunit", PEAK_USE, 0, None, NOT_SET),
        (f"{prefix} Request 佔 Node", f'{req} / on (instance) group_left () ({n_alloc(resource)})',
         "percentunit", None, 1, None, NOT_SET),
    ]


# W5 也是 5m,目前用量跟尖峰 (每 5 分鐘的平均) 用同一個查詢
CPU_POD_ALL = 'sum by (namespace, pod) (%s)' % one(f'rate(container_cpu_usage_seconds_total{{{ALL_CT}}}[{W5}])')
MEM_POD_ALL = 'sum by (namespace, pod) (%s)' % one(f'container_memory_working_set_bytes{{{ALL_CT}}}')
L = Line()
table(L, "【Node】$node 上每個 Pod：Request / 用量 / 尖峰 / Limit（尖峰是過去 $waste_window）", ["namespace", "pod"],
      node_pod_cols("cpu", CPU_POD_ALL, CPU_POD_ALL, "none", 2, "CPU（核）")
      + node_pod_cols("memory", MEM_POD_ALL, MEM_POD_ALL, "bytes", 1, "記憶體"),
      desc("上方「Node」選的那台 Node 上還沒結束的每個 Pod（所有 namespace，加起來等於 Node 表的 Request）",
           "**Request 佔 Node** = 這個 Pod 的 Request ÷ Node 可分配；**尖峰** = 過去 $waste_window 最高（CPU 是每 5 分鐘的平均）；"
           "Request、Limit 是所有容器加總（沒設的容器不算）。",
           "Node 表判定「砍 Request」時，看這裡誰的 Request 佔 Node 最多、尖峰 ÷ Request 又很低（紅）：那就是該砍的，"
           "CPU 可以調到尖峰附近，記憶體調到尖峰再留一點餘裕。尖峰 ÷ Request 接近或超過 100% 的不要砍，他偶爾真的用得到。"),
      sort_by="記憶體 Request 佔 Node", h=12, base=f'({NODE_SEL}) * 0', hide=("instance",))
L.newline()

# ---------------------------------------------------------------- 6 waste
row("6. 【浪費】長期用量遠低於 Request：找出可以調低 Request 的容器（過去 $waste_window 平均）")



def per_pod_win(per_ct, agg):
    """Rolls a per-container value over the window up to (workload, container) across every pod seen in the window."""
    return f'{agg} by ({K_CT}) (({per_ct}) * on (namespace, pod) group_left (workload, workload_type) {WL_WIN})'


def waste_cols(avg_ct, peak_ct, resource, unit, decimals, suffix):
    """Per (workload, container): average and peak per pod over the window vs the request each pod sets.
    Average per pod (not the sum over pods) so pods replaced by a rollout or a change in replicas
    do not add up; the unused total multiplies by the pods running now."""
    avg, peak = per_pod_win(avg_ct, "avg"), per_pod_win(peak_ct, "max")
    req = by_wl_ct(spec("requests", resource), "max")
    cols = [
        ("Pod 數", PODS_CT, "none", None, 0, None),
        (f"平均用量／Pod{suffix}", avg, unit, None, decimals + 1, None),
        (f"尖峰／Pod{suffix}", peak, unit, None, decimals, None),
        (f"Request／Pod{suffix}", req, unit, None, decimals, None),
        ("平均 ÷ Request", f'({avg}) / ({req})', "percentunit", LOW_USE, 0, None),
        ("尖峰 ÷ Request", f'({peak}) / ({req})', "percentunit", PEAK_USE, 0, None),
        (f"沒用到的 Request（全部 Pod）{suffix}", f'clamp_min(({req}) - ({avg}), 0) * {PODS_CT}', unit, None, decimals, None),
    ]
    # 只列平均用不到 Request 一半的容器
    return cols, f'((({avg}) / ({req})) < 0.5) * 0'


# 不用 rate(...[7d]):rate 除以整段區間,rollout 換掉或剛上線的 Pod 平均會被低估;每 5 分鐘的 rate 取平均只算它活著的時間
CPU_AVG_CT = one(f'avg_over_time(rate(container_cpu_usage_seconds_total{{{CT}}}[5m])[{WIN}:5m])')
CPU_PEAK_CT = one(f'max_over_time(rate(container_cpu_usage_seconds_total{{{CT}}}[5m])[{WIN}:5m])')
MEM_AVG_CT = one(f'avg_over_time(container_memory_working_set_bytes{{{CT}}}[{WIN}])')
MEM_PEAK_CT = one(f'max_over_time(container_memory_working_set_bytes{{{CT}}}[{WIN}])')
V_WASTE = "叢集內每一個 Workload 的每個容器（同一個 Workload 的 Pod 共用一份設定，容器只列一次）"
WASTE_WHAT = ("只列**過去 $waste_window 平均用量不到 Request 一半**的容器。"
              "**平均用量／Pod** = 這段期間每個 Pod 的平均用量，再取所有 Pod 的平均（rollout 換掉的舊 Pod 也算）；"
              "**尖峰／Pod** = 最高的那個 Pod 在這段期間的最高值{peak}；"
              "**Request／Pod** = 目前每個 Pod 設的 Request；"
              "**沒用到的 Request** = (Request − 平均用量) × 目前 Pod 數，就是可以還給 Node 的量。"
              "新上線不滿 $waste_window 的 Pod 只算它活著的時間。")
L = Line()
cols, rows = waste_cols(CPU_AVG_CT, CPU_PEAK_CT, "cpu", "none", 2, "（核）")
table(L, "【Workload】CPU：長期用量遠低於 Request（過去 $waste_window 平均）", K_CT.split(", "), cols,
      desc(V_WASTE, WASTE_WHAT.format(peak="（每 5 分鐘的平均）"),
           "依「沒用到的 Request」排序，越上面浪費越多。尖峰 ÷ Request 也很低（紅）→ 連最忙的時候都用不到，"
           "可以放心把 Request 調到尖峰附近；尖峰 ÷ Request 接近或超過 100% → 平常閒、偶爾會衝，"
           "Request 可以調到平均附近，靠 Limit 讓它衝（CPU 不夠只會變慢，不會被殺）。"),
      sort_by="沒用到的 Request（全部 Pod）（核）", h=10, key_links={"workload": DETAIL_LINK},
      key_rename={**WL_RENAME, "container": "容器"}, base=rows, hide=("workload_type",))
L.newline()
L = Line()
cols, rows = waste_cols(MEM_AVG_CT, MEM_PEAK_CT, "memory", "bytes", 1, "")
table(L, "【Workload】記憶體：長期用量遠低於 Request（過去 $waste_window 平均）", K_CT.split(", "), cols,
      desc(V_WASTE, WASTE_WHAT.format(peak=""),
           "依「沒用到的 Request」排序，越上面浪費越多。記憶體不像 CPU 可以借：Request 建議調到**尖峰**附近再留一點餘裕，"
           "不要調到平均，否則 Node 記憶體不夠時會先被驅逐；尖峰 ÷ Request 也很低（紅）→ 可以放心調低。"),
      sort_by="沒用到的 Request（全部 Pod）", h=10, key_links={"workload": DETAIL_LINK},
      key_rename={**WL_RENAME, "container": "容器"}, base=rows, hide=("workload_type",))
L.newline()

L = Line()
WASTE_HOW = "時間範圍拉長（例如 7 天）看長期：兩條線差距一直很大 → Request 普遍設太高；差距只在離峰時很大 → 正常的日夜起伏。"
REQ_CPU, REQ_MEM = spec("requests", "cpu"), spec("requests", "memory")
ts(L, "【叢集】CPU：Request 總和 vs 實際用量（核）",
   [f'sum({REQ_CPU} and on (namespace, pod, container) {cpu_ct(WR)})',
    f'sum({cpu_ct(WR)} and on (namespace, pod, container) {REQ_CPU})'],
   ["Request 總和", "實際用量"], "none",
   desc("上方 namespace 篩選內，所有有設 CPU Request 且正在跑的容器加總",
        "排程器保留的 CPU（Request）跟實際用掉的 CPU。兩條線中間的空間就是被保留卻沒用到的量。", WASTE_HOW))
ts(L, "【叢集】記憶體：Request 總和 vs 實際用量",
   [f'sum({REQ_MEM} and on (namespace, pod, container) {mem_ct()})',
    f'sum({mem_ct()} and on (namespace, pod, container) {REQ_MEM})'],
   ["Request 總和", "實際用量"], "bytes",
   desc("上方 namespace 篩選內，所有有設記憶體 Request 且正在跑的容器加總",
        "排程器保留的記憶體（Request）跟實際用掉的記憶體（working set）。兩條線中間的空間就是被保留卻沒用到的量。", WASTE_HOW))
ts(L, "【Workload】沒用到的 CPU Request（核）Top 10",
   f'topk(10, clamp_min({by_wl(total("requests", "cpu"))} - {by_wl(with_request(cpu_ct(WR), "cpu"))}, 0))',
   "{{namespace}}/{{workload}}", "none",
   desc(V_CLUSTER_WL, "Request − 實際用量（只算有設 Request 的容器，所有 Pod 加總）。", WASTE_HOW + "一直在 Top 10 的，到上方表格看是哪個容器。"))
ts(L, "【Workload】沒用到的記憶體 Request Top 10",
   f'topk(10, clamp_min({by_wl(total("requests", "memory"))} - {by_wl(with_request(mem_ct(), "memory"))}, 0))',
   "{{namespace}}/{{workload}}", "bytes",
   desc(V_CLUSTER_WL, "Request − 實際用量（只算有設 Request 的容器，所有 Pod 加總）。", WASTE_HOW + "一直在 Top 10 的，到上方表格看是哪個容器。"))
L.newline()

# ---------------------------------------------------------------- dashboard
dash = {
    "uid": "ocp-cluster-risk-overview",
    "title": "OCP 叢集風險總覽（找出危險 Workload 與 Node）",
    "tags": ["ocp", "loadtest", "capacity", "overview"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 6,
    "editable": True,
    "graphTooltip": 1,
    "refresh": "1m",
    "time": {"from": "now-3h", "to": "now"},
    "links": [{"title": "單一 Workload 細節（OCP 壓測影響評估）", "type": "link", "icon": "dashboard",
               "url": "/d/" + DETAIL_UID + "/?var-datasource=${datasource}", "keepTime": True,
               "targetBlank": False}],
    "templating": {"list": [
        {"type": "datasource", "name": "datasource", "label": "資料來源（叢集）",
         "query": "prometheus", "regex": "/^Prometheus.*OCP$/",
         "current": {}, "hide": 0, "refresh": 1},
        {"type": "query", "name": "namespace", "label": "Namespace", "datasource": DS,
         "query": "label_values(kube_pod_info, namespace)",
         "definition": "label_values(kube_pod_info, namespace)",
         "refresh": 2, "sort": 1, "multi": True, "includeAll": True, "allValue": ".*",
         "current": {"text": "All", "value": "$__all"}, "hide": 0},
        {"type": "textbox", "name": "exclude", "label": "排除的 Namespace（regex）",
         "query": "openshift.*|kube-.*",
         "current": {"text": "openshift.*|kube-.*", "value": "openshift.*|kube-.*"}, "hide": 0},
        {"type": "query", "name": "node", "label": "Node（第 5 區明細）", "datasource": DS,
         "query": "label_values(kube_node_info, node)", "definition": "label_values(kube_node_info, node)",
         "refresh": 2, "sort": 1, "multi": False, "includeAll": False, "current": {}, "hide": 0},
        {"type": "custom", "name": "waste_window", "label": "長期分析區間（第 5、6 區）",
         "query": "1d,7d,14d,30d", "current": {"text": "7d", "value": "7d"},
         "options": [{"text": v, "value": v, "selected": v == "7d"} for v in ("1d", "7d", "14d", "30d")],
         "multi": False, "includeAll": False, "hide": 0},
    ]},
    "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                              "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                              "name": "Annotations & Alerts", "type": "dashboard"}]},
    "panels": panels,
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(dash, f, ensure_ascii=False, indent=2)
print("panels:", len(panels))
