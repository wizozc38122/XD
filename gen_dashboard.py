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


def pod_io(op, sel=""):
    """Per-pod disk bytes/s. Falls back through the metrics clusters commonly keep:
    container-level cAdvisor fs stats, then pod-level fs stats, then pod-level blkio."""
    fs, blk = ("writes", "Write") if op == "write" else ("reads", "Read")
    s = sel + ", " if sel else ""
    return (f'(sum by (namespace, pod) (rate(container_fs_{fs}_bytes_total{{{s}container!="", container!="POD"}}[@R@]))'
            f' or sum by (namespace, pod) (rate(container_fs_{fs}_bytes_total{{{s}container="", pod!=""}}[@R@]))'
            f' or sum by (namespace, pod) (rate(container_blkio_device_usage_total{{{s}operation="{blk}", container="", pod!=""}}[@R@])))')


NO_POD_IO = "無資料：叢集沒有收集 Pod 層級的磁碟 IO 指標，請改看第 4 區整台 Node 的磁碟面板"


def q(s):
    return (s.replace("@POD@", POD).replace("@CT@", CT).replace("@R@", R)
            .replace("@DEV@", DEV).replace("@NIC@", NIC).replace("@NODE@", NODE)
            .replace("@ON_NODE@", ON_NODE))


def desc(view, what, how):
    return f"**視角**：{view}\n\n**這是什麼**：{what}\n\n**怎麼看**：{how}"


V_SELF = "他自己（這個 Deployment 的 Pod 合計）"
V_SHARE = "他 ÷ 所選 Node 的總量（只算跑在所選 Node 上的 Pod）"
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


def ts(line, title, items, unit, description, w=8, h=7, minv=None, no_value=None):
    custom = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 12,
              "showPoints": "never", "spanNulls": True}
    defaults = {"unit": unit, "custom": custom}
    if minv is not None:
        defaults["min"] = minv
    if no_value:
        defaults["noValue"] = no_value
    line.add({
        "id": nid(), "type": "timeseries", "title": title, "description": description,
        "datasource": DS, "targets": targets(items),
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }, w, h)


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
text(L, """### 這個 Dashboard 怎麼用
**目的**：壓測期間，判斷 `$workload`（namespace `$namespace`）是否拖慢**同一台 Node** 上的其他服務。重點是 CPU / 記憶體 limit **管不到**的共用資源：網路、磁碟 IO、conntrack。

**面板標題前綴代表「在看誰」，這點很重要：**

| 前綴 | 看的是 | 用來回答 |
|---|---|---|
| 【他】 | 他自己的 Pod 合計 | 他用了多少、有沒有被自己的 limit 卡住 |
| 【占比】 | 他 ÷ 所在 Node 總量 | 他在這台 Node 上算不算大戶 |
| 【整台 Node】 | 整台 Node 所有 Pod 加總，**不是他的用量** | 這台機器有沒有變吃緊（看壓測期間有沒有往上飆） |
| 【鄰居】 | 同 Node 上所有 Pod 排名 | 誰是大戶、鄰居有沒有跟著變化 |
| 【共用】 | 全叢集共用元件（Router、DNS） | 入口與 DNS 有沒有被打高 |

**判讀**：只有「他的用量上升」**且**「整台 Node 的壓力指標在同一時間往上飆」**且**「鄰居也受影響」三者時間對得上，才算他影響了整體。整台 Node 指標本身沒辦法告訴你是誰造成的，要搭配【他】和【鄰居】一起看。

**建議操作**：選 namespace 與 Deployment → `Node` 會自動帶出他所在的 Node → 時間範圍框住壓測**前後**（要有壓測前的基準才看得出飆升）。每個面板右上角的 ⓘ 有說明。""", w=24, h=11)
L.newline()

# ---------------------------------------------------------------- 1 summary
row("1. 總覽：他自己有沒有被限制卡住、部署在哪")
L = Line()
stat(L, "【他】可用副本數",
     'kube_deployment_status_replicas_available{namespace="$namespace", deployment="$workload"}',
     "none", steps(),
     desc(V_SELF, "目前 Ready 的 Pod 數量。", "壓測中如果 HPA 擴容，副本數會上升，Pod 也可能被排到新的 Node。"),
     decimals=0)
stat(L, "【他】CPU 用量 / Request",
     'sum(rate(container_cpu_usage_seconds_total{@CT@}[@R@])) / sum(kube_pod_container_resource_requests{resource="cpu", @POD@})',
     "percentunit", steps((Y, 1.0), (RD, 1.5)),
     desc(V_SELF, "實際 CPU 用量占「保證額度（request）」的比例。",
          "100% 以下：在自己的保證額度內。超過 100%：正在借用 Node 的閒置 CPU，Node 吃緊時最容易和鄰居互相搶。"))
stat(L, "【他】記憶體 / Limit（最高的容器）",
     'max(container_memory_working_set_bytes{@CT@} / on(namespace, pod, container) kube_pod_container_resource_limits{resource="memory", @POD@})',
     "percentunit", steps((Y, 0.8), (RD, 0.9)),
     desc(V_SELF, "記憶體用量最接近上限的那個容器，用量占 limit 的比例。",
          "超過 90% 要注意，到 100% 會被 OOMKilled。沒設 memory limit 則沒有資料。"))
stat(L, "【他】CPU throttling（最高的 Pod）",
     'max(sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{@CT@}[@R@])) / sum by (pod) (rate(container_cpu_cfs_periods_total{@CT@}[@R@])))',
     "percentunit", steps((Y, 0.25), (RD, 0.5)),
     desc(V_SELF, "CPU 想用但被 limit 擋下的時間比例。",
          "超過 25% 通常已讓他自己變慢。被節流**只影響他自己**，不會拖累鄰居。"))
stat(L, "【他】近 1 小時重啟次數",
     'sum(increase(kube_pod_container_status_restarts_total{@POD@}[1h]))',
     "none", steps((RD, 1)),
     desc(V_SELF, "所有 Pod 近 1 小時重啟次數合計。", "大於 0 要看下方的 OOMKilled 面板，或檢查 probe 是否失敗。"),
     decimals=0)
stat(L, "【占比】他占所選 Node 的 CPU",
     'sum(rate(container_cpu_usage_seconds_total{@CT@}[@R@])@ON_NODE@) / sum(rate(node_cpu_seconds_total{mode!~"idle|iowait", @NODE@}[@R@]))',
     "percentunit", steps((Y, 0.3), (RD, 0.6)),
     desc(V_SHARE, "這些 Node 上正在使用的 CPU，有多少是他用的。",
          "占比高只代表他是大戶，**不代表造成影響**；要看第 4 區整台 Node 是否同時變吃緊。"))
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
   [('sum(rate(container_cpu_usage_seconds_total{@CT@}[@R@]))', "用量"),
    ('sum(kube_pod_container_resource_requests{resource="cpu", @POD@})', "request 合計"),
    ('sum(kube_pod_container_resource_limits{resource="cpu", @POD@})', "limit 合計"),
    ('sum(rate(container_cpu_usage_seconds_total{@CT@}[@R@] offset 1d))', "昨天同時段")],
   "none",
   desc(V_SELF, "他的 Pod 合計 CPU 核數，對照 request、limit 與昨天同一時間。",
        "用量遠高於昨天同時段，代表這次壓測確實加重了負載；貼近 limit 線會開始被 throttling。"),
   w=12)
ts(L, "【他】記憶體：working set vs Request / Limit",
   [('sum(container_memory_working_set_bytes{@CT@})', "working set"),
    ('sum(kube_pod_container_resource_requests{resource="memory", @POD@})', "request 合計"),
    ('sum(kube_pod_container_resource_limits{resource="memory", @POD@})', "limit 合計")],
   "bytes",
   desc(V_SELF, "working set 是 OOM 判斷依據的記憶體用量。",
        "持續上升不回落可能是請求堆積或洩漏；碰到 limit 會被 OOMKilled。"),
   w=12)
ts(L, "【他】CPU throttling 比例（各 Pod）",
   [('sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{@CT@}[@R@])) / sum by (pod) (rate(container_cpu_cfs_periods_total{@CT@}[@R@]))', "{{pod}}")],
   "percentunit",
   desc(V_SELF, "每個 Pod 的 CPU 被 limit 擋下的時間比例。",
        "超過 25% 代表他自己變慢，可能引發 timeout 與重試，進而放大對外部服務的流量。"),
   minv=0)
ts(L, "【他】網路流量",
   [('sum(rate(container_network_transmit_bytes_total{@POD@}[@R@]))', "送出 TX"),
    ('sum(rate(container_network_receive_bytes_total{@POD@}[@R@]))', "接收 RX")],
   "Bps",
   desc(V_SELF, "他的 Pod 合計網路流量（Bytes/s）。",
        "和第 4 區「整台 Node 網路吞吐」對照，看 Node 的流量上升是不是主要來自他。"))
ts(L, "【他】網路丟包 / 錯誤",
   [('sum(rate(container_network_transmit_packets_dropped_total{@POD@}[@R@]))', "TX 丟包"),
    ('sum(rate(container_network_receive_packets_dropped_total{@POD@}[@R@]))', "RX 丟包"),
    ('sum(rate(container_network_transmit_errors_total{@POD@}[@R@]))', "TX 錯誤"),
    ('sum(rate(container_network_receive_errors_total{@POD@}[@R@]))', "RX 錯誤")],
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
   no_value=NO_POD_IO)
ts(L, "【他】容器可寫層 / 暫存空間用量",
   [('sum(container_fs_usage_bytes{@CT@})', "已使用")],
   "bytes",
   desc(V_SELF, "他的容器在 Node 本機磁碟上佔用的空間（可寫層、emptyDir 等）。",
        "持續成長代表一直在寫檔或 log，可能把 Node 磁碟寫滿，觸發 DiskPressure 並驅逐其他 Pod。"))
ts(L, "【他】重啟與 OOMKilled",
   [('sum by (pod) (increase(kube_pod_container_status_restarts_total{@POD@}[5m]))', "重啟 {{pod}}"),
    ('sum(kube_pod_container_status_last_terminated_reason{@POD@, reason="OOMKilled"})', "上次因 OOMKilled 結束的容器數")],
   "none",
   desc(V_SELF, "每 5 分鐘內的重啟次數，以及上次是因為記憶體超過 limit 被殺的容器數。",
        "有 OOMKilled 代表 memory limit 不夠或有記憶體堆積。"),
   minv=0)
L.newline()

# ---------------------------------------------------------------- 3 share
row("3. 【占比】他 ÷ 所在 Node 總量（他在這台算不算大戶）")
L = Line()
ts(L, "【占比】CPU",
   [('sum(rate(container_cpu_usage_seconds_total{@CT@}[@R@])@ON_NODE@) / sum(rate(node_cpu_seconds_total{mode!~"idle|iowait", @NODE@}[@R@]))', "占這些 Node 正在用的 CPU"),
    ('sum(rate(container_cpu_usage_seconds_total{@CT@}[@R@])@ON_NODE@) / sum(kube_node_status_allocatable{resource="cpu", node=~"$node"})', "占這些 Node 的可分配 CPU")],
   "percentunit",
   desc(V_SHARE, "第一條：Node 上正在被使用的 CPU 有多少是他的。第二條：他用掉這些 Node 總容量的幾成。",
        "占比高代表他是大戶，但影響與否要看第 4 區是否同時變吃緊。"),
   w=6, minv=0)
ts(L, "【占比】網路",
   [('sum(rate(container_network_transmit_bytes_total{@POD@}[@R@])@ON_NODE@) / sum(rate(node_network_transmit_bytes_total{@NODE@, @NIC@}[@R@]))', "TX 占 Node 網卡"),
    ('sum(rate(container_network_receive_bytes_total{@POD@}[@R@])@ON_NODE@) / sum(rate(node_network_receive_bytes_total{@NODE@, @NIC@}[@R@]))', "RX 占 Node 網卡")],
   "percentunit",
   desc(V_SHARE, "他的網路流量占 Node 實體網卡流量的比例。",
        "同一台 Node 內 Pod 互傳不經過實體網卡，所以可能超過 100%，只當量級參考。網卡名稱不是 ens/eth/eno/enp/bond 開頭時需修改查詢。"),
   w=6, minv=0)
ts(L, "【占比】磁碟寫入",
   [('sum(' + pod_io("write", POD) + '@ON_NODE@) / sum(rate(node_disk_written_bytes_total{@NODE@, @DEV@}[@R@]))', "寫入占 Node 磁碟")],
   "percentunit",
   desc(V_SHARE, "他的磁碟寫入量占 Node 本機磁碟總寫入量的比例。",
        "容器層與 Node 層的統計方式不同，只當量級參考。寫到 PVC（網路儲存）的量不算在 Node 本機磁碟。"),
   w=6, minv=0, no_value=NO_POD_IO)
ts(L, "【占比】記憶體",
   [('sum(container_memory_working_set_bytes{@CT@}@ON_NODE@) / sum(node_memory_MemTotal_bytes{@NODE@})', "占 Node 總記憶體")],
   "percentunit",
   desc(V_SHARE, "他的記憶體用量占 Node 總記憶體的比例。", "記憶體有 limit 保護，通常只需確認不要占太大。"),
   w=6, minv=0)
L.newline()

# ---------------------------------------------------------------- 4 node
row("4. 【整台 Node】共用資源壓力：所有 Pod 加總，不是他的用量，看壓測期間有沒有往上飆")
L = Line()
text(L, """**這一區看的是整台 Node 的狀態（所有 Pod 加總），不是他自己用了多少。** 用途是確認：他所在的 Node 在壓測期間有沒有變吃緊。

看法：比較**壓測前**與**壓測中**的曲線。壓測一開始就往上飆，再對照第 2 區【他】的用量和第 5 區【鄰居】，判斷是不是他造成的。壓測前後都很平，就代表 Node 沒被拖慢。""", w=24, h=3)
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
ts(L, "【整台 Node】磁碟忙碌程度",
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
ts(L, "【整台 Node】檔案系統使用率",
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
ts(L, "【整台 Node】conntrack 連線追蹤表使用率",
   [('node_nf_conntrack_entries{@NODE@} / node_nf_conntrack_entries_limit{@NODE@}', "{{instance}}")],
   "percentunit",
   desc(V_NODE,
        "Node 核心記錄每條網路連線的表格，所有 Pod 共用一張，有容量上限。大量短連線（例如每次都重新連 DB / Redis / Kafka）最容易吃滿。",
        "超過 70% 要注意；滿了之後新連線會被直接丟掉，同 Node 所有 Pod 都會連不出去。"),
   minv=0)
ts(L, "【整台 Node】CPU 使用率",
   [('1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle", @NODE@}[@R@]))', "{{instance}}")],
   "percentunit",
   desc(V_NODE, "整台 Node 的 CPU 有多少時間在工作。",
        "長時間超過 80% 且 PSI CPU 也上升時，沒設 limit 或超用 request 的 Pod 會互相搶 CPU。"),
   minv=0)
ts(L, "【整台 Node】可用記憶體比例",
   [('node_memory_MemAvailable_bytes{@NODE@} / node_memory_MemTotal_bytes{@NODE@}', "{{instance}}")],
   "percentunit",
   desc(V_NODE, "整台 Node 還能給程式用的記憶體比例。",
        "低於 10% 要注意，kubelet 可能開始驅逐 Pod（MemoryPressure）。"),
   minv=0)
ts(L, "【整台 Node】Node 壓力狀態（1 = 發生中）",
   [('max by (node, condition) (kube_node_status_condition{condition=~"DiskPressure|MemoryPressure|PIDPressure", status="true", node=~"$node"})', "{{node}} {{condition}}")],
   "none",
   desc(V_NODE, "kubelet 回報 Node 進入磁碟、記憶體或程序數不足的狀態。",
        "出現 1 代表 Node 已經嚴重吃緊，kubelet 會開始驅逐 Pod，影響同 Node 的所有服務。"),
   minv=0)
L.newline()

# ---------------------------------------------------------------- 5 neighbours
row("5. 【鄰居】所選 Node 上用量前 10 名（含他自己）")
L = Line()
JOIN = ' * on (namespace, pod) group_left () max by (namespace, pod) (kube_pod_info{node=~"$node"})'
NB_HOW = "第 4 區往上飆的時候，看是誰同時上升。如果前幾名都是他，加上 Node 壓力同時上升，就是他造成的；如果是別人，就跟他無關。"
ts(L, "【鄰居】CPU Top 10",
   [('topk(10, sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{container!=""}[@R@]))' + JOIN + ')', "{{namespace}}/{{pod}}")],
   "none", desc(V_NEIGHBOR, "所選 Node 上 CPU 用量最多的 10 個 Pod。", NB_HOW), w=12, h=8, minv=0)
ts(L, "【鄰居】記憶體 Top 10",
   [('topk(10, sum by (namespace, pod) (container_memory_working_set_bytes{container!=""})' + JOIN + ')', "{{namespace}}/{{pod}}")],
   "bytes", desc(V_NEIGHBOR, "所選 Node 上記憶體用量最多的 10 個 Pod。", NB_HOW), w=12, h=8, minv=0)
ts(L, "【鄰居】磁碟寫入 Top 10",
   [('topk(10, ' + pod_io("write") + JOIN + ')', "{{namespace}}/{{pod}}")],
   "Bps", desc(V_NEIGHBOR, "所選 Node 上寫磁碟最多的 10 個 Pod。", NB_HOW), w=12, h=8, minv=0,
   no_value=NO_POD_IO)
ts(L, "【鄰居】網路送出 Top 10",
   [('topk(10, sum by (namespace, pod) (rate(container_network_transmit_bytes_total[@R@]))' + JOIN + ')', "{{namespace}}/{{pod}}")],
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
   desc(V_SHARED, "全叢集 DNS 服務的查詢量與最慢 1% 查詢的延遲。",
        "壓測時若每次連線都重新查 DNS，查詢量會飆高；P99 延遲上升時，全叢集的服務解析名稱都會變慢。"),
   minv=0)
L.newline()


# ---------------------------------------------------------------- dashboard
def qvar(name, label, query, multi=False, include_all=False):
    return {"type": "query", "name": name, "label": label, "datasource": DS,
            "query": query, "definition": query, "refresh": 2, "sort": 1,
            "multi": multi, "includeAll": include_all, "hide": 0, "current": {}}


dash = {
    "uid": "ocp-loadtest-impact",
    "title": "OCP 壓測影響評估（Workload vs Node 共用資源）",
    "tags": ["ocp", "loadtest", "capacity"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 4,
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
    ]},
    "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                              "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                              "name": "Annotations & Alerts", "type": "dashboard"}]},
    "panels": panels,
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(dash, f, ensure_ascii=False, indent=2)
print("panels:", len(panels))
