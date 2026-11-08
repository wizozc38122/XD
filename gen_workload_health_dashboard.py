"""Generate the OCP workload health Grafana dashboard (all clusters on one page).

One table of counters (a row per cluster, red when > 0) and one detail table per problem type with
the rows of every selected cluster merged in, told apart by a cluster column, so nothing needs
switching data sources or opening one section per cluster.
Only kube-state-metrics series are used; the reason text stays in the OCP console Events tab.

Usage: python gen_workload_health_dashboard.py ocp-workload-health-dashboard.json
"""
import json
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "ocp-workload-health-dashboard.json"
UID = "ocp-workload-health"
OVERVIEW_UID = "ocp-cluster-risk-overview"
# 每個查詢的資料來源是多選的 ${datasource}，面板用 Mixed：Grafana 會對每個選到的叢集各跑一次，
# 結果合併在同一張表，再用 cluster 欄位區分
DS = {"type": "prometheus", "uid": "${datasource}"}
MIXED = {"type": "datasource", "uid": "-- Mixed --"}


def cl(expr):
    """Tags every series with the data source (cluster) name it came from."""
    return f'label_replace({expr}, "cluster", "${{datasource:text}}", "", "")'


S = 'namespace=~"$namespace", namespace!~"$exclude"'
NPC = "namespace, pod, container"
NP = "namespace, pod"
ND = "namespace, deployment"

# ---------------------------------------------------------------- conditions (one series per problem)
RESTARTS = f'kube_pod_container_status_restarts_total{{{S}}}'


def waiting(reasons):
    return (f'max by ({NPC}, reason) (kube_pod_container_status_waiting_reason'
            f'{{reason=~"{reasons}", {S}}} == 1)')


def restarted(w):
    return f'max by ({NPC}) (increase({RESTARTS}[{w}]) > 0)'


IMG = waiting("ErrImagePull|ImagePullBackOff|InvalidImageName")
UNSCHED = f'max by ({NP}) (kube_pod_status_unschedulable{{{S}}} == 1)'
UNAVAIL = f'max by ({ND}) (kube_deployment_status_replicas_unavailable{{{S}}} > 0)'
STUCK_ROLLOUT = (f'max by ({ND}) (kube_deployment_status_condition'
                 f'{{condition="Progressing", status="false", {S}}} == 1)')
# 容器在跑（不是 CrashLoop 等待中）但 readiness 不過，CrashLoop 歸第 7 類
NOT_READY = (f'max by ({NPC}) ((kube_pod_container_status_ready{{{S}}} == 0)'
             f' and on ({NP}) (kube_pod_status_phase{{phase="Running", {S}}} == 1)'
             f' and on ({NPC}) (kube_pod_container_status_running{{{S}}} == 1))')
RESTART_5M = restarted("5m")
RESTART_1H = restarted("1h")
# last_terminated_reason 會一直留著直到下一次終止，所以 OOM 只算 1 小時內有重啟過的
CRASH_OOM = (f'({waiting("CrashLoopBackOff")}) or (max by ({NPC}, reason) '
             f'(kube_pod_container_status_last_terminated_reason{{reason="OOMKilled", {S}}} == 1)'
             f' and on ({NPC}) {RESTART_1H})')
# ContainerCreating 只出現在 Pod 剛建立時，Pod 建立超過 5 分鐘還在 ContainerCreating 就是卡住
STUCK_CREATE = (f'max by ({NPC}) (kube_pod_container_status_waiting_reason'
                f'{{reason="ContainerCreating", {S}}} == 1) and on ({NP}) (time() - kube_pod_created{{{S}}} > 300)')
# 重啟明細：近 1 小時有重啟的容器，帶上「上次終止原因」
LAST_REASON = (f'max by ({NPC}, reason) (kube_pod_container_status_last_terminated_reason{{{S}}} == 1)')
RESTART_ROWS = (f'({RESTART_1H} * on ({NPC}) group_left (reason) {LAST_REASON})'
                f' or on ({NPC}) label_replace({RESTART_1H}, "reason", "—", "", "")')

# 資料來源完全沒有 kube-state-metrics 時顯示「沒資料」而不是綠色的 0
ALIVE = "0 * count(kube_namespace_created)"


def count(cond, by=None):
    inner = f'count by ({by}) ({cond})' if by else f'({cond})'
    return f'count({inner}) or ({ALIVE})'


def dur(cond_by_keys):
    """Minutes in the last hour the condition held (at most 60)."""
    return f'count_over_time(({cond_by_keys})[1h:1m])'


def strip_reason(cond, keys):
    return f'max by ({keys}) ({cond})'


# ---------------------------------------------------------------- layout helpers
panels = []
_id = [0]
_y = [0]
_open_row = [None]  # collapsed row that new panels go into


def nid():
    _id[0] += 1
    return _id[0]


def row(title):
    """A collapsed row: its panels only query when it is opened."""
    r = {"id": nid(), "type": "row", "title": title, "collapsed": True,
         "gridPos": {"h": 1, "w": 24, "x": 0, "y": _y[0]}, "panels": []}
    panels.append(r)
    _y[0] += 1
    _open_row[0] = r


class Line:
    def __init__(self):
        self.x = 0
        self.h = 0

    def add(self, panel, w, h):
        if self.x + w > 24:
            self.newline()
        panel["gridPos"] = {"h": h, "w": w, "x": self.x, "y": _y[0]}
        (_open_row[0]["panels"] if _open_row[0] else panels).append(panel)
        self.x += w
        self.h = max(self.h, h)

    def newline(self):
        _y[0] += self.h
        self.x = 0
        self.h = 0


EVENTS = "詳細原因請到 OCP console 查看該 Pod 的 Events（表格的 Pod 名稱可直接點過去）。"


def desc(what, how):
    return f"**這是什麼**：{what}\n\n**怎麼看**：{how}\n\n{EVENTS}"


RED_IF_ANY = [{"color": "green", "value": None}, {"color": "red", "value": 1}]


def text(line, content, w=24, h=4):
    line.add({"id": nid(), "type": "text", "title": "",
              "options": {"mode": "markdown", "content": content}}, w, h)


def matrix(line, cats, description, w=24, h=6):
    """One row per cluster, one column per problem type, plus the row total."""
    names = [c[1] for c in cats]
    line.add({
        "id": nid(), "type": "table", "title": "各叢集目前異常數（紅 = 有異常，點欄名可排序）",
        "description": description, "datasource": MIXED,
        "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": cl(expr), "legendFormat": "",
                     "range": False, "instant": True, "format": "table"} for i, (_, _, expr, _) in enumerate(cats)],
        "transformations": [
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True},
                "renameByName": {f"Value #{chr(65 + i)}": n for i, n in enumerate(names)},
                "indexByName": {"cluster": 0, **{f"Value #{chr(65 + i)}": i + 1 for i in range(len(names))}}}},
            {"id": "calculateField", "options": {"mode": "reduceRow", "reduce": {"reducer": "sum"},
                                                 "alias": "合計", "replaceFields": False}},
        ],
        "fieldConfig": {"defaults": {"unit": "none", "decimals": 0, "color": {"mode": "thresholds"},
                                     "thresholds": {"mode": "absolute", "steps": RED_IF_ANY},
                                     "custom": {"align": "center", "wrapHeaderText": True, "minWidth": 60,
                                                "cellOptions": {"type": "color-background", "mode": "basic"}}},
                        "overrides": [{"matcher": {"id": "byName", "options": "cluster"},
                                       "properties": [{"id": "displayName", "value": "叢集"},
                                                      {"id": "custom.width", "value": 220},
                                                      {"id": "custom.align", "value": "left"},
                                                      {"id": "custom.cellOptions", "value": {"type": "auto"}}]}]},
        "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "合計", "desc": True}],
                    "footer": {"show": False}},
    }, w, h)


def ts(line, title, expr, legend, description, w=24, h=8):
    line.add({
        "id": nid(), "type": "timeseries", "title": title, "description": description,
        "datasource": MIXED, "interval": "1m",
        "targets": [{"refId": "A", "datasource": DS, "expr": cl(expr), "legendFormat": legend,
                     "range": True, "instant": False}],
        "fieldConfig": {"defaults": {"unit": "none", "decimals": 0, "min": 0,
                                     "custom": {"drawStyle": "bars", "lineWidth": 1, "fillOpacity": 60,
                                                "showPoints": "never", "stacking": {"mode": "normal"}}},
                        "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["sum", "max"],
                               "sortBy": "Total", "sortDesc": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }, w, h)


CONSOLE = "topk(1, max by (url) (console_url))"
KEY_WIDTH = {"cluster": 170, "namespace": 150, "pod": 260, "container": 140, "reason": 150, "deployment": 220, "image": 260}
KEY_NAME = {"cluster": "叢集", "namespace": "Namespace", "pod": "Pod", "container": "Container", "reason": "原因",
            "deployment": "Deployment", "image": "Image"}


def link(kind):
    """OCP console Events tab of the pod / deployment. url comes from the console_url metric of the
    same cluster, so each cluster's table links to its own console."""
    return [{"title": "在 OCP console 看 Events",
             "url": "${__data.fields.url}/k8s/ns/${__data.fields.namespace}/" + kind + "/${__value.raw}/events",
             "targetBlank": True}]


def table(line, title, keys, base, columns, description, sort_by, w=24, h=6):
    """keys: label names of one row; base: query returning exactly those rows (labels = keys).
    columns: (name, expr, on, unit, decimals, text when missing). expr is keyed by `on` (a subset of
    keys) and is copied onto every base row with the same `on` labels; rows without a value show the
    text instead (stored as -1, nothing here is negative). Every query also carries the cluster's
    console url (when the console_url metric exists) for the links."""
    key_list = ["cluster"] + [k.strip() for k in keys.split(",")]
    bu = f'(({base}) * on () group_left (url) {CONSOLE}) or on ({keys}) ({base})'
    targets, overrides, rename, index = [], [], {}, {}
    for i, k in enumerate(key_list):
        index[k] = i
    for i, (name, expr, on, unit, decimals, na) in enumerate(columns):
        ref = chr(65 + i)
        e = f'(max by ({on}) ({expr}) * on ({on}) group_right () (0 * ({bu}) + 1)) or (0 * ({bu}) - 1)'
        targets.append({"refId": ref, "datasource": DS, "expr": cl(e), "legendFormat": "",
                        "range": False, "instant": True, "format": "table"})
        rename[f"Value #{ref}"] = name
        index[name] = len(key_list) + i
        props = [{"id": "unit", "value": unit},
                 {"id": "mappings", "value": [{"type": "value", "options": {
                     "-1": {"text": na, "color": "#8e8e8e", "index": 0}}}]}]
        if decimals is not None:
            props.append({"id": "decimals", "value": decimals})
        overrides.append({"matcher": {"id": "byName", "options": name}, "properties": props})
    for k in key_list:
        props = [{"id": "custom.width", "value": KEY_WIDTH[k]}, {"id": "custom.filterable", "value": True}]
        if k == "pod":
            props.append({"id": "links", "value": link("pods")})
        if k == "deployment":
            props.append({"id": "links", "value": link("deployments")})
        overrides.append({"matcher": {"id": "byName", "options": k},
                          "properties": props + [{"id": "displayName", "value": KEY_NAME[k]}]})
    # url 只給連結用：隱藏而不是 organize 排除，排除後 data link 讀不到
    overrides.append({"matcher": {"id": "byName", "options": "url"},
                      "properties": [{"id": "custom.hidden", "value": True}]})
    line.add({
        "id": nid(), "type": "table", "title": title, "description": description,
        "datasource": MIXED, "targets": targets,
        "transformations": [
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {"excludeByName": {"Time": True},
                                           "renameByName": rename, "indexByName": index}},
        ],
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "minWidth": 80, "wrapHeaderText": True},
                                     "noValue": "沒有異常"},
                        "overrides": overrides},
        "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": sort_by, "desc": True}],
                    "footer": {"show": False}},
    }, w, h)


# ---------------------------------------------------------------- categories
# (title, short name, count query, count description)
CATS = [
    ("1 Image 拉不下來", "1 Image", count(IMG),
     desc("容器在等待，原因是 ErrImagePull / ImagePullBackOff / InvalidImageName 的數量。",
          "常見：image 名稱或 tag 打錯、registry 認證（pull secret）失效、registry 連不到。")),
    ("2 排不進 Node", "2 排程失敗", count(UNSCHED),
     desc("Pending 且排程器判定無法排程（PodScheduled=False, Unschedulable）的 Pod 數。",
          "常見：資源不足（Insufficient cpu / memory）、nodeSelector / affinity / taint 不符、PVC 綁不到。")),
    ("3 Deployment 副本不足", "3 副本不足", count(UNAVAIL),
     desc("不可用副本 > 0 的 Deployment 數。",
          "滾動更新中短暫出現是正常的；一直不消失通常是 Pod 建不出來（quota、SCC、LimitRange）"
          "或建出來起不來（看第 1、5、7、8 類）。Pod 建不出來時 Pod 頁沒有 Events，要看 Deployment / ReplicaSet 的 Events。")),
    ("4 Rollout 卡住", "4 Rollout 卡住", count(STUCK_ROLLOUT),
     desc("Progressing 條件為 False 的 Deployment 數（超過 progressDeadlineSeconds，預設 10 分鐘，新版還沒好）。",
          "通常跟第 3 類一起出現；看 Deployment 的 Events 與新 ReplicaSet 的 Pod。")),
    ("5 Readiness 不過", "5 Readiness", count(NOT_READY),
     desc("Pod 是 Running、容器也在跑，但 Ready=false 的容器數（CrashLoop 不算在這，歸第 7 類）。",
          "Pod 剛啟動、還在 initialDelay 時會短暫出現；明細表的「持續」超過幾分鐘才需要處理。")),
    ("6 5 分鐘內有重啟", "6 重啟（5 分鐘內）", count(RESTART_5M),
     desc("過去 5 分鐘重啟次數 > 0 的容器數（liveness 失敗、程式自己結束、OOM 都會重啟）。",
          "明細表列近 1 小時有重啟的容器與上次終止原因；下方趨勢圖看是哪個 Namespace 一直在重啟。")),
    ("7 CrashLoop / OOM", "7 CrashLoop / OOM", count(CRASH_OOM, NPC),
     desc("CrashLoopBackOff 中，或上次終止是 OOMKilled 且 1 小時內有重啟的容器數。",
          "OOMKilled → 記憶體 Limit 太小或程式記憶體洩漏；CrashLoop → 看容器上一次的 log（oc logs --previous）。")),
    ("8 建立超過 5 分鐘", "8 建立卡住", count(STUCK_CREATE),
     desc("Pod 建立超過 5 分鐘，容器還在 ContainerCreating 的數量。",
          "常見：PVC 掛載失敗（FailedMount / FailedAttachVolume）、Secret / ConfigMap 不存在、CNI 網路配置失敗。")),
]

# ---------------------------------------------------------------- guide
L = Line()
text(L, """**OCP Workload 異常狀態（所有叢集）**　上表每列一個叢集，紅色 = 目前有異常；下表是所有叢集、所有類別的異常清單（資料延遲約 1–2 分鐘）。\
「叢集名稱篩選」填 regex（例如 `prod`、`dev|uat`）縮小範圍。**原因訊息在 OCP console 的 Events**，清單的「對象」可直接點過去；\
要看 image、Request、副本數、記憶體 Limit 等欄位，展開最下面的「各類明細」。""", h=2)
L.newline()

# ---------------------------------------------------------------- ① counters, one row per cluster
matrix(L, CATS, "每一列是一個叢集（資料來源），每一欄是一類異常的目前數量，紅色 = 有異常；依「合計」由大到小排序。"
       "\n\n叢集整列沒出現 = 這個資料來源查不到 kube-state-metrics。各類的定義看下方異常清單或「各類明細」表的說明。")
L.newline()

# ---------------------------------------------------------------- ② every problem of every cluster in one list
RESTART_5M_ROWS = (f'({RESTART_5M} * on ({NPC}) group_left (reason) {LAST_REASON})'
                   f' or on ({NPC}) {RESTART_5M}')
CRASH_ONLY = strip_reason(waiting("CrashLoopBackOff"), NPC)
# (short name, rows, labels the duration is keyed by, duration, object label, console page, reason when none)
ISSUES = [
    (CATS[0][1], IMG, NPC, dur(strip_reason(IMG, NPC)), "pod", "pods", ""),
    (CATS[1][1], UNSCHED, NP, dur(UNSCHED), "pod", "pods", "Unschedulable"),
    (CATS[2][1], UNAVAIL, ND, dur(UNAVAIL), "deployment", "deployments", "副本不足"),
    (CATS[3][1], STUCK_ROLLOUT, ND, dur(STUCK_ROLLOUT), "deployment", "deployments", "ProgressDeadlineExceeded"),
    (CATS[4][1], NOT_READY, NPC, dur(NOT_READY), "pod", "pods", "NotReady"),
    (CATS[5][1], RESTART_5M_ROWS, NPC, dur(RESTART_5M), "pod", "pods", "—"),
    # CrashLoop 算等待的分鐘數；只有 OOM 的算有重啟的分鐘數（不對 1h increase 做 subquery，太重）
    (CATS[6][1], CRASH_OOM, NPC, f"({dur(CRASH_ONLY)}) or ({dur(RESTART_5M)})", "pod", "pods", ""),
    (CATS[7][1], STUCK_CREATE, NPC, dur(STUCK_CREATE), "pod", "pods", "ContainerCreating"),
]
LIST_KEYS = "category, kind, namespace, object, container, reason"


def issue_rows(short, rows, on, duration, obj, kind, reason):
    e = f'(max by ({on}) ({duration}) * on ({on}) group_right () (0 * ({rows}) + 1)) or (0 * ({rows}) - 1)'
    e = f'label_replace({e}, "category", "{short}", "", "")'
    e = f'label_replace({e}, "object", "$1", "{obj}", "(.*)")'
    e = f'label_replace({e}, "kind", "{kind}", "", "")'
    if reason:
        e = f'label_replace({e}, "reason", "{reason}", "reason", "")'  # only when the row has no reason
    return f'max by ({LIST_KEYS}) ({e})'


# console 網址：沒有 console_url 時 vector(0) 墊底，topk(1) 保證只有一筆（console_url = 1 優先）
CONSOLE_OR_NONE = "topk(1, max by (url) (console_url) or vector(0))"
ISSUE_EXPR = " or ".join(f"({issue_rows(*i)})" for i in ISSUES)
ISSUE_EXPR = f"({ISSUE_EXPR}) + on () group_left (url) (0 * {CONSOLE_OR_NONE})"
LIST_COLS = [("cluster", "叢集", 170), ("category", "類別", 150), ("namespace", "Namespace", 160),
             ("object", "對象（Pod / Deployment）", 280), ("container", "Container", 140), ("reason", "原因", 170)]
L.add({
    "id": nid(), "type": "table", "title": "異常清單（所有叢集）",
    "description": desc("所有選到的叢集、8 類異常合在一張表，一列一個問題；類別編號對應上表的欄位。"
                        "**持續** = 近 1 小時內有幾分鐘處於這個狀態（最多 60；第 6 類是有重啟的分鐘數）。"
                        "第 6 類的原因是上次終止原因（Error、OOMKilled、Completed…）。",
                        "點欄名排序、點漏斗篩選；同一個容器可能同時出現在第 5、6、7 類。"
                        "第 3、4 類的對象是 Deployment，Pod 建不出來（quota、SCC）時要看 Deployment / ReplicaSet 的 Events。"),
    "datasource": MIXED,
    "targets": [{"refId": "A", "datasource": DS, "expr": cl(ISSUE_EXPR), "legendFormat": "",
                 "range": False, "instant": True, "format": "table"}],
    # 每個叢集回來一個 frame，merge 合成一張表
    "transformations": [{"id": "merge", "options": {}}, {"id": "organize", "options": {
        "excludeByName": {"Time": True},
        "renameByName": {"Value": "持續（近 1h 分鐘）"},
        "indexByName": {**{k: i for i, (k, _, _) in enumerate(LIST_COLS)}, "Value": len(LIST_COLS)}}}],
    "fieldConfig": {"defaults": {"custom": {"align": "auto", "minWidth": 80, "filterable": True}},
                    "overrides": [{"matcher": {"id": "byName", "options": k},
                                   "properties": [{"id": "displayName", "value": n}, {"id": "custom.width", "value": w}]}
                                  for k, n, w in LIST_COLS] + [
        # 第 2、3、4 類沒有 container
        {"matcher": {"id": "byName", "options": "container"}, "properties": [{"id": "noValue", "value": "—"}]},
        {"matcher": {"id": "byName", "options": "object"},
         "properties": [{"id": "links", "value": [{
             "title": "在 OCP console 看 Events", "targetBlank": True,
             "url": "${__data.fields.url}/k8s/ns/${__data.fields.namespace}/${__data.fields.kind}/${__value.raw}/events"}]}]},
        {"matcher": {"id": "byName", "options": "持續（近 1h 分鐘）"},
         "properties": [{"id": "unit", "value": "m"}, {"id": "decimals", "value": 0},
                        {"id": "mappings", "value": [{"type": "value", "options": {
                            "-1": {"text": "—", "color": "#8e8e8e", "index": 0}}}]}]},
        {"matcher": {"id": "byRegexp", "options": "^(url|kind)$"},
         "properties": [{"id": "custom.hidden", "value": True}]}]},
    "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "叢集", "desc": False}],
                "footer": {"show": False}},
}, 24, 10)
L.newline()

ts(L, "各 Namespace 重啟次數（每 5 分鐘）",
   f'sum by (namespace) (increase({RESTARTS}[5m])) > 0', "{{cluster}} / {{namespace}}",
   desc("每個時間點往前 5 分鐘內，該 Namespace 所有容器的重啟次數加總。",
        "持續有柱子的 Namespace 是一直在重啟；對照異常清單第 6、7 類找是哪個容器。"), h=7)
L.newline()

# ---------------------------------------------------------------- ③ per-type tables with extra columns (collapsed)
row("各類明細（多了 image、Request、副本數、記憶體 Limit 等欄位；展開才查詢）")
L = Line()
POD_AGE = f'time() - kube_pod_created{{{S}}}'
REQ = 'kube_pod_container_resource_requests{resource="%s", ' + S + '}'

table(L, "1 Image 拉不下來",
      f"{NPC}, reason, image",
      f'{IMG} * on ({NPC}) group_left (image) max by ({NPC}, image) (kube_pod_container_info{{{S}}})',
      [("持續（近 1h 分鐘）", dur(strip_reason(IMG, NPC)), NPC, "m", 0, "—"),
       ("Pod 建立多久", POD_AGE, NP, "s", 0, "—")],
      CATS[0][3], "持續（近 1h 分鐘）")
table(L, "2 排不進 Node", NP, UNSCHED,
      [("Pending 多久", POD_AGE, NP, "s", 0, "—"),
       ("CPU Request", f'sum by ({NP}) ({REQ % "cpu"})', NP, "none", 2, "未設"),
       ("記憶體 Request", f'sum by ({NP}) ({REQ % "memory"})', NP, "bytes", 1, "未設")],
      CATS[1][3], "Pending 多久")
L.newline()

DEP = f'{{{S}}}'
table(L, "3 Deployment 副本不足", ND, UNAVAIL,
      [("期望", f'kube_deployment_spec_replicas{DEP}', ND, "none", 0, "—"),
       ("可用", f'kube_deployment_status_replicas_available{DEP}', ND, "none", 0, "—"),
       ("不可用", f'kube_deployment_status_replicas_unavailable{DEP}', ND, "none", 0, "—"),
       ("持續（近 1h 分鐘）", dur(UNAVAIL), ND, "m", 0, "—")],
      CATS[2][3], "持續（近 1h 分鐘）")
table(L, "4 Rollout 卡住", ND, STUCK_ROLLOUT,
      [("期望", f'kube_deployment_spec_replicas{DEP}', ND, "none", 0, "—"),
       ("已更新", f'kube_deployment_status_replicas_updated{DEP}', ND, "none", 0, "—"),
       ("可用", f'kube_deployment_status_replicas_available{DEP}', ND, "none", 0, "—"),
       ("持續（近 1h 分鐘）", dur(STUCK_ROLLOUT), ND, "m", 0, "—")],
      CATS[3][3], "持續（近 1h 分鐘）")
L.newline()

table(L, "5 Readiness 不過", NPC, NOT_READY,
      [("持續（近 1h 分鐘）", dur(NOT_READY), NPC, "m", 0, "—"),
       ("1h 內重啟", f'increase({RESTARTS}[1h])', NPC, "none", 0, "—")],
      CATS[4][3], "持續（近 1h 分鐘）")
table(L, "6 近 1 小時有重啟", f"{NPC}, reason", RESTART_ROWS,
      [("5 分鐘內", f'increase({RESTARTS}[5m])', NPC, "none", 0, "—"),
       ("1 小時內", f'increase({RESTARTS}[1h])', NPC, "none", 0, "—"),
       ("累計", RESTARTS, NPC, "none", 0, "—")],
      desc("近 1 小時有重啟的容器；原因 = 上次終止原因（Error、OOMKilled、Completed…）。",
           "「5 分鐘內」> 0 的就是上方紅色計數裡的容器；只有「1 小時內」有數字的是剛剛重啟過、目前已恢復。"),
      "5 分鐘內")
L.newline()

table(L, "7 CrashLoop / OOM", f"{NPC}, reason", CRASH_OOM,
      [("1h 內重啟", f'increase({RESTARTS}[1h])', NPC, "none", 0, "—"),
       ("累計重啟", RESTARTS, NPC, "none", 0, "—"),
       ("記憶體 Limit", f'kube_pod_container_resource_limits{{resource="memory", {S}}}', NPC, "bytes", 1, "未設")],
      CATS[6][3], "1h 內重啟")
table(L, "8 建立超過 5 分鐘", NPC, STUCK_CREATE,
      [("Pod 建立多久", POD_AGE, NP, "s", 0, "—")],
      CATS[7][3], "Pod 建立多久")
L.newline()

# ---------------------------------------------------------------- dashboard
dash = {
    "uid": UID,
    "title": "OCP Workload 異常狀態（所有叢集）",
    "description": "用 kube-state-metrics 監控 Image pull、排程失敗、副本不足、Rollout 卡住、Readiness、重啟、CrashLoop / OOM、"
                   "ContainerCreating 卡住；所有叢集合併在同一張表。",
    "tags": ["ocp", "workload", "health", "multi-cluster"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 2,
    "editable": True,
    "graphTooltip": 1,
    "refresh": "1m",
    "time": {"from": "now-1h", "to": "now"},
    "links": [{"title": "叢集風險總覽", "type": "link", "icon": "dashboard",
               "url": "/d/" + OVERVIEW_UID + "/", "keepTime": True, "targetBlank": False}],
    "templating": {"list": [
        {"type": "textbox", "name": "ds_filter", "label": "叢集名稱篩選（regex）",
         "query": ".*", "current": {"text": ".*", "value": ".*"}, "hide": 0},
        # 只列名稱是 Prometheus…OCP 的資料來源，再用上面的篩選（名稱任一處符合即可）縮小。
        # 要用 :raw，Grafana 預設會把 regex 裡的變數跳脫（.* 變成 \.\*，什麼都比不到）
        {"type": "datasource", "name": "datasource", "label": "資料來源（叢集，可多選）",
         "query": "prometheus", "regex": "/^(?=.*(?:${ds_filter:raw}))Prometheus.*OCP$/",
         "multi": True, "includeAll": True,
         "current": {"text": ["All"], "value": ["$__all"]}, "hide": 0, "refresh": 1},
        # 跨叢集時 Namespace 清單各叢集不同，所以用 regex 文字框而不是下拉選單
        {"type": "textbox", "name": "namespace", "label": "Namespace（regex）",
         "query": ".*", "current": {"text": ".*", "value": ".*"}, "hide": 0},
        {"type": "textbox", "name": "exclude", "label": "排除的 Namespace（regex）",
         "query": "openshift.*|kube-.*",
         "current": {"text": "openshift.*|kube-.*", "value": "openshift.*|kube-.*"}, "hide": 0},
    ]},
    "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                              "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                              "name": "Annotations & Alerts", "type": "dashboard"}]},
    "panels": panels,
}

with open(OUT, "w", encoding="utf-8", newline="\n") as f:
    json.dump(dash, f, ensure_ascii=False, indent=2)
print("panels:", len(panels))
