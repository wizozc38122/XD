"""Generate the OCP load-test impact Grafana dashboard.

Usage: python gen_dashboard.py ocp-loadtest-impact-dashboard.json
"""
import json
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "ocp-loadtest-impact-dashboard.json"
DS = {"type": "prometheus", "uid": "${datasource}"}

POD = 'namespace="$namespace", pod=~"$workload-[a-z0-9]+-[a-z0-9]+"'
CT = POD + ', container!="", container!="POD"'
R = "$__rate_interval"
DEV = 'device=~"sd[a-z]+|nvme[0-9]+n[0-9]+|vd[a-z]+|xvd[a-z]+"'
NIC = 'device=~"(ens|eth|eno|enp|bond)[a-z0-9]*"'
NODE = 'instance=~"$node"'
# Keeps only the series of pods that run on the selected nodes.
ON_NODE = ' * on (namespace, pod) group_left () max by (namespace, pod) (kube_pod_info{node=~"$node"})'


def one(expr, by="namespace, pod, container"):
    """Keeps one series per container (or per `by`) before summing. Some clusters scrape the same
    container metric from two kubelet endpoints (/metrics/cadvisor and /metrics/resource), which
    would otherwise double every sum and break one-to-one divisions."""
    return f'max by ({by}) ({expr})'


# Deduplicated per-container building blocks for his pods.
CPU_USE = one('rate(container_cpu_usage_seconds_total{@CT@}[@R@])')
MEM_WS = one('container_memory_working_set_bytes{@CT@}')
THROTTLED = one('rate(container_cpu_cfs_throttled_periods_total{@CT@}[@R@])')
PERIODS = one('rate(container_cpu_cfs_periods_total{@CT@}[@R@])')
FS_USE = one('container_fs_usage_bytes{@CT@}', "namespace, pod, container, device")


def spec(kind, resource):
    """Request or limit per container. 0 means "not set" to Kubernetes and would divide by zero, so it is dropped."""
    return one(f'kube_pod_container_resource_{kind}{{resource="{resource}", @POD@}} > 0')


def net(metric, sel="@POD@"):
    return one(f'rate(container_network_{metric}{{{sel}}}[@R@])', "namespace, pod, interface")


def pod_io(op, sel=""):
    """Per-pod disk bytes/s. Falls back through the metrics clusters commonly keep:
    container-level cAdvisor fs stats, then pod-level fs stats, then pod-level blkio."""
    fs, blk = ("writes", "Write") if op == "write" else ("reads", "Read")
    s = sel + ", " if sel else ""
    by_dev = "namespace, pod, container, device"
    ctr = one(f'rate(container_fs_{fs}_bytes_total{{{s}container!="", container!="POD"}}[@R@])', by_dev)
    pod = one(f'rate(container_fs_{fs}_bytes_total{{{s}container="", pod!=""}}[@R@])', by_dev)
    blkio = one(f'rate(container_blkio_device_usage_total{{{s}operation="{blk}", container="", pod!=""}}[@R@])',
                "namespace, pod, device, operation")
    return (f'(sum by (namespace, pod) ({ctr}) or sum by (namespace, pod) ({pod})'
            f' or sum by (namespace, pod) ({blkio}))')


NO_POD_IO = "無資料：叢集沒有收集 Pod 層級的磁碟 IO 指標，請改看第 4 區整台 Node 的磁碟面板"
FS = 'mountpoint=~"/sysroot|/var|/", fstype!~"tmpfs|overlay|squashfs"'


def as_node(expr):
    """Renames node-exporter's instance label to node so it joins with kube_pod_info."""
    return f'max by (node) (label_replace({expr}, "node", "$1", "instance", "(.*)"))'


def his_by_node(per_pod):
    """Sums a per-pod vector (labels: namespace, pod) of his pods per node."""
    return (f'sum by (node) ({per_pod} * on (namespace, pod) group_left (node) '
            f'max by (namespace, pod, node) (kube_pod_info{{node=~"$node"}}))')


def manual_cap(var, factor):
    """A per-node constant from a dashboard variable; dropped when the variable is 0."""
    return f'(max by (instance) (node_uname_info{{@NODE@}}) * 0 + ${var} * {factor} > 0)'


# Capacities per node (label: node). The NIC uses the speed the node reports unless the
# hidden nic_mbps variable overrides it (e.g. ?var-nic_mbps=1000 in the URL).
NET_CAP = as_node(manual_cap("nic_mbps", 125000) + ' or max by (instance) (node_network_speed_bytes{@NODE@, @NIC@})')
FS_CAP = as_node(f'max by (instance) (node_filesystem_size_bytes{{@NODE@, {FS}}})')


def q(s):
    return (s.replace("@POD@", POD).replace("@CT@", CT).replace("@R@", R)
            .replace("@DEV@", DEV).replace("@NIC@", NIC).replace("@NODE@", NODE)
            .replace("@ON_NODE@", ON_NODE))


def desc(view, what, how):
    return f"**視角**：{view}\n\n**這是什麼**：{what}\n\n**怎麼看**：{how}"


V_SELF = "他自己（這個 Deployment 的 Pod 合計）"
V_SHARE = "他 ÷ 這台 Node **目前實際的總用量**（每台 Node 一條線，只算他跑在該 Node 上的 Pod）"
V_HIS_CAP = "他 ÷ 這台 Node 的**容量**（每台 Node 一條線，只算他跑在該 Node 上的 Pod）"
V_NODE_CAP = "整台 Node ÷ **容量**（所有 Pod 加總，**不是他的用量**），每台 Node 一條線"
V_HIS_PEAK = "他的 Pod 合計：目前（實線）vs 他自己壓測前 7 天峰值（虛線）"
V_NODE_PEAK = "整台 Node（所有 Pod 加總）：目前（實線）vs 這台 Node 壓測前 7 天峰值（虛線），每台 Node 各一組"
V_NODE = "整台 Node（所有 Pod 加總），**不是他自己的用量**"
V_NEIGHBOR = "所選 Node 上的所有 Pod 排名（含他自己）"
V_SHARED = "全叢集共用元件，不分 Node"

panels = []
_id = [0]
_y = [0]


def nid():
    _id[0] += 1
    return _id[0]


def targets(items, instant=False, fmt="time_series"):
    out = []
    for i, (expr, legend) in enumerate(items):
        out.append({"refId": chr(65 + i), "datasource": DS, "expr": q(expr),
                    "legendFormat": legend, "range": not instant, "instant": instant,
                    "format": fmt})
    return out


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


def ts(line, title, items, unit, description, w=8, h=7, minv=None, no_value=None, thresholds=None,
       overrides=None):
    custom = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 12,
              "showPoints": "never", "spanNulls": True}
    defaults = {"unit": unit, "custom": custom}
    if minv is not None:
        defaults["min"] = minv
    if no_value:
        defaults["noValue"] = no_value
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
        custom["thresholdsStyle"] = {"mode": "dashed"}
    line.add({
        "id": nid(), "type": "timeseries", "title": title, "description": description,
        "datasource": DS, "targets": targets(items),
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }, w, h)


# Draws every series whose name contains 峰值 as a dashed, unfilled line.
PEAK_DASHED = [{"matcher": {"id": "byRegexp", "options": ".*峰值.*"},
                "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                               {"id": "custom.fillOpacity", "value": 0},
                               {"id": "custom.lineWidth", "value": 2}]}]


def stat(line, title, expr, unit, steps, description, w=4, h=4, decimals=None):
    defaults = {"unit": unit,
                "thresholds": {"mode": "absolute", "steps": steps},
                "color": {"mode": "thresholds"}}
    if decimals is not None:
        defaults["decimals"] = decimals
    line.add({
        "id": nid(), "type": "stat", "title": title, "description": description,
        "datasource": DS, "targets": targets([(expr, "")]),
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "area", "textMode": "auto"},
    }, w, h)


def table(line, title, expr, description, w=12, h=7):
    line.add({
        "id": nid(), "type": "table", "title": title, "description": description,
        "datasource": DS, "targets": targets([(expr, "")], instant=True, fmt="table"),
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {"showHeader": True},
        "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True}}}],
    }, w, h)


def multi_table(line, title, key, columns, description, w=24, h=6):
    """One row per key value; columns: list of (name, expr, unit, decimals). Each column is an instant query."""
    targets, rename, index, overrides = [], {key: "Node"}, {key: 0}, []
    for i, (name, expr, unit, decimals) in enumerate(columns):
        ref = chr(65 + i)
        targets.append({"refId": ref, "datasource": DS, "expr": q(expr), "legendFormat": "",
                        "range": False, "instant": True, "format": "table"})
        rename[f"Value #{ref}"] = name
        index[f"Value #{ref}"] = i + 1
        overrides.append({"matcher": {"id": "byName", "options": name},
                          "properties": [{"id": "unit", "value": unit}, {"id": "decimals", "value": decimals}]})
    line.add({
        "id": nid(), "type": "table", "title": title, "description": description,
        "datasource": DS, "targets": targets,
        "transformations": [
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {"excludeByName": {"Time": True},
                                           "renameByName": rename, "indexByName": index}},
        ],
        "fieldConfig": {"defaults": {}, "overrides": overrides},
        "options": {"showHeader": True},
    }, w, h)


def text(line, content, w=24, h=8):
    line.add({"id": nid(), "type": "text", "title": "",
              "options": {"mode": "markdown", "content": content}}, w, h)


G, Y, RD = "green", "yellow", "red"


def steps(*pairs):
    out = [{"color": G, "value": None}]
    for color, v in pairs:
        out.append({"color": color, "value": v})
    return out


# ---------------------------------------------------------------- guide
L = Line()
text(L, """**目的**：壓測時判斷 `$workload`（`$namespace`）有沒有拖慢**同一台 Node** 上的其他服務。重點是 limit 管不到的共用資源：網路、磁碟 IO、conntrack。

**標題前綴 = 在看誰**：【他】他自己的 Pod（主要看這個）｜【整台 Node】他所在 Node 的所有 Pod 加總，不是他的用量｜【鄰居】同 Node 所有 Pod 排名｜【共用】Router、DNS

**百分比先看分母（標題的「÷ …」）**：÷ 自己的 Request / Limit → 有沒有超出自己的設定（1、2 區）｜÷ Node 目前用量 → 是不是大戶，不代表快滿（3A）｜÷ Node 容量 → 離滿還多遠，**主要看這個**（3B 他、3D 整台）｜3C、3E 的虛線 = 壓測前 7 天峰值

**判讀**：「他的用量上升」「整台 Node 壓力上升」「鄰居受影響」三者時間對得上，才算他影響了整體。

**操作**：選 Namespace、Deployment（Node 會自動帶出）→ 時間範圍要包含壓測**前後**。每個面板的 ⓘ 有說明。""", w=24, h=10)
L.newline()

# ---------------------------------------------------------------- 1 summary
row("1. 總覽：他自己有沒有被限制卡住、部署在哪")
L = Line()
stat(L, "【他】可用副本數",
     'kube_deployment_status_replicas_available{namespace="$namespace", deployment="$workload"}',
     "none", steps(),
     desc(V_SELF, "目前 Ready 的 Pod 數量。", "壓測中如果 HPA 擴容，副本數會上升，Pod 也可能被排到新的 Node。"),
     decimals=0)
stat(L, "【他】CPU 用量 ÷ 自己的 Request",
     f'sum({CPU_USE} and on (namespace, pod, container) {spec("requests", "cpu")}) / sum({spec("requests", "cpu")})',
     "percentunit", steps((Y, 1.0), (RD, 1.5)),
     desc(V_SELF, "CPU 用量占 request（保證額度）的比例，只算有設 request 的容器。",
          "超過 100% = 正在借用 Node 的閒置 CPU，Node 吃緊時會和鄰居互搶。沒設 request 則沒有資料。"))
stat(L, "【他】記憶體 ÷ 自己的 Limit（最高的容器）",
     f'max({MEM_WS} / on (namespace, pod, container) {spec("limits", "memory")})',
     "percentunit", steps((Y, 0.8), (RD, 0.9)),
     desc(V_SELF, "最接近 limit 的那個容器，記憶體用量占 limit 的比例。",
          "到 100% 會被 OOMKilled。沒設 memory limit（或設 0）則沒有資料。"))
stat(L, "【他】CPU throttling（最高的 Pod）",
     f'max(sum by (pod) ({THROTTLED}) / sum by (pod) ({PERIODS}))',
     "percentunit", steps((Y, 0.25), (RD, 0.5)),
     desc(V_SELF, "CPU 想用但被 limit 擋下的時間比例。", "超過 25% 通常已讓他自己變慢；只影響他自己，不會拖累鄰居。"))
stat(L, "【他】近 1 小時重啟次數",
     'sum(increase(kube_pod_container_status_restarts_total{@POD@}[1h]))',
     "none", steps((RD, 1)),
     desc(V_SELF, "所有 Pod 近 1 小時重啟次數合計。", "大於 0 → 看 2B 重啟紀錄的原因。"),
     decimals=0)
stat(L, "【他】CPU ÷ 所選 Node 總核數",
     f'sum({CPU_USE}@ON_NODE@) / count(node_cpu_seconds_total{{mode="idle", @NODE@}})',
     "percentunit", steps((Y, 0.5), (RD, 0.8)),
     desc("他 ÷ 所選 Node 的 CPU 容量合計",
          "他用掉所選 Node 總核數的幾成（分母是容量，其他服務停掉也不會被灌高）。",
          "每台 Node 分開看 3B；整台 Node 快不快滿看 3D。"))
L.newline()
L = Line()
ts(L, "【他】各 Node 上的 Pod 數（部署位置）",
   [('count by (node) (kube_pod_info{@POD@})', "{{node}}")],
   "none",
   desc(V_SELF, "他的 Pod 分布在哪些 Node、各有幾個。",
        "第 4、5 區看的就是這些 Node。壓測中 HPA 擴容或 Pod 重建時，可能出現新的 Node，要把新 Node 也一起看。"),
   w=12, minv=0)
table(L, "【他】目前 Pod 與 Node 對應",
      'max by (pod, node, pod_ip) (kube_pod_info{@POD@})',
      desc(V_SELF, "目前每個 Pod 跑在哪台 Node、Pod IP。",
           "如果他的 Pod 和重要服務在同一台 Node，要特別留意第 4 區。"),
      w=12)
L.newline()

# ---------------------------------------------------------------- 2 workload
row("2. 【他】自己的用量（只看他的 Pod）")
L = Line()
ts(L, "【他】CPU：用量 vs Request / Limit / 昨天同時段",
   [(f'sum({CPU_USE})', "用量"),
    (f'sum({spec("requests", "cpu")})', "request 合計"),
    (f'sum({spec("limits", "cpu")})', "limit 合計"),
    ('sum(' + one('rate(container_cpu_usage_seconds_total{@CT@}[@R@] offset 1d)') + ')', "昨天同時段")],
   "none",
   desc(V_SELF, "他的 Pod 合計 CPU 核數，對照 request、limit 與昨天同一時間。",
        "用量遠高於昨天同時段，代表這次壓測確實加重了負載；貼近 limit 線會開始被 throttling。"),
   w=12)
ts(L, "【他】記憶體：working set vs Request / Limit",
   [(f'sum({MEM_WS})', "working set"),
    (f'sum({spec("requests", "memory")})', "request 合計"),
    (f'sum({spec("limits", "memory")})', "limit 合計")],
   "bytes",
   desc(V_SELF, "working set 是 OOM 判斷依據的記憶體用量。",
        "持續上升不回落可能是請求堆積或洩漏；碰到 limit 會被 OOMKilled。"),
   w=12)
ts(L, "【他】CPU throttling 比例（各 Pod）",
   [(f'sum by (pod) ({THROTTLED}) / sum by (pod) ({PERIODS})', "{{pod}}")],
   "percentunit",
   desc(V_SELF, "每個 Pod 的 CPU 被 limit 擋下的時間比例。",
        "超過 25% 代表他自己變慢，可能引發 timeout 與重試，進而放大對外部服務的流量。"),
   minv=0)
ts(L, "【他】網路流量",
   [(f'sum({net("transmit_bytes_total")})', "送出 TX"),
    (f'sum({net("receive_bytes_total")})', "接收 RX")],
   "Bps",
   desc(V_SELF, "他的 Pod 合計網路流量（Bytes/s）。",
        "和第 4 區「整台 Node 網路吞吐」對照，看 Node 的流量上升是不是主要來自他。"))
ts(L, "【他】網路丟包 / 錯誤",
   [(f'sum({net("transmit_packets_dropped_total")})', "TX 丟包"),
    (f'sum({net("receive_packets_dropped_total")})', "RX 丟包"),
    (f'sum({net("transmit_errors_total")})', "TX 錯誤"),
    (f'sum({net("receive_errors_total")})', "RX 錯誤")],
   "pps",
   desc(V_SELF, "他的 Pod 網路介面上每秒丟掉或出錯的封包數。",
        "正常應接近 0。壓測時開始出現，代表他自己的網路已經塞住，常見原因是流量太大或接收端處理不及。"),
   minv=0)
ts(L, "【他】磁碟讀寫量",
   [('sum(' + pod_io("write", POD) + ')', "寫入"),
    ('sum(' + pod_io("read", POD) + ')', "讀取")],
   "Bps",
   desc(V_SELF, "他的容器每秒讀寫磁碟的量，包含寫 log、暫存檔、容器內寫檔。",
        "和第 4 區「整台 Node 磁碟」對照。他寫得多、Node 磁碟延遲同時上升，就是 IO 影響的證據。數字用來看趨勢，不一定精確。"),
   w=12, no_value=NO_POD_IO)
ts(L, "【他】容器可寫層 / 暫存空間用量",
   [(f'sum({FS_USE})', "已使用")],
   "bytes",
   desc(V_SELF, "他的容器在 Node 本機磁碟上佔用的空間（可寫層、emptyDir 等）。",
        "持續成長代表一直在寫檔或 log，可能把 Node 磁碟寫滿，觸發 DiskPressure 並驅逐其他 Pod。"),
   w=12)
L.newline()


# ---------------------------------------------------------------- 2B restarts
row("2B. 【他】重啟紀錄與原因")
L = Line()
RESTARTS = 'max by (namespace, pod, container) (kube_pod_container_status_restarts_total{@POD@})'
LAST_REASON = ('max by (namespace, pod, container, reason) '
               '(kube_pod_container_status_last_terminated_reason{@POD@} == 1)')
ts(L, "【他】重啟次數（依上次結束原因，每 5 分鐘）",
   [(f'sum by (reason) (max by (namespace, pod, container) (increase(kube_pod_container_status_restarts_total{{@POD@}}[5m]))'
     f' * on (namespace, pod, container) group_left (reason) {LAST_REASON})', "重啟：{{reason}}"),
    ('count by (reason) (kube_pod_container_status_waiting_reason{@POD@} == 1)', "等待中：{{reason}}"),
    ('count(kube_pod_status_reason{@POD@, reason="Evicted"} == 1)', "被驅逐（Evicted）的 Pod")],
   "none",
   desc(V_SELF, "每 5 分鐘重啟幾次，依容器上次結束的原因分開；以及目前卡在等待狀態（CrashLoopBackOff 等）的容器數、被驅逐的 Pod 數。",
        "OOMKilled → 記憶體超過 limit；Error → 程式異常結束（exit code 看右表）；"
        "CrashLoopBackOff → 一直啟動失敗，Kubernetes 拉長重試間隔。Evicted → Node 資源不足被趕走，看第 4 區。"),
   w=24, h=7, minv=0)
L.newline()
L = Line()
EXIT_CODES = [{"type": "value", "options": {
    "0": {"text": "0（正常結束）", "index": 0},
    "1": {"text": "1（程式錯誤）", "color": "orange", "index": 1},
    "126": {"text": "126（無法執行）", "color": "orange", "index": 2},
    "127": {"text": "127（找不到指令）", "color": "orange", "index": 3},
    "137": {"text": "137（被強制終止）", "color": "red", "index": 4},
    "139": {"text": "139（Segfault）", "color": "red", "index": 5},
    "143": {"text": "143（收到終止訊號）", "index": 6}}}]
restart_cols = [
    ("時間範圍內重啟", 'max by (pod, container) (increase(kube_pod_container_status_restarts_total{@POD@}[$__range]))',
     [{"id": "decimals", "value": 0}, {"id": "noValue", "value": "0"},
      {"id": "thresholds", "value": {"mode": "absolute", "steps": steps((RD, 1))}},
      {"id": "custom.cellOptions", "value": {"type": "color-text"}}]),
    ("累計重啟", f'max by (pod, container) ({RESTARTS})', [{"id": "decimals", "value": 0}]),
    # 文字欄:原因放在 label,值欄隱藏
    ("上次結束原因", 'max by (pod, container, last_reason) (label_replace('
                   'kube_pod_container_status_last_terminated_reason{@POD@} == 1, "last_reason", "$1", "reason", "(.*)"))', None),
    ("上次 exit code", 'max by (pod, container) (kube_pod_container_status_last_terminated_exitcode{@POD@})',
     [{"id": "mappings", "value": EXIT_CODES}, {"id": "custom.cellOptions", "value": {"type": "color-text"}}]),
    ("上次結束", 'max by (pod, container) (kube_pod_container_status_last_terminated_timestamp{@POD@}) * 1000',
     [{"id": "unit", "value": "dateTimeFromNow"}]),
    ("目前等待原因", 'max by (pod, container, waiting) (label_replace('
                   'kube_pod_container_status_waiting_reason{@POD@} == 1, "waiting", "$1", "reason", "(.*)"))', None),
]
rt_targets, rt_rename, rt_hide, rt_over = [], {"pod": "Pod", "container": "容器", "last_reason": "上次結束原因",
                                               "waiting": "目前等待原因"}, {"Time": True}, []
for i, (name, expr, props) in enumerate(restart_cols):
    ref = chr(65 + i)
    rt_targets.append({"refId": ref, "datasource": DS, "expr": q(expr), "legendFormat": "",
                       "range": False, "instant": True, "format": "table"})
    if props is None:
        rt_hide[f"Value #{ref}"] = True
    else:
        rt_rename[f"Value #{ref}"] = name
        rt_over.append({"matcher": {"id": "byName", "options": name}, "properties": props})
rt_order = ["pod", "container", "Value #A", "Value #B", "last_reason", "Value #D", "Value #E", "waiting"]
L.add({
    "id": nid(), "type": "table", "title": "【他】重啟紀錄（每個容器）",
    "description": desc(V_SELF,
                        "每個 Pod 的每個容器：時間範圍內重啟幾次、累計幾次、上次為什麼結束、exit code、多久前、目前是不是卡在等待。",
                        "**上次結束原因**：OOMKilled = 記憶體超過 limit；Error = 程式異常結束；Completed = 程式自己結束（常駐服務不該這樣）。"
                        "**exit code 137** 但不是 OOMKilled → 通常是 liveness probe 失敗被殺；143 → 收到終止訊號（重新部署、縮容）。"
                        "probe 失敗的細節不在 Prometheus，要看 `oc describe pod` 的 Events。沒資料的欄位 = kube-state-metrics 版本太舊沒有這個指標。"),
    "datasource": DS, "targets": rt_targets,
    "transformations": [
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {"excludeByName": rt_hide, "renameByName": rt_rename,
                                       "indexByName": {k: i for i, k in enumerate(rt_order)}}},
        {"id": "sortBy", "options": {"sort": [{"field": "時間範圍內重啟", "desc": True}]}},
    ],
    "fieldConfig": {"defaults": {"custom": {"align": "auto", "minWidth": 90}}, "overrides": rt_over},
    "options": {"showHeader": True},
}, 24, 7)
L.newline()

# ---------------------------------------------------------------- 3 shared expressions
HIS_CPU = f'sum by (namespace, pod) ({CPU_USE})'
HIS_MEM = f'sum by (namespace, pod) ({MEM_WS})'
HIS_TX = f'sum by (namespace, pod) ({net("transmit_bytes_total")})'
HIS_RX = f'sum by (namespace, pod) ({net("receive_bytes_total")})'
NODE_CPU_USED = 'sum by (instance) (rate(node_cpu_seconds_total{mode!~"idle|iowait", @NODE@}[@R@]))'
NODE_CPU_CORES = 'count by (instance) (node_cpu_seconds_total{mode="idle", @NODE@})'
NODE_MEM_USED = 'max by (instance) (node_memory_MemTotal_bytes{@NODE@} - node_memory_MemAvailable_bytes{@NODE@})'
NODE_MEM_TOTAL = 'max by (instance) (node_memory_MemTotal_bytes{@NODE@})'
NODE_TX = 'sum by (instance) (rate(node_network_transmit_bytes_total{@NODE@, @NIC@}[@R@]))'
NODE_RX = 'sum by (instance) (rate(node_network_receive_bytes_total{@NODE@, @NIC@}[@R@]))'
NODE_DISK_WRITE = 'sum by (instance) (rate(node_disk_written_bytes_total{@NODE@, @DEV@}[@R@]))'


def ratio(num, den):
    return f'{num} / on (node) {den}'


LIMIT_STEPS = steps((Y, 0.7), (RD, 0.9))
NODE_DISK_BUSY = f'max by (instance) (rate(node_disk_io_time_seconds_total{{@NODE@, @DEV@}}[@R@]))'
NODE_DISK_BYTES = ('sum by (instance) (rate(node_disk_read_bytes_total{@NODE@, @DEV@}[@R@])'
                   ' + rate(node_disk_written_bytes_total{@NODE@, @DEV@}[@R@]))')
NODE_DISK_IOPS = ('sum by (instance) (rate(node_disk_reads_completed_total{@NODE@, @DEV@}[@R@])'
                  ' + rate(node_disk_writes_completed_total{@NODE@, @DEV@}[@R@]))')
NODE_MEM_RATIO = 'max by (instance) (1 - node_memory_MemAvailable_bytes{@NODE@} / node_memory_MemTotal_bytes{@NODE@})'
NODE_FS_RATIO = (f'max by (instance) (1 - node_filesystem_avail_bytes{{@NODE@, {FS}}}'
                 f' / node_filesystem_size_bytes{{@NODE@, {FS}}})')
HIS_DISK_BYTES = (f'(({pod_io("write", POD)} + {pod_io("read", POD)})'
                  f' or {pod_io("write", POD)} or {pod_io("read", POD)})')
HIS_FS = f'sum by (namespace, pod) ({FS_USE})'
NO_NIC_SPEED = "無資料：Node 沒有回報網卡速度，請改看第 4 區網路吞吐與丟包"
VM_NIC = "容量是 Node 回報的網卡速度；虛擬機常固定回報 10 Gbps，實際可用頻寬可能更低，請搭配第 4 區「網路丟包」一起看。"


def peak_7d(expr):
    return f'max_over_time(({expr})[7d:5m])'


def peak_before(expr):
    """Peak over the 7 days before the start of the dashboard time range, evaluated once."""
    return f'max_over_time(({expr})[7d:5m] @ start())'


def fixed(expr):
    return expr.replace("@R@", "5m")


# ---------------------------------------------------------------- 3A his share of current usage
row("3A. 【他】÷ Node 目前用量：他是不是這台 Node 的大戶（⚠️ 不代表資源快滿）")
L = Line()
text(L, """**分母是 Node 目前的總用量，不是容量**：只回答「這台 Node 現在用掉的資源有幾成是他的」。

⚠️ 其他服務很閒或停掉時這裡一定接近 100%，**不代表資源快滿**；快不快滿看 3B、3D。""", h=2)
L.newline()
L = Line()
SHARE_HOW = "接近 100% 只代表這台 Node 目前的用量幾乎都是他的；其他服務停掉時一定會這樣。資源快不快滿請看 3B、3D。"
ts(L, "【他】CPU ÷ Node 目前 CPU 用量",
   [(ratio(his_by_node(HIS_CPU), as_node(NODE_CPU_USED)), "{{node}}")],
   "percentunit", desc(V_SHARE, "這台 Node 正在使用的 CPU，有幾成是他用的。", SHARE_HOW), w=6, minv=0)
ts(L, "【他】記憶體 ÷ Node 目前記憶體用量",
   [(ratio(his_by_node(HIS_MEM), as_node(NODE_MEM_USED)), "{{node}}")],
   "percentunit", desc(V_SHARE, "這台 Node 已被使用的記憶體，有幾成是他用的。", SHARE_HOW), w=6, minv=0)
ts(L, "【他】網路 ÷ Node 目前網卡流量",
   [(ratio(his_by_node(HIS_TX), as_node(NODE_TX)), "{{node}} 送出 TX"),
    (ratio(his_by_node(HIS_RX), as_node(NODE_RX)), "{{node}} 接收 RX")],
   "percentunit",
   desc(V_SHARE, "這台 Node 實體網卡目前的流量，有幾成是他的。",
        SHARE_HOW + "同一台 Node 內 Pod 互傳不經過實體網卡，所以可能超過 100%，只當量級參考。"),
   w=6, minv=0)
ts(L, "【他】磁碟寫入 ÷ Node 目前磁碟寫入量",
   [(ratio(his_by_node(pod_io("write", POD)), as_node(NODE_DISK_WRITE)), "{{node}}")],
   "percentunit",
   desc(V_SHARE, "這台 Node 本機磁碟目前的寫入量，有幾成是他寫的。",
        SHARE_HOW + "容器層與 Node 層的統計方式不同，寫到 PVC（網路儲存）的量也不算在 Node 本機磁碟，只當量級參考。"),
   w=6, minv=0, no_value=NO_POD_IO)
L.newline()

# ---------------------------------------------------------------- 3B his share of capacity
row("3B. 【他】÷ Node 容量：他自己用掉 Node 容量的幾成（主要看這區）")
L = Line()
text(L, """**分母是他所在 Node 的容量**（總核數、總記憶體、網卡速度、磁碟忙碌時間、檔案系統容量），每台 Node 一條線。

其他服務停掉也不會被灌高，這才是他用掉多少資源的真實比例；整台 Node 快不快滿看 3D。""", h=2)
L.newline()
L = Line()
HIS_CAP_HOW = "越高代表他一個人就吃掉這台 Node 越多容量；接近紅色虛線代表光是他就快把這項資源吃滿。"
ts(L, "【他】CPU ÷ Node 總核數",
   [(ratio(his_by_node(HIS_CPU), as_node(NODE_CPU_CORES)), "{{node}}")],
   "percentunit",
   desc(V_HIS_CAP, "他在這台 Node 上的 Pod，CPU 實際用量占總核數的幾成。",
        HIS_CAP_HOW + "他的 CPU 有 limit 保護，不會超過他的 limit 合計。"),
   minv=0, thresholds=LIMIT_STEPS)
ts(L, "【他】記憶體 ÷ Node 總記憶體",
   [(ratio(his_by_node(HIS_MEM), as_node(NODE_MEM_TOTAL)), "{{node}}")],
   "percentunit",
   desc(V_HIS_CAP, "他在這台 Node 上的 Pod，記憶體 working set 占總記憶體的幾成。",
        HIS_CAP_HOW + "他的記憶體有 limit 保護，不會超過他的 limit 合計。"),
   minv=0, thresholds=LIMIT_STEPS)
ts(L, "【他】網路 ÷ 網卡上限",
   [(ratio(his_by_node(HIS_TX), NET_CAP), "{{node}} 送出 TX"),
    (ratio(his_by_node(HIS_RX), NET_CAP), "{{node}} 接收 RX")],
   "percentunit",
   desc(V_HIS_CAP, "他的網路流量占網卡速度的幾成，送出與接收各自有上限，所以分開畫。", HIS_CAP_HOW + VM_NIC),
   minv=0, thresholds=LIMIT_STEPS, no_value=NO_NIC_SPEED)
ts(L, "【他】磁碟忙碌程度（估算）÷ 總時間",
   [('(' + ratio(his_by_node(HIS_DISK_BYTES), as_node(NODE_DISK_BYTES)) + ') * on (node) ' + as_node(NODE_DISK_BUSY),
     "{{node}}")],
   "percentunit",
   desc(V_HIS_CAP,
        "磁碟的讀寫速度與 IOPS 上限沒有指標會回報，所以用「磁碟忙碌時間 ÷ 總時間」代表磁碟容量。"
        "這是**估算值** = 整台磁碟忙碌率 × 他占目前磁碟讀寫量的比例。",
        HIS_CAP_HOW + "SSD 和雲端磁碟可同時處理多筆 IO，請搭配第 4 區「磁碟延遲」一起看。"),
   minv=0, thresholds=LIMIT_STEPS, no_value=NO_POD_IO)
ts(L, "【他】磁碟空間 ÷ 檔案系統容量",
   [(ratio(his_by_node(HIS_FS), FS_CAP), "{{node}}")],
   "percentunit",
   desc(V_HIS_CAP, "他的容器可寫層（暫存檔、容器內寫的檔案、log）占 Node 本機檔案系統容量的幾成。",
        "持續往上爬代表他一直在寫檔或 log，可能把 Node 磁碟寫滿，觸發 DiskPressure 並驅逐其他 Pod。"),
   minv=0, thresholds=steps((Y, 0.3), (RD, 0.5)))
L.newline()

# ---------------------------------------------------------------- 3C his usage vs his own 7-day peak
row("3C. 【他】目前 vs 他自己壓測前 7 天峰值：他比平常最忙時高多少")
L = Line()
text(L, """**實線 = 他現在，虛線 = 時間範圍開始前 7 天他自己的最高值**（Pod 合計、每 5 分鐘平均）。實線遠高於虛線 = 壓測讓他用到平常沒到過的量。

⚠️ 時間範圍的**起點要在壓測開始前**，虛線才不會被壓測本身墊高。""", h=2)
L.newline()
L = Line()
HIS_PEAK_HOW = "實線高出虛線越多，代表這次壓測讓他用得比平常最忙時多越多；再到 3D、3E 看整台 Node 是否因此變吃緊。"


def his_peak_panel(title, his_expr, unit, what, scale=""):
    total = f'sum({his_expr})'
    ts(L, title,
       [(scale + total, "他 目前"),
        (scale + peak_before(fixed(total)), "他 壓測前 7 天峰值")],
       unit, desc(V_HIS_PEAK, what, HIS_PEAK_HOW), minv=0, overrides=PEAK_DASHED)


his_peak_panel("【他】CPU 用量（核）", HIS_CPU, "none", "他所有 Pod 合計的 CPU 用量（核）。")
his_peak_panel("【他】記憶體用量", HIS_MEM, "bytes", "他所有 Pod 合計的記憶體 working set。")
his_peak_panel("【他】網路送出 TX", HIS_TX, "bps", "他所有 Pod 合計的送出流量（bits/s）。", scale="8 * ")
his_peak_panel("【他】網路接收 RX", HIS_RX, "bps", "他所有 Pod 合計的接收流量（bits/s）。", scale="8 * ")
his_peak_panel("【他】磁碟讀寫量", HIS_DISK_BYTES, "Bps",
               "他所有 Pod 合計的磁碟讀寫量。沒資料代表叢集沒有收集 Pod 層級的磁碟 IO。")
his_peak_panel("【他】容器可寫層用量", HIS_FS, "bytes", "他所有容器在 Node 本機磁碟上佔用的空間。")
L.newline()

# ---------------------------------------------------------------- 3D whole node share of capacity
row("3D. 【整台 Node】÷ 容量：他所在的 Node 快不快滿（所有 Pod 加總，不是他的用量）")
L = Line()
NODE_CAP_HOW = "接近紅色虛線代表這項資源快滿，同 Node 所有 Pod 都會受影響。和 3B 對照：3B 他的比例越接近這裡，代表用量主要來自他。"
ts(L, "【整台 Node】CPU ÷ 總核數",
   [(ratio(as_node(NODE_CPU_USED), as_node(NODE_CPU_CORES)), "{{node}}")],
   "percentunit",
   desc(V_NODE_CAP, "整台 Node 實際在用的 CPU 占總核數的幾成（不含閒置與 iowait）。",
        NODE_CAP_HOW + "CPU 快滿時，超用 request 的 Pod 會互相搶 CPU。"),
   minv=0, thresholds=LIMIT_STEPS)
ts(L, "【整台 Node】記憶體 ÷ 總記憶體",
   [(as_node(NODE_MEM_RATIO), "{{node}}")],
   "percentunit",
   desc(V_NODE_CAP, "整台 Node 已使用的記憶體（總記憶體 − 可用記憶體，含系統程式）占總記憶體的幾成。",
        NODE_CAP_HOW + "太高時 kubelet 可能開始驅逐 Pod（MemoryPressure）。"),
   minv=0, thresholds=LIMIT_STEPS)
ts(L, "【整台 Node】網路 ÷ 網卡上限",
   [(ratio(as_node(NODE_TX), NET_CAP), "{{node}} 送出 TX"),
    (ratio(as_node(NODE_RX), NET_CAP), "{{node}} 接收 RX")],
   "percentunit",
   desc(V_NODE_CAP, "整台 Node 實體網卡的流量占網卡速度的幾成，送出與接收各自有上限，所以分開畫。", NODE_CAP_HOW + VM_NIC),
   minv=0, thresholds=LIMIT_STEPS, no_value=NO_NIC_SPEED)
ts(L, "【整台 Node】磁碟忙碌程度 ÷ 總時間",
   [(as_node(NODE_DISK_BUSY), "{{node}}")],
   "percentunit",
   desc(V_NODE_CAP, "Node 上最忙的那顆本機磁碟，有在處理 IO 的時間占總時間的幾成。",
        NODE_CAP_HOW + "SSD 和雲端磁碟可同時處理多筆 IO，到 100% 不一定真的慢：忙碌率高且第 4 區磁碟延遲上升，才是真的滿了。"),
   minv=0, thresholds=LIMIT_STEPS)
ts(L, "【整台 Node】磁碟空間 ÷ 檔案系統容量",
   [(as_node(NODE_FS_RATIO), "{{node}}")],
   "percentunit",
   desc(V_NODE_CAP, "Node 本機檔案系統用了幾成，容器映像、容器可寫層和 log 都放在這裡。",
        "超過約 85–90% 時，kubelet 會進入 DiskPressure 並開始驅逐 Pod。"),
   minv=0, thresholds=steps((Y, 0.8), (RD, 0.85)))
L.newline()

# ---------------------------------------------------------------- 3E whole node vs its 7-day peak
row("3E. 【整台 Node】目前 vs 整台壓測前 7 天峰值：Node 有沒有比平常最忙時還忙")
L = Line()
text(L, """**實線 = 整台 Node 現在的用量，虛線 = 時間範圍開始前 7 天內，這台 Node 的最高值**（每 5 分鐘平均），每台 Node 各一組。實線超過同一台 Node 的虛線，代表這台 Node 正在承受平常沒遇過的壓力；在虛線以下，通常還好。""", h=2)
L.newline()
L = Line()
NODE_PEAK_HOW = "實線超過同一台 Node 的虛線，代表比過去一週最忙時還忙；再對照 3C 看他是不是同時也超出平常。"


def node_peak_panel(title, node_expr, unit, what, scale=""):
    ts(L, title,
       [(scale + as_node(node_expr), "{{node}} 目前"),
        (scale + as_node(peak_before(fixed(node_expr))), "{{node}} 壓測前 7 天峰值")],
       unit, desc(V_NODE_PEAK, what, NODE_PEAK_HOW), minv=0, overrides=PEAK_DASHED)


node_peak_panel("【整台 Node】CPU 用量（核）", NODE_CPU_USED, "none", "使用中的 CPU 核數（不含閒置與 iowait）。")
node_peak_panel("【整台 Node】記憶體用量", NODE_MEM_USED, "bytes", "已使用的記憶體（總記憶體 − 可用記憶體）。")
node_peak_panel("【整台 Node】網路送出 TX", NODE_TX, "bps", "實體網卡送出方向的流量（bits/s）。", scale="8 * ")
node_peak_panel("【整台 Node】網路接收 RX", NODE_RX, "bps", "實體網卡接收方向的流量（bits/s）。", scale="8 * ")
node_peak_panel("【整台 Node】磁碟吞吐（讀 + 寫）", NODE_DISK_BYTES, "Bps", "Node 本機磁碟每秒讀寫量。")
node_peak_panel("【整台 Node】磁碟 IOPS", NODE_DISK_IOPS, "iops", "Node 本機磁碟每秒讀寫次數。")
L.newline()
L = Line()
NIC_BITS = "8 * " + 'max by (instance) (node_network_speed_bytes{@NODE@, @NIC@})'
multi_table(L, "【整台 Node】參考：網卡速度與最近 7 天峰值", "node", [
    ("網卡回報速度", as_node(NIC_BITS), "bps", 0),
    ("7 天網路送出峰值", as_node(peak_7d("8 * " + fixed(NODE_TX))), "bps", 1),
    ("7 天網路接收峰值", as_node(peak_7d("8 * " + fixed(NODE_RX))), "bps", 1),
    ("7 天磁碟吞吐峰值", as_node(peak_7d(fixed(NODE_DISK_BYTES))), "Bps", 1),
    ("7 天磁碟 IOPS 峰值", as_node(peak_7d(fixed(NODE_DISK_IOPS))), "iops", 0),
], desc("所選的每台 Node（整台加總）",
        "Node 自己回報的網卡速度，以及到時間範圍結束為止、最近 7 天實際跑到的最高網路、磁碟流量（每 5 分鐘平均）。",
        "峰值只代表「至少撐得住這麼多」，不是上限：峰值那段時間的磁碟延遲與網路丟包都正常，就代表上限更高；"
        "延遲已明顯上升，則這附近就是實際可用上限。網卡速度遠高於峰值，代表網卡還有很多餘裕。"),
    h=6)
L.newline()

# ---------------------------------------------------------------- 4 node
row("4. 【整台 Node】共用資源壓力：所有 Pod 加總，不是他的用量，看壓測期間有沒有往上飆")
L = Line()
text(L, """**整台 Node 的狀態（所有 Pod 加總），不是他的用量。** 比較壓測前與壓測中：

壓測一開始就往上飆 → 對照第 2 區【他】和第 5 區【鄰居】判斷是不是他造成的；前後都很平 → Node 沒被拖慢。""", w=24, h=2)
L.newline()
L = Line()
ts(L, "【整台 Node】PSI：有多少時間有工作在等資源",
   [('rate(node_pressure_cpu_waiting_seconds_total{@NODE@}[@R@])', "{{instance}} CPU"),
    ('rate(node_pressure_io_waiting_seconds_total{@NODE@}[@R@])', "{{instance}} IO"),
    ('rate(node_pressure_memory_waiting_seconds_total{@NODE@}[@R@])', "{{instance}} 記憶體")],
   "percentunit",
   desc(V_NODE,
        "PSI（Pressure Stall Information）是 Linux 核心記錄的「有工作因為搶不到 CPU / 磁碟 IO / 記憶體而在等」的時間比例。比 CPU 使用率更直接反映「大家有沒有變慢」。",
        "壓測前後比較，往上飆就是 Node 開始吃緊。粗略參考：CPU 或 IO 持續超過 10% 值得注意。**沒有資料**代表 Node 核心沒開 PSI（RHCOS 預設可能關閉），請改看右邊的替代面板。"),
   minv=0, no_value="無資料：Node 核心未開啟 PSI，請看右邊「PSI 替代」面板")
ts(L, "【整台 Node】PSI 替代：iowait 與每核負載",
   [('avg by (instance) (rate(node_cpu_seconds_total{mode="iowait", @NODE@}[@R@]))', "{{instance}} iowait"),
    ('node_load1{@NODE@} / on (instance) count by (instance) (node_cpu_seconds_total{mode="idle", @NODE@})', "{{instance}} 每核負載")],
   "percentunit",
   desc(V_NODE,
        "iowait：CPU 閒著在等磁碟的時間比例。每核負載：排隊等 CPU 或 IO 的工作數 ÷ CPU 核數（100% 約等於每核一個工作）。",
        "PSI 沒資料時用這個。iowait 往上飆代表磁碟變慢；每核負載長時間超過 100% 代表工作在排隊。"),
   minv=0)
ts(L, "【整台 Node】磁碟延遲（每次 IO 平均花多久）",
   [('(rate(node_disk_read_time_seconds_total{@NODE@, @DEV@}[@R@]) + rate(node_disk_write_time_seconds_total{@NODE@, @DEV@}[@R@])) / (rate(node_disk_reads_completed_total{@NODE@, @DEV@}[@R@]) + rate(node_disk_writes_completed_total{@NODE@, @DEV@}[@R@]))', "{{instance}} {{device}}")],
   "s",
   desc(V_NODE,
        "Node 本機磁碟每次讀寫平均要等多久，所有 Pod 共用這顆磁碟。**判斷磁碟有沒有拖慢大家，這個最準。**",
        "壓測中明顯往上飆就是磁碟變慢，同 Node 所有 Pod 都會受影響。粗略參考：SSD 通常數 ms 以內，超過 10–20 ms 就很慢。"),
   minv=0)
ts(L, "【整台 Node】磁碟忙碌程度（÷ 總時間）",
   [('rate(node_disk_io_time_seconds_total{@NODE@, @DEV@}[@R@])', "{{instance}} {{device}}")],
   "percentunit",
   desc(V_NODE, "磁碟有在處理 IO 的時間比例（類似 iostat 的 %util）。",
        "接近 100% 可能飽和，但 SSD / 雲端磁碟可同時處理多筆 IO，100% 不一定代表慢，要搭配左邊的磁碟延遲一起看。"),
   minv=0)
ts(L, "【整台 Node】磁碟每秒讀寫次數（IOPS）",
   [('rate(node_disk_reads_completed_total{@NODE@, @DEV@}[@R@])', "{{instance}} {{device}} 讀"),
    ('rate(node_disk_writes_completed_total{@NODE@, @DEV@}[@R@])', "{{instance}} {{device}} 寫")],
   "iops",
   desc(V_NODE, "Node 本機磁碟每秒完成的讀寫次數。",
        "IOPS 上升而延遲沒變，代表磁碟還撐得住；IOPS 上不去但延遲飆升，代表已經到磁碟上限。"),
   minv=0)
ts(L, "【整台 Node】檔案系統使用率（÷ 容量）",
   [('1 - node_filesystem_avail_bytes{@NODE@, mountpoint=~"/sysroot|/var|/", fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes{@NODE@, mountpoint=~"/sysroot|/var|/", fstype!~"tmpfs|overlay|squashfs"}', "{{instance}} {{mountpoint}}")],
   "percentunit",
   desc(V_NODE, "Node 本機磁碟空間用了幾成，容器映像、容器可寫層和 log 都放在這裡。",
        "超過 85% 要注意；剩餘空間低於約 10–15% 時 kubelet 會開始驅逐 Pod（DiskPressure）。"),
   minv=0)
ts(L, "【整台 Node】網路吞吐",
   [('sum by (instance) (rate(node_network_transmit_bytes_total{@NODE@, @NIC@}[@R@]))', "{{instance}} 送出 TX"),
    ('sum by (instance) (rate(node_network_receive_bytes_total{@NODE@, @NIC@}[@R@]))', "{{instance}} 接收 RX")],
   "Bps",
   desc(V_NODE, "Node 實體網卡的總流量，所有 Pod 共用這張網卡。",
        "和第 2 區【他】的網路流量對照，看 Node 的流量上升是不是主要來自他。接近網卡頻寬上限時，所有 Pod 都會變慢。"),
   minv=0)
ts(L, "【整台 Node】網路丟包 / 錯誤",
   [('sum by (instance) (rate(node_network_receive_drop_total{@NODE@, @NIC@}[@R@]) + rate(node_network_transmit_drop_total{@NODE@, @NIC@}[@R@]))', "{{instance}} 丟包"),
    ('sum by (instance) (rate(node_network_receive_errs_total{@NODE@, @NIC@}[@R@]) + rate(node_network_transmit_errs_total{@NODE@, @NIC@}[@R@]))', "{{instance}} 錯誤")],
   "pps",
   desc(V_NODE, "Node 實體網卡每秒丟掉或出錯的封包數。丟包通常是網卡或核心的緩衝區塞滿處理不及。",
        "正常應接近 0。壓測期間開始出現並往上升，代表網卡塞住，同 Node 所有 Pod 的網路都會受影響（重傳、延遲變高）。"),
   minv=0)
ts(L, "【整台 Node】網路封包處理 CPU（softirq，÷ 總 CPU 時間）",
   [('avg by (instance) (rate(node_cpu_seconds_total{mode="softirq", @NODE@}[@R@]))', "{{instance}}")],
   "percentunit",
   desc(V_NODE,
        "Node 核心處理網路封包（以及部分磁碟完成通知）所花的 CPU 時間比例。這部分 CPU **不會算在 Pod 的 CPU limit 裡**，"
        "所以他的 CPU 被限制住，大流量還是會吃掉整台 Node 的 CPU。",
        "壓測時跟著網路流量一起往上飆，代表大流量正在消耗 Node 的 CPU。粗略參考：持續超過 10% 值得注意，"
        "若集中在少數 CPU 核上，該核會先滿而讓網路變慢。"),
   minv=0)
ts(L, "【整台 Node】conntrack 使用率（÷ 表格上限）",
   [('node_nf_conntrack_entries{@NODE@} / node_nf_conntrack_entries_limit{@NODE@}', "{{instance}}")],
   "percentunit",
   desc(V_NODE,
        "Node 核心記錄每條網路連線的表格，所有 Pod 共用一張，有容量上限。大量短連線（例如每次都重新連 DB / Redis / Kafka）最容易吃滿。",
        "超過 70% 要注意；滿了之後新連線會被直接丟掉，同 Node 所有 Pod 都會連不出去。"),
   w=6, minv=0)
ts(L, "【整台 Node】CPU 使用率（÷ 總核數）",
   [('1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle", @NODE@}[@R@]))', "{{instance}}")],
   "percentunit",
   desc(V_NODE, "整台 Node 的 CPU 有多少時間在工作。",
        "長時間超過 80% 且 PSI CPU 也上升時，沒設 limit 或超用 request 的 Pod 會互相搶 CPU。"),
   w=6, minv=0)
ts(L, "【整台 Node】記憶體使用率（÷ 總記憶體）",
   [('1 - node_memory_MemAvailable_bytes{@NODE@} / node_memory_MemTotal_bytes{@NODE@}', "{{instance}}")],
   "percentunit",
   desc(V_NODE, "整台 Node 已用掉的記憶體比例（總記憶體 − 可用記憶體）。",
        "超過 90% 要注意，kubelet 可能開始驅逐 Pod（MemoryPressure）。"),
   w=6, minv=0)
ts(L, "【整台 Node】Node 壓力狀態（1 = 發生中）",
   [('max by (node, condition) (kube_node_status_condition{condition=~"DiskPressure|MemoryPressure|PIDPressure", status="true", node=~"$node"})', "{{node}} {{condition}}")],
   "none",
   desc(V_NODE, "kubelet 回報 Node 進入磁碟、記憶體或程序數不足的狀態。",
        "出現 1 代表 Node 已經嚴重吃緊，kubelet 會開始驅逐 Pod，影響同 Node 的所有服務。"),
   w=6, minv=0)
L.newline()

# ---------------------------------------------------------------- 5 neighbours
row("5. 【鄰居】所選 Node 上用量前 10 名（含他自己）")
L = Line()
JOIN = ' * on (namespace, pod) group_left () max by (namespace, pod) (kube_pod_info{node=~"$node"})'
NB_HOW = "第 4 區往上飆的時候，看是誰同時上升。如果前幾名都是他，加上 Node 壓力同時上升，就是他造成的；如果是別人，就跟他無關。"
ts(L, "【鄰居】CPU Top 10",
   [('topk(10, sum by (namespace, pod) (' + one('rate(container_cpu_usage_seconds_total{container!="", container!="POD"}[@R@])')
     + ')' + JOIN + ')', "{{namespace}}/{{pod}}")],
   "none", desc(V_NEIGHBOR, "所選 Node 上 CPU 用量最多的 10 個 Pod。", NB_HOW), w=12, h=8, minv=0)
ts(L, "【鄰居】記憶體 Top 10",
   [('topk(10, sum by (namespace, pod) (' + one('container_memory_working_set_bytes{container!="", container!="POD"}')
     + ')' + JOIN + ')', "{{namespace}}/{{pod}}")],
   "bytes", desc(V_NEIGHBOR, "所選 Node 上記憶體用量最多的 10 個 Pod。", NB_HOW), w=12, h=8, minv=0)
ts(L, "【鄰居】磁碟寫入 Top 10",
   [('topk(10, ' + pod_io("write") + JOIN + ')', "{{namespace}}/{{pod}}")],
   "Bps", desc(V_NEIGHBOR, "所選 Node 上寫磁碟最多的 10 個 Pod。", NB_HOW), w=12, h=8, minv=0,
   no_value=NO_POD_IO)
ts(L, "【鄰居】網路送出 Top 10",
   [('topk(10, sum by (namespace, pod) (' + net("transmit_bytes_total", 'pod!=""') + ')' + JOIN + ')', "{{namespace}}/{{pod}}")],
   "Bps", desc(V_NEIGHBOR, "所選 Node 上網路送出最多的 10 個 Pod。", NB_HOW), w=12, h=8, minv=0)
L.newline()

# ---------------------------------------------------------------- 6 ingress / shared
row("6. 【共用】入口與 DNS（全叢集共用，不分 Node）")
L = Line()
ts(L, "【共用】Router 回應數（依狀態碼，此 namespace）",
   [('sum by (code) (rate(haproxy_backend_http_responses_total{exported_namespace="$namespace"}[@R@]))', "{{code}}")],
   "reqps",
   desc(V_SHARED, "經過 OCP Router 進到這個 namespace 的請求數，依 HTTP 狀態碼分。",
        "5xx 上升代表後端出問題。沒資料代表此服務不經過 Router，或沒有權限查詢 Router 指標。"),
   minv=0)
ts(L, "【共用】Router 後端排隊（此 namespace）",
   [('sum by (route) (haproxy_backend_current_queue{exported_namespace="$namespace"})', "{{route}}")],
   "none",
   desc(V_SHARED, "請求在 Router 等待後端 Pod 接手的數量。",
        "大於 0 代表後端 Pod 處理不及，請求在入口排隊。"),
   minv=0)
ts(L, "【共用】CoreDNS 請求量與 P99 延遲",
   [('sum(rate(coredns_dns_requests_total[@R@]))', "每秒查詢數"),
    ('histogram_quantile(0.99, sum by (le) (rate(coredns_dns_request_duration_seconds_bucket[@R@])))', "P99 延遲（秒）")],
   "short",
   desc(V_SHARED, "全叢集 DNS 服務的查詢量（左軸）與最慢 1% 查詢的延遲（右軸）。",
        "壓測時若每次連線都重新查 DNS，查詢量會飆高；P99 延遲上升時，全叢集的服務解析名稱都會變慢。"),
   minv=0, overrides=[{"matcher": {"id": "byName", "options": "P99 延遲（秒）"},
                       "properties": [{"id": "unit", "value": "s"}, {"id": "custom.axisPlacement", "value": "right"}]}])
L.newline()


# ---------------------------------------------------------------- dashboard
def qvar(name, label, query, multi=False, include_all=False):
    return {"type": "query", "name": name, "label": label, "datasource": DS,
            "query": query, "definition": query, "refresh": 2, "sort": 1,
            "multi": multi, "includeAll": include_all, "hide": 0, "current": {}}


def textbox(name, label, default="0", hide=0):
    return {"type": "textbox", "name": name, "label": label, "query": default,
            "current": {"text": default, "value": default}, "hide": hide}


dash = {
    "uid": "ocp-loadtest-impact",
    "title": "OCP 壓測影響評估（Workload vs Node 共用資源）",
    "tags": ["ocp", "loadtest", "capacity"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 9,
    "editable": True,
    "graphTooltip": 1,
    "refresh": "30s",
    "time": {"from": "now-3h", "to": "now"},
    "links": [{"title": "回到叢集風險總覽", "type": "link", "icon": "dashboard",
               "url": "/d/ocp-cluster-risk-overview/?var-datasource=${datasource}", "keepTime": True,
               "targetBlank": False}],
    "templating": {"list": [
        {"type": "datasource", "name": "datasource", "label": "資料來源",
         "query": "prometheus", "regex": "/^Prometheus.*OCP$/",
         "current": {}, "hide": 0, "refresh": 1},
        qvar("namespace", "Namespace", "label_values(kube_pod_info, namespace)"),
        qvar("workload", "Deployment",
             'label_values(kube_deployment_spec_replicas{namespace="$namespace"}, deployment)'),
        qvar("node", "Node（自動帶出他所在的 Node）",
             'label_values(kube_pod_info{namespace="$namespace", pod=~"$workload-[a-z0-9]+-[a-z0-9]+"}, node)',
             multi=True, include_all=True),
        textbox("nic_mbps", "網卡上限 Mbps（0 = 用 Node 回報值）", hide=2),
    ]},
    "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                              "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                              "name": "Annotations & Alerts", "type": "dashboard"}]},
    "panels": panels,
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(dash, f, ensure_ascii=False, indent=2)
print("panels:", len(panels))
