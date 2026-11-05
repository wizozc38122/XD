"""Generate the OCP two-cluster diff Grafana dashboard.

Two clusters that should run the same thing (active-active) but show very different Request %:
pick cluster A and cluster B at the top, and every table lines the two up row by row
(namespace, workload, container) so the gap can be traced to what is different.

Each cluster is its own Prometheus data source, so the comparison happens in Grafana:
every panel uses the Mixed data source, runs the same query against A and against B,
and joins the results with the merge transformation.

Usage: python gen_cluster_diff_dashboard.py ocp-cluster-diff-dashboard.json
"""
import json
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "ocp-cluster-diff-dashboard.json"
DS = {"A": {"type": "prometheus", "uid": "${ds_a}"}, "B": {"type": "prometheus", "uid": "${ds_b}"}}
MIXED = {"type": "datasource", "uid": "-- Mixed --"}
OVERVIEW_UID = "ocp-cluster-risk-overview"

NSF = 'namespace=~"$ns", namespace!~"$exclude"'


def sel(*parts):
    return ", ".join(p for p in parts if p)


# ---------------------------------------------------------------- per-pod building blocks (labels: namespace, pod)
def live(f):
    """Pods that hold requests on a node: scheduled to one and not finished yet (Pending / Running)."""
    phase = sel('phase=~"Pending|Running"', f)
    info = sel('node!=""', f)
    return (f'(max by (namespace, pod) (kube_pod_status_phase{{{phase}}} == 1)'
            f' and on (namespace, pod) kube_pod_info{{{info}}})')


LIVE = live(NSF)
LIVE_ALL = live("")


def spec_ct(kind, resource, f=NSF):
    """Request or limit per container of the live pods. A value of 0 means "not set" to Kubernetes, so it is
    dropped. max by container: some clusters scrape kube-state-metrics twice, which would double every sum."""
    m = sel(f'resource="{resource}"', f)
    return (f'(max by (namespace, pod, container) (kube_pod_container_resource_{kind}{{{m}}} > 0)'
            f' and on (namespace, pod) {LIVE if f else LIVE_ALL})')


def pod_req(resource, f=NSF):
    return f'sum by (namespace, pod) ({spec_ct("requests", resource, f)})'


def owner(kind, workload, workload_type):
    """kube_pod_owner of one owner kind (a label matcher), relabelled to workload / workload_type.
    workload / workload_type are label_replace arguments: replacement, source label, regex."""
    return (f'label_replace(label_replace(kube_pod_owner{{{kind}, {NSF}}}, "workload", {workload}),'
            f' "workload_type", {workload_type})')


# Pod -> workload. Names that differ between clusters on purpose are normalised so the same workload
# lines up: Deployments go through their ReplicaSet (drops the pod-template-hash), DeploymentConfig pods
# (ReplicationController <name>-<revision>) and Job pods (CronJob <name>-<schedule time>) drop the trailing
# number, and static pods (owned by the Node, named after it) become one row per namespace.
DEPLOY = ('label_replace(label_replace('
          f'label_replace(kube_pod_owner{{owner_kind="ReplicaSet", {NSF}}}, "replicaset", "$1", "owner_name", "(.*)")'
          ' * on (namespace, replicaset) group_left (owner_deploy) '
          'max by (namespace, replicaset, owner_deploy) ('
          f'label_replace(kube_replicaset_owner{{owner_kind="Deployment", {NSF}}}, "owner_deploy", "$1", "owner_name", "(.*)"))'
          ', "workload", "$1", "owner_deploy", "(.*)"), "workload_type", "Deployment", "", "")')
OWNER_NAME = '"$1", "owner_name", "(.*)"'
STRIP_NUM = '"$1", "owner_name", "(.*?)(-[0-9]+)?"'
OWNER_KIND = '"$1", "owner_kind", "(.*)"'
KNOWN = ('(' + DEPLOY
         + ' or ' + owner('owner_kind=~"StatefulSet|DaemonSet"', OWNER_NAME, OWNER_KIND)
         + ' or ' + owner('owner_kind="ReplicationController"', STRIP_NUM, '"DeploymentConfig", "", ""')
         + ' or ' + owner('owner_kind="Job"', STRIP_NUM, '"Job", "", ""') + ')')
WL = ('max by (namespace, pod, workload, workload_type) (' + KNOWN
      # anything else that has an owner (a ReplicaSet without a Deployment, an operator's own kind)
      + ' or ' + owner('owner_kind!~"<none>|Node"', OWNER_NAME, OWNER_KIND) + f' unless on (namespace, pod) {KNOWN}'
      + ' or ' + owner('owner_kind="Node"', '"(static pods)", "", ""', '"Node", "", ""')
      + ' or ' + owner('owner_kind="<none>"', '"$1", "pod", "(.*)"', '"Pod", "", ""') + ')')

K_WL = "namespace, workload, workload_type"
K_CT = "namespace, workload, workload_type, container"


def by_wl(per_pod, agg="sum", keys=K_WL):
    return f'{agg} by ({keys}) (({per_pod}) * on (namespace, pod) group_left (workload, workload_type) {WL})'


WL_PODS = f'count by ({K_WL}) ({LIVE} * on (namespace, pod) group_left (workload, workload_type) {WL})'
CT_ROWS = by_wl(f'max by (namespace, pod, container) (kube_pod_container_info{{{NSF}}}) and on (namespace, pod) {LIVE}',
                "count", K_CT)


def ct_spec(kind, resource):
    """Per-container request / limit of one pod (max over the workload's pods: they share one template)."""
    return by_wl(spec_ct(kind, resource), "max", K_CT)


NS_PODS = f'count by (namespace) ({LIVE})'


def ns_req(resource):
    return f'sum by (namespace) ({pod_req(resource)})'


# ---------------------------------------------------------------- node / cluster level (no namespace filter:
# every pod's request takes up node allocatable, whoever it belongs to)
PODS_NODE = f'(max by (namespace, pod, node) (kube_pod_info{{node!=""}}) and on (namespace, pod) {LIVE_ALL})'


def alloc(resource):
    return f'max by (node) (kube_node_status_allocatable{{resource="{resource}"}})'


def node_req(resource):
    """Requests of the live pods on each node; 0 on a node without any."""
    return (f'(sum by (node) ({spec_ct("requests", resource, "")} * on (namespace, pod) group_left (node) {PODS_NODE})'
            f' or {alloc(resource)} * 0)')


SCHED_OK = ('(max by (node) (kube_node_spec_unschedulable == 0)'
            ' unless on (node) kube_node_spec_taint{effect=~"NoSchedule|NoExecute"})')
ROLE = ('(max by (node, role) (kube_node_role)'
        ' or label_replace(max by (node) (kube_node_info) unless on (node) kube_node_role, "role", "(none)", "", ""))')


def by_role(per_node):
    return f'sum by (role) ({ROLE} * on (node) group_left () ({per_node}))'


GIB = 1024 ** 3


def items(rows):
    """One query returning one series per row, labelled item; rows: (label, expr)."""
    return " or ".join(f'label_replace(({e}), "item", "{i + 1:02d} {label}", "", "")' for i, (label, e) in enumerate(rows))


def pct(num, den):
    return f'100 * ({num}) / ({den})'


TOTAL_ROWS = [
    ("Node 數", 'count(max by (node) (kube_node_info))'),
    ("可排程 Node 數", f'count({SCHED_OK}) or vector(0)'),
    ("Pod 數（已排程、未結束）", f'count({LIVE_ALL}) or vector(0)'),
    ("排不進來的 Pod", 'count(max by (namespace, pod) (kube_pod_status_unschedulable) == 1) or vector(0)'),
    ("可分配 CPU（核）", f'sum({alloc("cpu")})'),
    ("CPU Request（核）", f'sum({node_req("cpu")})'),
    ("CPU Request ÷ 可分配（%，全部 Node）", pct(f'sum({node_req("cpu")})', f'sum({alloc("cpu")})')),
    ("CPU Request ÷ 可分配（%，可排程 Node）",
     pct(f'sum({node_req("cpu")} and on (node) {SCHED_OK})', f'sum({alloc("cpu")} and on (node) {SCHED_OK})')),
    ("可分配記憶體（GiB）", f'sum({alloc("memory")}) / {GIB}'),
    ("記憶體 Request（GiB）", f'sum({node_req("memory")}) / {GIB}'),
    ("記憶體 Request ÷ 可分配（%，全部 Node）", pct(f'sum({node_req("memory")})', f'sum({alloc("memory")})')),
    ("記憶體 Request ÷ 可分配（%，可排程 Node）",
     pct(f'sum({node_req("memory")} and on (node) {SCHED_OK})', f'sum({alloc("memory")} and on (node) {SCHED_OK})')),
    ("篩選內 Namespace 數", f'count(count by (namespace) ({LIVE})) or vector(0)'),
    ("篩選內 Workload 數", f'count({WL_PODS}) or vector(0)'),
    ("篩選內 Pod 數", f'count({LIVE}) or vector(0)'),
    ("篩選內 CPU Request（核）", f'sum({pod_req("cpu")}) or vector(0)'),
    ("篩選內記憶體 Request（GiB）", f'(sum({pod_req("memory")}) or vector(0)) / {GIB}'),
]

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


def text(line, content, w=24, h=8):
    line.add({"id": nid(), "type": "text", "title": "",
              "options": {"mode": "markdown", "content": content}}, w, h)


def target(side, ref, expr, fmt="table", legend=""):
    t = {"refId": ref, "datasource": DS[side], "expr": expr, "legendFormat": legend}
    if fmt == "table":
        t.update({"range": False, "instant": True, "format": "table"})
    else:
        t.update({"range": True, "instant": False})
    return t


def row_sum(names, alias):
    """Adds a field = sum of the named fields in each row. Missing cells (a row that exists in only one
    cluster) are skipped, i.e. count as 0; plain binary operations would turn them into null or NaN."""
    return {"id": "calculateField", "options": {"mode": "reduceRow", "alias": alias, "replaceFields": False,
                                                "reduce": {"include": names, "reducer": "sum"}}}


def row_max(names, alias):
    return {"id": "calculateField", "options": {"mode": "reduceRow", "alias": alias, "replaceFields": False,
                                                "reduce": {"include": names, "reducer": "max"}}}


KEY_WIDTH = {"namespace": 150, "workload": 180, "workload_type": 110, "container": 130, "item": 330, "role": 110,
             "node": 200}
KEY_NAME = {"namespace": "Namespace", "workload": "Workload", "workload_type": "類型", "container": "容器",
            "item": "項目", "role": "角色", "node": "Node"}
GREY = "#8e8e8e"
STATUS_MAP = [{"type": "value", "options": {
    "1": {"text": "只有 A", "color": "orange", "index": 0},
    "2": {"text": "只有 B", "color": "purple", "index": 1},
    "3": {"text": "兩邊都有", "color": "transparent", "index": 2}}}]
SAME_MAP = [{"type": "value", "options": {"0": {"text": "一樣", "color": "transparent", "index": 0}}},
            {"type": "range", "options": {"from": 1e-12, "to": 1e300,
                                          "result": {"text": "不同", "color": "red", "index": 1}}}]
# B − A: B 比較少藍色、一樣不上色、B 比較多橘色
SIGNED = {"mode": "absolute", "steps": [{"color": "blue", "value": None}, {"color": "text", "value": 0},
                                        {"color": "orange", "value": 1e-9}]}


def compare_table(line, title, keys, metrics, description, sort_by, rows=None, only=None, same=False,
                  w=24, h=12, hide=(), sort_desc=True):
    """Lines up cluster A and cluster B row by row.

    keys: label columns both queries return (the join key).
    metrics: (name, expr, unit, decimals, diff) — the same expr runs against A and B; diff is
      "abs" (adds 差距 = |A − B|), "signed" (adds B − A) or None (only the A / B columns).
    rows: an expr that has a series for every row that exists in a cluster; adds 狀態 (只有 A / 只有 B / 兩邊都有).
    only: "one-side" keeps only rows that exist in one cluster; "diff" keeps only rows where some diff ≠ 0.
    same: adds 設定 (一樣 / 不同): any of the metrics differs, a value set on one side only counts too.

    A cell is empty when that cluster has no such row or the value is not set there. Differences treat
    an empty cell as 0, so a workload only in A shows its whole request as the gap."""
    targets, tf, index, rename, overrides, hidden = [], [], {}, {}, [], []
    for i, k in enumerate(keys):
        index[k] = i
        rename[k] = KEY_NAME.get(k, k)
    n = len(keys)

    def place(field, name=None):
        index[field] = len(index)
        if name:
            rename[field] = name

    if rows:
        targets += [target("A", "fa", f'({rows}) * 0 + 1'), target("B", "fb", f'({rows}) * 0 + 2')]
        hidden += ["Value #fa", "Value #fb"]
        tf.append(row_sum(["Value #fa", "Value #fb"], "狀態"))
        place("狀態")
        overrides.append({"matcher": {"id": "byName", "options": "狀態"}, "properties": [
            {"id": "mappings", "value": STATUS_MAP}, {"id": "custom.width", "value": 90},
            {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}}]})
    if same:
        place("設定")
    diffs = []
    for m, (name, expr, unit, decimals, diff) in enumerate(metrics):
        a, b = f"Value #a{m}", f"Value #b{m}"
        targets += [target("A", f"a{m}", expr), target("B", f"b{m}", expr)]
        cols = [f"{name} A".strip(), f"{name} B".strip()]
        place(a, cols[0])
        place(b, cols[1])
        if diff:
            # B 那邊也一定要有第二個查詢：Mixed 會把同一個資料來源的查詢分成一批送出，
            # 一批只有一個查詢時欄位叫「Value」而不是「Value #b0」，改名和計算就對不到
            targets += [target("A", f"na{m}", f'-({expr})'), target("B", f"nb{m}", f'-({expr})')]
            hidden += [f"Value #na{m}", f"Value #nb{m}"]
            d = f"_d{m}" if diff == "abs" else f"{name} B − A".strip()
            tf.append(row_sum([b, f"Value #na{m}"], d))
            if diff == "abs":
                hidden += [d, f"_nd{m}"]
                tf += [row_sum([a, f"Value #nb{m}"], f"_nd{m}"), row_max([d, f"_nd{m}"], f"{name} 差距")]
                d = f"{name} 差距"
            place(d)
            cols.append(d)
            diffs.append(d)
            if diff == "signed":
                overrides.append({"matcher": {"id": "byName", "options": d}, "properties": [
                    {"id": "thresholds", "value": SIGNED}, {"id": "color", "value": {"mode": "thresholds"}},
                    {"id": "custom.cellOptions", "value": {"type": "color-text"}}]})
        for c in cols:
            props = [{"id": "unit", "value": unit}]
            if decimals is not None:
                props.append({"id": "decimals", "value": decimals})
            overrides.append({"matcher": {"id": "byName", "options": c}, "properties": props})
    if same:
        # every 差距 added up: units differ, so only "0 or not" means anything (needs diff="abs" metrics)
        tf.append(row_sum(diffs, "設定"))
        overrides.append({"matcher": {"id": "byName", "options": "設定"}, "properties": [
            {"id": "mappings", "value": SAME_MAP}, {"id": "custom.width", "value": 70},
            {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}}]})
    if only == "one-side":
        tf.append({"id": "filterByValue", "options": {"type": "include", "match": "any", "filters": [
            {"fieldName": "狀態", "config": {"id": "lower", "options": {"value": 3}}}]}})
    elif only == "diff":
        tf.append({"id": "filterByValue", "options": {"type": "include", "match": "any", "filters": [
            {"fieldName": "設定", "config": {"id": "greater", "options": {"value": 0}}}]}})
    for k in hide:
        hidden.append(k)
    for side in "AB":
        # 一個資料來源只有一個查詢時，Grafana 把欄位叫「Value」而不是「Value #<refId>」
        assert sum(t["datasource"] is DS[side] for t in targets) >= 2, title
    line.add({
        "id": nid(), "type": "table", "title": title, "description": description,
        "datasource": MIXED, "targets": targets,
        "transformations": [
            # 兩個資料來源各自帶時間戳記，先拿掉，merge 才會只用 label 欄位對齊同一列
            {"id": "organize", "options": {"excludeByName": {"Time": True}}},
            {"id": "merge", "options": {}},
            *tf,
            {"id": "organize", "options": {"excludeByName": {h_: True for h_ in hidden},
                                           "renameByName": rename, "indexByName": index}},
        ],
        "fieldConfig": {"defaults": {"custom": {"filterable": False, "align": "auto", "minWidth": 70,
                                                "wrapHeaderText": True}},
                        "overrides": overrides + [{"matcher": {"id": "byName", "options": rename[k]},
                                                   "properties": [{"id": "custom.filterable", "value": True},
                                                                  {"id": "custom.width", "value": KEY_WIDTH.get(k, 120)}]}
                                                  for k in keys]},
        "options": {"showHeader": True, "sortBy": [{"displayName": sort_by, "desc": sort_desc}],
                    "footer": {"show": False}, "cellHeight": "sm"},
    }, w, h)


def side_table(line, side, title, key, columns, description, sort_by, w=12, h=10):
    """A plain table of one cluster; columns: (name, expr, unit, decimals, thresholds or None, mappings or None)."""
    targets, rename, index, overrides = [], {key: KEY_NAME.get(key, key)}, {key: 0}, []
    for i, (name, expr, unit, decimals, thr, mappings) in enumerate(columns):
        ref = chr(65 + i)
        targets.append(target(side, ref, expr))
        rename[f"Value #{ref}"] = name
        index[f"Value #{ref}"] = i + 1
        props = [{"id": "unit", "value": unit}]
        if decimals is not None:
            props.append({"id": "decimals", "value": decimals})
        if thr:
            props += [{"id": "thresholds", "value": {"mode": "absolute", "steps": thr}},
                      {"id": "color", "value": {"mode": "thresholds"}},
                      {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}}]
        if mappings:
            props.append({"id": "mappings", "value": mappings})
        overrides.append({"matcher": {"id": "byName", "options": name}, "properties": props})
    line.add({
        "id": nid(), "type": "table", "title": title, "description": description, "datasource": DS[side],
        "targets": targets,
        "transformations": [{"id": "merge", "options": {}},
                            {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                           "renameByName": rename, "indexByName": index}}],
        "fieldConfig": {"defaults": {"custom": {"filterable": False, "align": "auto", "minWidth": 70,
                                                "wrapHeaderText": True}},
                        "overrides": overrides + [{"matcher": {"id": "byName", "options": rename[key]},
                                                   "properties": [{"id": "custom.filterable", "value": True},
                                                                  {"id": "custom.width", "value": KEY_WIDTH.get(key, 120)}]}]},
        "options": {"showHeader": True, "sortBy": [{"displayName": sort_by, "desc": True}],
                    "footer": {"show": False}, "cellHeight": "sm"},
    }, w, h)


def ts_ab(line, title, expr, unit, description, w=12, h=8):
    """The same query against A and B as two lines."""
    line.add({
        "id": nid(), "type": "timeseries", "title": title, "description": description, "datasource": MIXED,
        "targets": [target("A", "A", expr, "range", "A"), target("B", "B", expr, "range", "B")],
        "fieldConfig": {"defaults": {"unit": unit, "min": 0,
                                     "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0,
                                                "showPoints": "never", "spanNulls": True}},
                        "overrides": [
                            {"matcher": {"id": "byFrameRefID", "options": "A"},
                             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}]},
                            {"matcher": {"id": "byFrameRefID", "options": "B"},
                             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "purple"}}]}]},
        "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "max"]},
                    "tooltip": {"mode": "multi", "sort": "none"}},
    }, w, h)


FULL = [{"color": "green", "value": None}, {"color": "yellow", "value": 0.8}, {"color": "red", "value": 0.9}]
V_FILTER = "上方 Namespace 篩選內，已排程、還沒結束（Pending / Running）的 Pod"
V_ALL = "整個叢集所有 Node、所有 namespace（不受上方 Namespace 篩選影響）"
GAP = ("**CPU / 記憶體** = Request 加總（CPU 單位是核）；**差距** = |A − B|，一邊沒有這一列（或沒設）就當 0 算，所以只在一邊有的，整個量都是差距；"
       "**B − A** 正數（橘）= B 比較多，負數（藍）= A 比較多。")

# ---------------------------------------------------------------- guide
L = Line()
text(L, """**雙叢集差異比對**：兩個叢集照理說部署一樣的東西（AA 架構），Request % 卻差很多 —— 上方選 **叢集 A**、**叢集 B**，每張表都把兩邊一列一列對齊。

Request % = **Request 加總 ÷ 可分配**，差異只會來自這幾個地方，由上往下查：

1. **分母不同**：Node 數、Node 規格、可排程的 Node 不一樣 → 第 1 區
2. **某一邊多部署了東西**（只有 A / 只有 B）→ 第 2 區（常見：operator、logging、監控、測試用的 namespace）
3. **同一個 Workload Pod 數不同**（replicas、HPA 擴出去沒縮回來）→ 第 3 區的「Pod B − A」
4. **同一個 Workload 每個 Pod 的 Request 不同**（設定沒同步、sidecar 只有一邊注入）→ 第 4 區只列出設定不一樣的容器

預設**不排除**任何 namespace：Request % 是整個叢集算的，`openshift-*` 系統元件也可能就是差異來源。只想看自己的服務，在上方「排除的 Namespace」填 `openshift.*|kube-.*`。
表格都是時間範圍**結束時**的快照；只算已排程、還沒結束（Pending / Running）的 Pod，跟排程器佔住 Node 的方式一樣。""", h=7)
L.newline()

# ---------------------------------------------------------------- 1 cluster totals
row("1.【叢集】總量對照：Request % 差在分子（Request）還是分母（可分配）")
L = Line()
compare_table(L, "【叢集】A vs B 總量", ["item"],
              [("", items(TOTAL_ROWS), "none", 2, "signed")],
              desc("前 12 列是" + V_ALL + "；「篩選內」開頭的列是" + V_FILTER,
                   "兩個叢集的 Node 數、可分配、Request 加總與 Request ÷ 可分配。"
                   "記憶體換算成 GiB、百分比乘 100，才能放在同一欄。"
                   "**可排程 Node** = 沒有 cordon、沒有 NoSchedule / NoExecute taint 的 Node（通常就是 worker），"
                   "跟「OCP 叢集風險總覽」第 5 區的「叢集 Request ÷ 可分配」算法一樣。",
                   "先比「CPU Request ÷ 可分配」是分子還是分母造成的：**可分配差很多** → Node 數或規格不同，到右邊看哪個角色的 Node 不一樣；"
                   "**Request 差很多** → 往下第 2–4 區找是誰。「篩選內」那幾列可以看排除系統 namespace 後還剩多少差距。"),
              sort_by="項目", sort_desc=False, w=11, h=14)
NODE_ROLE = [("Node", by_role("max by (node) (kube_node_info)"), "none", 0, None),
             ("可分配 CPU", by_role(alloc("cpu")), "none", 1, None),
             ("CPU Req%", f'{by_role(node_req("cpu"))} / {by_role(alloc("cpu"))}', "percentunit", 0, None),
             ("可分配記憶體", by_role(alloc("memory")), "bytes", 0, None),
             ("記憶體 Req%", f'{by_role(node_req("memory"))} / {by_role(alloc("memory"))}',
              "percentunit", 0, None)]
compare_table(L, "【Node 角色】A vs B", ["role"], NODE_ROLE,
              desc(V_ALL + "，依 Node 角色（kube_node_role）分組",
                   "每個角色的 Node 數、可分配加總、該角色 Node 上 Pod 的 Request ÷ 可分配（**Req%**）。可分配 CPU 單位是核。"
                   "一台 Node 有兩個角色（例如 master + worker）會兩邊都算；沒有角色標籤的 Node 歸在 (none)。",
                   "Node 數、可分配不一樣 → 分母不同，Request % 不能直接比；某個角色只有一邊有（例如 infra）→ "
                   "那一邊有些 Pod 跑在 infra Node 上，分子分母都會跟另一邊不同。"),
              sort_by="角色", sort_desc=False, w=13, h=6)
ts_ab(L, "【叢集】CPU Request ÷ 可分配（可排程 Node）", f'sum({node_req("cpu")} and on (node) {SCHED_OK})'
      f' / sum({alloc("cpu")} and on (node) {SCHED_OK})', "percentunit",
      desc(V_ALL, "兩個叢集的 CPU Request ÷ 可分配隨時間的變化。",
           "時間範圍拉長（例如 7 天）看差距是一直都在，還是某個時間點（部署、擴容、加 Node）才拉開。"), w=6, h=7)
ts_ab(L, "【叢集】記憶體 Request ÷ 可分配（可排程 Node）", f'sum({node_req("memory")} and on (node) {SCHED_OK})'
      f' / sum({alloc("memory")} and on (node) {SCHED_OK})', "percentunit",
      desc(V_ALL, "兩個叢集的記憶體 Request ÷ 可分配隨時間的變化。",
           "時間範圍拉長（例如 7 天）看差距是一直都在，還是某個時間點（部署、擴容、加 Node）才拉開。"), w=7, h=7)
# 兩張趨勢圖疊在 Node 角色表下面，跟左邊的總量表同高
total_p, role_p, cpu_p, mem_p = panels[-4:]
cpu_p["gridPos"] = {"h": 8, "w": 6, "x": 11, "y": role_p["gridPos"]["y"] + 6}
mem_p["gridPos"] = {"h": 8, "w": 7, "x": 17, "y": role_p["gridPos"]["y"] + 6}
_y[0] = total_p["gridPos"]["y"] + 14
L = Line()
L = Line()
# 0 可排程、1 cordon 或有 NoSchedule / NoExecute taint
SCHED = f'({SCHED_OK} or {alloc("pods")} * 0 + 1)'
SCHED_MAP = [{"type": "value", "options": {"0": {"text": "可排程"}, "1": {"text": "不可排程", "color": "yellow"}}}]
NODE_COLS = [("可排程", SCHED, "none", 0, None, SCHED_MAP),
             ("Pod 數", f'count by (node) ({PODS_NODE}) or {alloc("pods")} * 0', "none", 0, None, None),
             ("可分配 CPU（核）", alloc("cpu"), "none", 1, None, None),
             ("CPU Request ÷ 可分配", f'{node_req("cpu")} / {alloc("cpu")}', "percentunit", 0, FULL, None),
             ("可分配記憶體", alloc("memory"), "bytes", 0, None, None),
             ("記憶體 Request ÷ 可分配", f'{node_req("memory")} / {alloc("memory")}', "percentunit", 0, FULL, None)]
for side in ("A", "B"):
    side_table(L, side, f"【Node】叢集 {side} 每台 Node", "node", NODE_COLS,
               desc(V_ALL, "每台 Node 是否可排程（沒有 cordon、沒有 NoSchedule / NoExecute taint）、Pod 數、可分配、Request ÷ 可分配。",
                    "兩張表左右對照：Node 台數、規格（可分配）一不一樣；Request ÷ 可分配是平均分散，還是集中在某幾台。"
                    "不可排程的 Node（通常是 master）不算在「可排程 Node」的 Request ÷ 可分配裡。"),
               sort_by="CPU Request ÷ 可分配", h=9)
L.newline()

# ---------------------------------------------------------------- 2 deployed on one side only
row("2.【部署】只在一邊有的 Namespace / Workload")
L = Line()
ONE_SIDE_HOW = "依 CPU 差距排序，越上面對 Request % 影響越大。確認是該補部署、該刪掉，還是本來就只在一邊（例如測試用）。"
compare_table(L, "【Namespace】只在一邊有", ["namespace"],
              [("Pod", NS_PODS, "none", 0, None),
               ("CPU", ns_req("cpu"), "none", 2, "abs"),
               ("記憶體", ns_req("memory"), "bytes", 1, None)],
              desc(V_FILTER, "只有其中一個叢集有在跑 Pod 的 namespace。**CPU / 記憶體** = Request 加總（CPU 單位是核）。", ONE_SIDE_HOW),
              sort_by="CPU 差距", rows=NS_PODS, only="one-side", w=9, h=9)
compare_table(L, "【Workload】只在一邊有", ["namespace", "workload", "workload_type"],
              [("Pod", WL_PODS, "none", 0, None),
               ("CPU", by_wl(pod_req("cpu")), "none", 2, "abs"),
               ("記憶體", by_wl(pod_req("memory")), "bytes", 1, None)],
              desc(V_FILTER + "，每個 Workload 所有 Pod 合計",
                   "只有其中一個叢集有的 Workload。Deployment 用 Deployment 名稱比，DeploymentConfig、Job 去掉名稱最後的流水號，"
                   "Node 擁有的靜態 Pod 每個 namespace 合成一列 (static pods)。同一個服務兩邊類型不同（例如一邊 Deployment、"
                   "一邊 DeploymentConfig）會出現成兩列。**CPU / 記憶體** = Request 加總（CPU 單位是核）。", ONE_SIDE_HOW),
              sort_by="CPU 差距", rows=WL_PODS, only="one-side", w=15, h=9)
L.newline()

# ---------------------------------------------------------------- 3 namespace / workload side by side
row("3.【對照】每個 Namespace / Workload：Pod 數與 Request 差多少")
L = Line()
compare_table(L, "【Namespace】A vs B", ["namespace"],
              [("Pod", NS_PODS, "none", 0, "signed"),
               ("CPU", ns_req("cpu"), "none", 2, "abs"),
               ("記憶體", ns_req("memory"), "bytes", 1, "abs")],
              desc(V_FILTER + "，每個 namespace 所有 Pod 合計", "兩個叢集每個 namespace 的 Pod 數與 Request 加總。" + GAP,
                   "依 CPU 差距排序（點表頭可改依記憶體）。差距大的 namespace，在下方 Workload 表用 Namespace 欄的漏斗篩選，找是哪個 Workload。"),
              sort_by="CPU 差距", rows=NS_PODS, w=24, h=9)
compare_table(L, "【Workload】A vs B", ["namespace", "workload", "workload_type"],
              [("Pod", WL_PODS, "none", 0, "signed"),
               ("CPU", by_wl(pod_req("cpu")), "none", 2, "abs"),
               ("記憶體", by_wl(pod_req("memory")), "bytes", 1, "abs")],
              desc(V_FILTER + "，每個 Workload 所有 Pod 合計", "兩個叢集每個 Workload 的 Pod 數與 Request 加總。" + GAP,
                   "差距大的 Workload：**Pod B − A ≠ 0** → replicas 或 HPA 不同（`oc get hpa`）；"
                   "**Pod 數一樣、Request 還是不同** → 每個 Pod 的設定不同，到第 4 區看是哪個容器。"),
              sort_by="CPU 差距", rows=WL_PODS, w=24, h=14)
L.newline()

# ---------------------------------------------------------------- 4 container spec
row("4.【設定】同一個 Workload 每個 Pod 的 Request / Limit 不一樣的容器")
L = Line()
compare_table(L, "【容器】設定不一樣的容器（每個 Pod 的值）", ["namespace", "workload", "workload_type", "container"],
              [("CPU Req", ct_spec("requests", "cpu"), "none", 3, "abs"),
               ("記憶體 Req", ct_spec("requests", "memory"), "bytes", 1, "abs"),
               ("CPU Lim", ct_spec("limits", "cpu"), "none", 3, "abs"),
               ("記憶體 Lim", ct_spec("limits", "memory"), "bytes", 1, "abs")],
              desc(V_FILTER + "，每個 Workload 的每個容器（一個 Pod 的值，不是所有 Pod 加總）",
                   "只列出兩邊設定不一樣的容器：Request / Limit 數字不同、只有一邊有設、或這個容器只有一邊有"
                   "（例如 sidecar 只在一邊注入，狀態會是「只有 A / 只有 B」）。空白 = 沒設，或沒有這個容器。"
                   "**Req / Lim** = Request / Limit（CPU 單位是核）。同一個 Workload 的 Pod 共用一份範本，所以取最大的那個 Pod；rollout 進行中新舊版本不同時會取到比較大的。" + GAP,
                   "依 CPU 差距排序。單一 Pod 差一點、Pod 數一多，整個 Workload 就差很多：差距 × 第 3 區的 Pod 數（Pod A / Pod B） ≈ 這個容器造成的總差距。"
                   "表格是空的 → 兩邊每個容器的設定都一樣，差異來自 Pod 數或部署內容不同。"),
              sort_by="CPU Req 差距", rows=CT_ROWS, only="diff", same=True, w=24, h=14)
L.newline()

# ---------------------------------------------------------------- dashboard
dash = {
    "uid": "ocp-cluster-diff",
    "title": "OCP 雙叢集差異比對（A vs B：部署、Pod 數、Request）",
    "tags": ["ocp", "capacity", "compare"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "editable": True,
    "graphTooltip": 1,
    "refresh": "",
    "time": {"from": "now-1h", "to": "now"},
    "links": [{"title": "OCP 叢集風險總覽（叢集 A）", "type": "link", "icon": "dashboard",
               "url": "/d/" + OVERVIEW_UID + "/?var-datasource=${ds_a}", "keepTime": True, "targetBlank": False},
              {"title": "OCP 叢集風險總覽（叢集 B）", "type": "link", "icon": "dashboard",
               "url": "/d/" + OVERVIEW_UID + "/?var-datasource=${ds_b}", "keepTime": True, "targetBlank": False}],
    "templating": {"list": [
        {"type": "datasource", "name": "ds_a", "label": "叢集 A", "query": "prometheus",
         "regex": "/^Prometheus.*OCP$/", "current": {}, "hide": 0, "refresh": 1},
        {"type": "datasource", "name": "ds_b", "label": "叢集 B", "query": "prometheus",
         "regex": "/^Prometheus.*OCP$/", "current": {}, "hide": 0, "refresh": 1},
        # 兩個叢集的 namespace 不一定一樣，下拉選單只能從一個資料來源取值，所以用 regex 文字框
        {"type": "textbox", "name": "ns", "label": "Namespace（regex）", "query": ".*",
         "current": {"text": ".*", "value": ".*"}, "hide": 0},
        {"type": "textbox", "name": "exclude", "label": "排除的 Namespace（regex）", "query": "",
         "current": {"text": "", "value": ""}, "hide": 0},
    ]},
    "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                              "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                              "name": "Annotations & Alerts", "type": "dashboard"}]},
    "panels": panels,
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(dash, f, ensure_ascii=False, indent=2)
print("panels:", len(panels))
