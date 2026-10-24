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

# Pod -> workload mapping: Deployments through their ReplicaSet, StatefulSets and DaemonSets directly.
WL = ('max by (namespace, pod, workload, workload_type) ('
      'label_replace(label_replace('
      f'label_replace(kube_pod_owner{{owner_kind="ReplicaSet", {NSF}}}, "replicaset", "$1", "owner_name", "(.*)")'
      ' * on (namespace, replicaset) group_left (owner_deploy) '
      'max by (namespace, replicaset, owner_deploy) ('
      f'label_replace(kube_replicaset_owner{{owner_kind="Deployment", {NSF}}}, "owner_deploy", "$1", "owner_name", "(.*)"))'
      ', "workload", "$1", "owner_deploy", "(.*)"), "workload_type", "Deployment", "namespace", ".*")'
      ' or label_replace(label_replace('
      f'kube_pod_owner{{owner_kind=~"StatefulSet|DaemonSet", {NSF}}}, "workload", "$1", "owner_name", "(.*)")'
      ', "workload_type", "$1", "owner_kind", "(.*)"))')


def by_wl(per_pod, agg="sum"):
    """Rolls a per-pod vector (labels: namespace, pod) up to its workload."""
    return (f'{agg} by (namespace, workload, workload_type) ('
            f'{per_pod} * on (namespace, pod) group_left (workload, workload_type) {WL})')


# ---- per-pod building blocks (result labels: namespace, pod)
def cpu(w):
    return f'sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{{container!="", container!="POD", {NSF}}}[{w}]))'


def cpu_req():
    return f'sum by (namespace, pod) (kube_pod_container_resource_requests{{resource="cpu", {NSF}}})'


def throttle(w):
    return (f'(sum by (namespace, pod) (rate(container_cpu_cfs_throttled_periods_total{{container!="", {NSF}}}[{w}]))'
            f' / sum by (namespace, pod) (rate(container_cpu_cfs_periods_total{{container!="", {NSF}}}[{w}])))')


def mem_limit():
    return (f'max by (namespace, pod) (container_memory_working_set_bytes{{container!="", container!="POD", {NSF}}}'
            f' / on (namespace, pod, container) max by (namespace, pod, container) '
            f'(kube_pod_container_resource_limits{{resource="memory", {NSF}}}))')


def restarts():
    return f'sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total{{{NSF}}}[1h]))'


def net(w):
    return (f'(sum by (namespace, pod) (rate(container_network_transmit_bytes_total{{{NSF}}}[{w}]))'
            f' + sum by (namespace, pod) (rate(container_network_receive_bytes_total{{{NSF}}}[{w}])))')


def drops(w):
    return (f'(sum by (namespace, pod) (rate(container_network_transmit_packets_dropped_total{{{NSF}}}[{w}]))'
            f' + sum by (namespace, pod) (rate(container_network_receive_packets_dropped_total{{{NSF}}}[{w}])))')


def disk_write(w):
    """Same fallback order as the detail dashboard: container fs stats, pod fs stats, pod blkio."""
    return (f'(sum by (namespace, pod) (rate(container_fs_writes_bytes_total{{{NSF}, container!="", container!="POD"}}[{w}]))'
            f' or sum by (namespace, pod) (rate(container_fs_writes_bytes_total{{{NSF}, container="", pod!=""}}[{w}]))'
            f' or sum by (namespace, pod) (rate(container_blkio_device_usage_total{{{NSF}, operation="Write", container="", pod!=""}}[{w}])))')


def fs_usage():
    return f'sum by (namespace, pod) (container_fs_usage_bytes{{container!="", container!="POD", {NSF}}})'


# ---- per-node building blocks (result label: instance = node name)
def n_cpu(w):
    return f'1 - avg by (instance) (rate(node_cpu_seconds_total{{mode="idle"}}[{w}]))'


def n_iowait(w):
    return f'avg by (instance) (rate(node_cpu_seconds_total{{mode="iowait"}}[{w}]))'


def n_softirq(w):
    return f'avg by (instance) (rate(node_cpu_seconds_total{{mode="softirq"}}[{w}]))'


def n_mem():
    return 'max by (instance) (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'


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
    line.add({
        "id": nid(), "type": "timeseries", "title": title, "description": description,
        "datasource": DS,
        "targets": [{"refId": "A", "datasource": DS, "expr": expr, "legendFormat": legend,
                     "range": True, "instant": False}],
        "fieldConfig": {"defaults": {"unit": unit, "min": 0,
                                     "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 10,
                                                "showPoints": "never", "spanNulls": True}},
                        "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["max"],
                               "sortBy": "Max", "sortDesc": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }, w, h)


def table(line, title, key_cols, columns, description, sort_by, w=24, h=12, key_rename=None, key_links=None):
    """columns: list of (name, expr, unit, thresholds or None, decimals, value mappings or None)."""
    targets, rename, index, overrides = [], {}, {}, []
    for i, k in enumerate(key_cols):
        index[k] = i
    for i, (name, expr, unit, thr, decimals, mappings) in enumerate(columns):
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
            {"id": "organize", "options": {"excludeByName": {"Time": True},
                                           "renameByName": rename, "indexByName": index}},
        ],
        "fieldConfig": {"defaults": {"custom": {"filterable": True, "align": "auto"}}, "overrides": overrides},
        "options": {"showHeader": True, "sortBy": [{"displayName": sort_by, "desc": True}],
                    "footer": {"show": False}},
    }, w, h)


V_CLUSTER_NODE = "叢集內每一台 Node（該 Node 上所有 Pod 加總）"
V_CLUSTER_WL = "叢集內每一個 Workload（該 Workload 所有 Pod 合計）"

# ---------------------------------------------------------------- guide
L = Line()
text(L, """### 叢集風險總覽：先找出嫌疑對象，再進細節 Dashboard 查證
一個資料來源 = 一個 OCP 叢集。上方切換資料來源就是切換叢集。

**建議流程**
1. **第 1 區【Node】**：先看哪幾台 Node 正在變吃緊（conntrack、磁碟延遲、網路丟包、iowait）。
2. **第 2 區【Workload】風險排行**：表頭可點擊排序，每欄右側的漏斗可以篩選。紅色、黃色格子就是要注意的地方。
3. **點表格中的 `workload` 名稱**：會帶著 namespace、workload 與目前時間範圍，開啟「OCP 壓測影響評估」細節 Dashboard，查他所在 Node 有沒有被拖慢（細節 Dashboard 目前只支援 Deployment）。
4. **第 3、4 區 Top 10 趨勢**：看是誰在什麼時間點飆起來，對照第 1 區 Node 變吃緊的時間。

**百分比的分母都寫在欄位名稱的「÷ …」**：÷ 自己的 Request / Limit 是跟他自己的設定比；÷ 總核數、總記憶體、容量、表格上限是跟 Node 的容量比。

**注意**：表格是時間範圍**結束前 5 分鐘**的快照，要看壓測當下，請把時間範圍的結束點拉到壓測期間。用量排名高只代表是大戶，不代表造成影響，要進細節 Dashboard 對照 Node 壓力的時間點。預設排除 `openshift*`、`kube-*` 系統 namespace，可在上方「排除的 Namespace」清空。""", h=11)
L.newline()

# ---------------------------------------------------------------- 1 nodes
row("1. 【Node】叢集各 Node 狀態：哪台機器正在變吃緊")
L = Line()
table(L, "【Node】各 Node 狀態（時間範圍結束前 5 分鐘）", ["instance"], [
    ("Pod 數", n_pods(), "none", None, 0, None),
    ("CPU 使用率（÷ 總核數）", n_cpu(W5), "percentunit", steps((Y, 0.8), (RD, 0.9)), 1, None),
    ("剩餘記憶體（÷ 總記憶體）", n_mem(), "percentunit", steps((Y, 0.1), (G, 0.2), base=RD), 1, None),
    ("conntrack（÷ 表格上限）", n_conntrack(), "percentunit", steps((Y, 0.5), (RD, 0.7)), 1, None),
    ("磁碟延遲（最慢的磁碟）", n_disk_latency(W5), "s", steps((Y, 0.01), (RD, 0.02)), None, None),
    ("磁碟空間（÷ 容量，最高）", n_fs(), "percentunit", steps((Y, 0.8), (RD, 0.85)), 1, None),
    ("網路丟包", n_drops(W5), "pps", steps((Y, 1), (RD, 50)), 1, None),
    ("iowait（÷ CPU 時間）", n_iowait(W5), "percentunit", steps((Y, 0.05), (RD, 0.1)), 1, None),
    ("網路封包處理 CPU（÷ CPU 時間）", n_softirq(W5), "percentunit", steps((Y, 0.05), (RD, 0.1)), 1, None),
    ("Node 壓力狀態", n_pressure(), "none", steps((RD, 1)), 0,
     [{"type": "value", "options": {"0": {"text": "正常"}, "1": {"text": "發生中"}}}]),
], desc(V_CLUSTER_NODE,
        "每台 Node 共用資源的目前狀態。conntrack、磁碟、網卡都是每台 Node 一份，所有 Pod 共用。"
        "「網路封包處理 CPU」是核心處理網路封包（softirq）的 CPU，不算在 Pod 的 CPU limit 裡。",
        "紅色、黃色就是正在吃緊的 Node。記下 Node 名稱，再到第 2 區找跑在上面的大戶，或進細節 Dashboard 看該 Node 的 Top 10 鄰居。"),
    sort_by="conntrack（÷ 表格上限）", h=10, key_rename={"instance": "Node"})
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
L = Line()
table(L, "【Workload】風險排行（時間範圍結束前 5 分鐘）", ["namespace", "workload", "workload_type"], [
    ("Pod 數", f'count by (namespace, workload, workload_type) ({WL})', "none", None, 0, None),
    ("CPU 用量（核）", by_wl(cpu(W5)), "none", None, 2, None),
    ("CPU（÷ 自己的 Request）", f'{by_wl(cpu(W5))} / {by_wl(cpu_req())}', "percentunit", steps((Y, 1.0), (RD, 1.5)), 0, None),
    ("最高 CPU throttling", by_wl(throttle(W5), "max"), "percentunit", steps((Y, 0.25), (RD, 0.5)), 0, None),
    ("記憶體（÷ 自己的 Limit，最高）", by_wl(mem_limit(), "max"), "percentunit", steps((Y, 0.8), (RD, 0.9)), 0, None),
    ("重啟（1 小時）", by_wl(restarts()), "none", steps((Y, 1), (RD, 5)), 0, None),
    ("網路流量", by_wl(net(W5)), "Bps", None, 1, None),
    ("網路丟包", by_wl(drops(W5)), "pps", steps((Y, 1), (RD, 50)), 1, None),
    ("磁碟寫入", by_wl(disk_write(W5)), "Bps", None, 1, None),
    ("可寫層用量", by_wl(fs_usage()), "bytes", None, 1, None),
], desc(V_CLUSTER_WL,
        "全叢集每個 Deployment / StatefulSet / DaemonSet 的用量與風險指標。",
        "點表頭排序找出前幾名；紅色、黃色格子要注意。CPU ÷ 自己的 Request 超過 100% 代表在借用 Node 的共用 CPU；"
        "網路丟包、磁碟寫入、可寫層用量是 limit 管不到、會影響同 Node 鄰居的資源。點 workload 名稱進細節 Dashboard 查證。"),
    sort_by="CPU（÷ 自己的 Request）", h=14, key_links={"workload": DETAIL_LINK})
L.newline()

# ---------------------------------------------------------------- 3/4 workload trends
WL_HOW = "看誰在什麼時間往上飆，對照第 1 區 Node 變吃緊的時間點；右側圖例依最大值排序。"
row("3. 【Workload】limit 相關 Top 10 趨勢（主要影響他自己）")
L = Line()
ts(L, "【Workload】CPU ÷ 自己的 Request Top 10",
   f'topk(10, {by_wl(cpu(WR))} / {by_wl(cpu_req())})', "{{namespace}}/{{workload}}", "percentunit",
   desc(V_CLUSTER_WL, "CPU 用量占保證額度的比例。超過 100% 代表在借用 Node 的閒置 CPU。", WL_HOW))
ts(L, "【Workload】最高 CPU throttling Top 10",
   f'topk(10, {by_wl(throttle(WR), "max")})', "{{namespace}}/{{workload}}", "percentunit",
   desc(V_CLUSTER_WL, "該 Workload 中被 limit 擋最多的 Pod 的節流比例。只影響他自己。", WL_HOW))
ts(L, "【Workload】記憶體 ÷ 自己的 Limit（最高）Top 10",
   f'topk(10, {by_wl(mem_limit(), "max")})', "{{namespace}}/{{workload}}", "percentunit",
   desc(V_CLUSTER_WL, "最接近 memory limit 的容器的使用比例，到 100% 會被 OOMKilled。", WL_HOW))
ts(L, "【Workload】重啟次數 Top 10（每 1 小時）",
   f'topk(10, {by_wl(restarts())})', "{{namespace}}/{{workload}}", "none",
   desc(V_CLUSTER_WL, "過去 1 小時的重啟次數。", WL_HOW))
L.newline()
row("4. 【Workload】共用資源 Top 10 趨勢（limit 管不到，會影響同 Node 鄰居）")
L = Line()
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

# ---------------------------------------------------------------- dashboard
dash = {
    "uid": "ocp-cluster-risk-overview",
    "title": "OCP 叢集風險總覽（找出危險 Workload 與 Node）",
    "tags": ["ocp", "loadtest", "capacity", "overview"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 3,
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
    ]},
    "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                              "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                              "name": "Annotations & Alerts", "type": "dashboard"}]},
    "panels": panels,
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(dash, f, ensure_ascii=False, indent=2)
print("panels:", len(panels))
