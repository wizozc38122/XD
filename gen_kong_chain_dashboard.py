"""Generate the Kong → OCP Router → AP Pod latency chain Grafana dashboard.

Splits the time of one request into the parts each hop can measure, and works out the part nobody
measures (A10 / F5 / network between Kong and the Router) as the difference. Kong and OCP may be two
different Prometheus data sources, so cross-source differences use Grafana server-side expressions.

Usage: python gen_kong_chain_dashboard.py kong-router-ocp.json
"""
import json
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "kong-router-ocp.json"
DSK = {"type": "prometheus", "uid": "${kong_ds}"}
DSO = {"type": "prometheus", "uid": "${ocp_ds}"}
MIXED = {"type": "datasource", "uid": "-- Mixed --"}
EXPR = {"type": "__expr__", "uid": "__expr__", "name": "Expression"}

KS = 'service=~"$kong_service", route=~"$kong_route"'
RS = 'exported_namespace=~"$namespace", route=~"$router_route"'
POD = 'namespace=~"$namespace", pod=~"$deployment-[a-z0-9]+-[a-z0-9]+"'
APP = POD + ', uri!~"/actuator.*"'
CT = POD + ', container!="", container!="POD"'
RI = "$__rate_interval"
W5 = "5m"  # stats: the last 5 minutes before the end of the time range


# ---------------------------------------------------------------- queries
def k_avg(metric, w):
    """Kong average in ms from a histogram (sum ÷ count), comparable with the Router's averages."""
    return f'sum(rate({metric}_sum{{{KS}}}[{w}])) / (sum(rate({metric}_count{{{KS}}}[{w}])) > 0)'


def k_pct(q, metric, w, by=""):
    return f'histogram_quantile({q}, sum by (le{by}) (rate({metric}_bucket{{{KS}}}[{w}])))'


K_TOTAL, K_UP, K_SELF = "kong_request_latency_ms", "kong_upstream_latency_ms", "kong_kong_latency_ms"


def r_rps_by(w):
    return f'sum by (route, pod) (rate(haproxy_backend_http_responses_total{{{RS}}}[{w}]))'


def r_avg(kind, w):
    """Router average in ms. Each router pod keeps its own average per route (over its last 1024
    requests), so they are weighted by how many responses each router pod served.
    The gauge is only a snapshot of "now": for a longer window (5m, $__range) take it every minute and
    weight each minute by its responses, so it covers the same time as Kong's average over that window."""
    gauge = f'max by (route, pod) (haproxy_backend_http_average_{kind}_latency_milliseconds{{{RS}}})'
    if w in (RI, "1m"):
        return f'sum({gauge} * on (route, pod) {r_rps_by(w)}) / (sum({r_rps_by(w)}) > 0)'
    return (f'sum_over_time((sum({gauge} * on (route, pod) {r_rps_by("1m")}))[{w}:1m])'
            f' / (sum_over_time((sum({r_rps_by("1m")}))[{w}:1m]) > 0)')


def app_avg(w):
    return (f'1000 * sum(rate(http_server_requests_seconds_sum{{{APP}}}[{w}]))'
            f' / (sum(rate(http_server_requests_seconds_count{{{APP}}}[{w}])) > 0)')


def app_pct(q, w, by=""):
    """App percentile in ms; needs the Micrometer histogram (percentiles-histogram) to be enabled."""
    return f'1000 * histogram_quantile({q}, sum by (le{by}) (rate(http_server_requests_seconds_bucket{{{APP}}}[{w}])))'


def kong_sent(w):
    """Requests Kong sent upstream: answered by the upstream, plus the ones Kong had to answer itself
    because the upstream did not (502/503/504). Without a source label (Kong < 3) every request counts."""
    return (f'sum(rate(kong_http_requests_total{{{KS}, source=~"service|"}}[{w}]))'
            f' + (sum(rate(kong_http_requests_total{{{KS}, source="kong", code=~"502|503|504"}}[{w}])) or vector(0))')


def router_recv(w):
    return f'sum(rate(haproxy_backend_http_responses_total{{{RS}}}[{w}]))'


def app_recv(w):
    return f'sum(rate(http_server_requests_seconds_count{{{APP}}}[{w}]))'


# ---- 逾時:Kong / Router 的逾時設定不在指標裡,改看實際等了多久 (Kong 直方圖的格子) 跟 504 一起對照


def over_timeout(secs, w, fn="rate"):
    """Kong 等後端回應超過 secs 秒的請求 (每秒,或 fn=increase 時的筆數)。secs 要剛好是直方圖的分界 (Kong 預設有 5、10、30、60 秒)。
    同一組直方圖裡 +Inf 格減掉 secs 那一格,每個 series 先各自相減再加總 (分開加總再相減,series 出現 / 消失的時間不同會算出負數),
    最後小於 0 的歸零。"""
    b = f"{K_UP}_bucket"
    return (f'clamp_min(sum({fn}({b}{{{KS}, le="+Inf"}}[{w}]) - ignoring (le) '
            f'{fn}({b}{{{KS}, le=~"0*{secs}000([.]0+)?"}}[{w}])), 0)')


APP_MAX = f"1000 * max(http_server_requests_seconds_max{{{APP}}})"
ROUTER_MAX_POD = f"max(haproxy_backend_http_average_response_latency_milliseconds{{{RS}}})"


def pos(x):
    """Server-side expression: x when positive, else 0."""
    return f"({x}) * (({x}) > 0)"


# ---------------------------------------------------------------- panel helpers
_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def K(expr, legend="", ref="A", hide=False, instant=False):
    return {"refId": ref, "datasource": DSK, "expr": expr, "legendFormat": legend, "range": not instant,
            "instant": instant, "editorMode": "code", "hide": hide}


def O(expr, legend="", ref="A", hide=False, instant=False):
    return {"refId": ref, "datasource": DSO, "expr": expr, "legendFormat": legend, "range": not instant,
            "instant": instant, "editorMode": "code", "hide": hide}


def M(expression, ref):
    return {"refId": ref, "datasource": EXPR, "type": "math", "expression": expression, "hide": False}


def name_ref(ref, name, extra=()):
    """Display name (and more properties) for the series of one query, e.g. an expression."""
    return {"matcher": {"id": "byFrameRefID", "options": ref},
            "properties": [{"id": "displayName", "value": name}] + list(extra)}


DASHED = [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
          {"id": "custom.fillOpacity", "value": 0}, {"id": "custom.lineWidth", "value": 2},
          {"id": "custom.stacking", "value": {"mode": "none", "group": "A"}}]


# 百分位數碰到直方圖最大一格時,Prometheus 只能回那一格的上限 (Kong 60 秒、App / Micrometer 30 秒),
# 實際比這更久、量不到;剛好等於上限就顯示「≥ 上限」,避免誤會成剛好量到 1 分鐘
CAP_MAPPINGS = [{"type": "value", "options": {
    "60000": {"text": "≥ 60 秒（上限）", "color": "red", "index": 10},
    "30000": {"text": "≥ 30 秒（上限）", "color": "red", "index": 11}}}]
SEC = "suffix: 秒"
CAP_MAPPINGS_S = [{"type": "value", "options": {
    "60": {"text": "≥ 60 秒（上限）", "color": "red", "index": 10},
    "30": {"text": "≥ 30 秒（上限）", "color": "red", "index": 11}}}]


def desc(what, how):
    return f"{what}\n\n**怎麼看**：{how}"


def ds_of(targets):
    sources = {json.dumps(t["datasource"], sort_keys=True) for t in targets}
    return targets[0]["datasource"] if len(sources) == 1 else MIXED


def ts(title, d, targets, unit="ms", overrides=(), stack=False, fill=10, thresholds=None, no_value=None, log=False,
       rows=None):
    """rows: 圖例大概有幾列 (預設:每個顯示的查詢一列,依 label 拆的算 3 列),Board.line 用來把面板撐高到圖例放得下。"""
    if log and unit == "ms" and all("expr" in t for t in targets):
        # 對數刻度的格線在 10 的次方;用 ms 或 s 單位時 100 秒那條會被換算成「1.67 mins」,看起來像有請求跑了 1.67 分鐘。
        # 改用固定後綴「秒」的單位,不會被換算成分鐘。
        targets = [dict(t, expr=f"({t['expr']}) / 1000") for t in targets]
        thresholds = [dict(st, value=None if st["value"] is None else st["value"] / 1000) for st in thresholds or []] or None
        unit = SEC
    custom = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 35 if stack else fill, "showPoints": "never",
              "spanNulls": True, "axisSoftMin": 0,
              "stacking": {"mode": "normal" if stack else "none", "group": "A"},
              "thresholdsStyle": {"mode": "dashed" if thresholds else "off"}}
    if log:
        custom["scaleDistribution"] = {"type": "log", "log": 10}
        del custom["axisSoftMin"]
    if unit == "percentunit":
        custom["axisSoftMax"] = 1
    defaults = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"}}
    if unit == "ms":
        defaults["mappings"] = CAP_MAPPINGS
    elif unit == SEC:
        defaults["mappings"] = CAP_MAPPINGS_S
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
    if no_value:
        defaults["noValue"] = no_value
    p = {"id": nid(), "type": "timeseries", "title": title, "description": d, "datasource": ds_of(targets),
         "targets": targets, "fieldConfig": {"defaults": defaults, "overrides": list(overrides)},
         "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max", "lastNotNull"],
                                "showLegend": True},
                     "tooltip": {"mode": "multi", "sort": "desc"}}}
    p["_rows"] = rows or sum(3 if "{{" in t.get("legendFormat", "") else 1 for t in targets if not t.get("hide"))
    if p["_rows"] > 5:
        p["options"]["legend"].update(placement="right", calcs=["mean", "lastNotNull"])
        p["_rows"] = 0
    if p["datasource"] == MIXED:
        p["interval"] = "1m"  # 兩個資料來源用一樣的時間點,expression 才對得起來
    return p


def bar(title, d, targets, overrides=(), unit="ms"):
    p = {"id": nid(), "type": "bargauge", "title": title, "description": d, "datasource": ds_of(targets),
         "targets": targets,
         "fieldConfig": {"defaults": {"unit": unit, "min": 0, "decimals": 0, "color": {"mode": "palette-classic"},
                                      "noValue": "—", "mappings": CAP_MAPPINGS if unit == "ms" else []},
                         "overrides": list(overrides)},
         "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True, "valueMode": "text",
                     "namePlacement": "top", "sizing": "auto", "text": {"titleSize": 13, "valueSize": 15},
                     "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}
    return p


def table(title, d, keys, cols, sort, key_width=140):
    """One row per keys; cols: (name, query target, field properties). Every target is an instant query."""
    targets, rename, over = [], {k: n for k, n in keys}, []
    for t, (name, target, props) in zip("ABCDEFGH", cols):
        target = dict(target, refId=t, instant=True, range=False, format="table")
        targets.append(target)
        rename[f"Value #{t}"] = name
        if props:
            over.append({"matcher": {"id": "byName", "options": name}, "properties": props})
    order = [k for k, _ in keys] + [f"Value #{t}" for t in "ABCDEFGH"[:len(cols)]]
    return {"id": nid(), "type": "table", "title": title, "description": d, "datasource": ds_of(targets),
            "targets": targets,
            "transformations": [{"id": "merge", "options": {}},
                                {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": rename,
                                                               "indexByName": {k: i for i, k in enumerate(order)}}},
                                {"id": "sortBy", "options": {"sort": [{"field": sort, "desc": True}]}}],
            "fieldConfig": {"defaults": {"unit": "ms", "decimals": 0, "mappings": CAP_MAPPINGS,
                                         "custom": {"align": "auto", "minWidth": 90, "wrapHeaderText": True}},
                            "overrides": [{"matcher": {"id": "byName", "options": n}, "properties": [
                                {"id": "custom.minWidth", "value": key_width}]} for _, n in keys] + over},
            "options": {"showHeader": True}}


def text(content):
    return {"id": nid(), "type": "text", "title": "", "options": {"mode": "markdown", "content": content}}


class Board:
    """row() starts a section; line() lays panels side by side across the full width."""

    def __init__(self):
        self.panels, self.y, self.inner, self.iy = [], 0, None, 0

    def row(self, title, collapsed=False):
        r = {"id": nid(), "type": "row", "title": title, "collapsed": collapsed,
             "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []}
        self.panels.append(r)
        self.y += 1
        self.inner, self.iy = (r["panels"], self.y) if collapsed else (None, 0)

    def line(self, *ps, h=9, widths=None):
        widths = widths or [24 // len(ps)] * len(ps)
        # 圖例是表格放在下面,每列約 24px;撐高到圖例 + 至少 180px 的圖都放得下 (一格高約 38px)
        rows = max([min(p.pop("_rows", 0), 8) for p in ps])
        h = max(h, -(-(40 + 180 + 24 * (rows + 1) + 8) // 38) if rows else 0)
        y, x = (self.iy if self.inner is not None else self.y), 0
        for p, w in zip(ps, widths):
            p["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
            x += w
            (self.inner if self.inner is not None else self.panels).append(p)
        if self.inner is not None:
            self.iy += h
        else:
            self.y += h


# ---------------------------------------------------------------- dashboard
B = Board()
B.line(text("""**Kong → Router → AP 延遲鏈路**　`Client →（A10/F5?）→ Kong →（A10/F5 / 網路）→ OCP Router → AP Pod`
- **篩選**：「名稱關鍵字」輸入共同名稱（例如 `order`），名稱不像的例外用 `|` 加（`order|ord-api`）。**A10 / F5 沒有指標**，用 Kong 和 Router 的差值推算。
- **區塊**：**A** 現在 → **B** 整段平均 → **C** 整段慢請求 → **D** 逾時 → ②～⑤ 各段細節；B～D 跟著上方時間，拖曳選壓測時段可回看。"""), h=3)

GUIDE = """| 看到 | 可能是哪段 / 原因 |
|---|---|
| **Kong 自己處理** 變厚 | Kong plugin（認證、rate limit、log）或 Kong 節點資源；看 ② 各 Kong 節點 |
| **Kong ↔ Router 之間** 變厚，Router 各段正常 | A10 / F5、網路、TLS 握手、DNS：查 LB 的連線數、SNAT port、health check、TLS 設定 |
| **Kong 送出 > Router 收到** | 請求在中間被擋或逾時，沒進到 Router；Kong 會出現自己回的 502 / 504 |
| **Router 收到 > Kong 送出** | 有其他來源直接打 Router（內部服務、繞過 Kong、LB health check） |
| **Router 排隊等 Pod** 高 | Pod 數不夠或 Pod 的 maxconn 太小，請求在 Router 等 |
| **Router 連線** 高 | Router 連不太上 Pod：Pod 網路、SDN、Pod 忙到 accept 不了 |
| **Router 等 Pod 回應** 高、App 自己處理也高 | App 慢：看 ④ CPU throttling、GC、DB、下游服務 |
| **Router 等 Pod 回應** 高、App 自己處理不高 | 請求在 Pod 裡排隊（thread pool 滿）或 Pod 網路慢 |
| 只有某台 Kong / Router Pod / AP Pod 慢 | 那台機器的 Node、LB 分配不均、那個 Pod 的資料或 GC |
| Kong 等後端回應 p95 / p99 卡在 30 秒 / 60 秒 | 碰到逾時（Router 預設 30 秒、Kong 預設 60 秒），誰卡住看 D |

**注意**：Router 的平均是每個 Router Pod 最近 1024 筆請求的平均，流量很小時會反應比較慢。要追單筆請求，用 X-Request-ID 串 Kong、Router、App 的 log。"""
B.row("看到什麼 → 哪段有問題（判讀表，點開）", collapsed=True)
B.line(text(GUIDE), h=12)

# ---- 拓樸圖 (Canvas)


# ---- 拓樸圖 (Canvas):每個方塊 = 背景 + 標題 + 「名稱 值」好幾列;位置都是面板的百分比
def c_el(name, typ, box, text=None, size=13, color="#cbd5e1", align="left", bg=None, border=None, dash=False, conns=()):
    left, top, right, bottom = box
    e = {"type": typ, "name": name, "constraint": {"horizontal": "scale", "vertical": "scale"},
         "placement": {"left": left, "top": top, "right": right, "bottom": bottom},
         "config": {"align": align, "valign": "middle", "size": size,
                    "color": color if isinstance(color, dict) else {"fixed": color}},
         "background": {"color": {"fixed": bg or "transparent"}},
         "border": {"color": {"fixed": border or "transparent"}, "width": 2 if border else 0, "radius": 10},
         "connections": list(conns)}
    if text is not None:
        e["config"]["text"] = text
    if dash:
        e["border"]["color"] = {"fixed": border}
    return e


def c_conn(target, sx=1, sy=0, tx=-1, ty=0, color="#94a3b8"):
    return {"source": {"x": sx, "y": sy}, "target": {"x": tx, "y": ty}, "targetName": target, "path": "straight",
            "color": {"fixed": color}, "size": {"fixed": 2, "min": 1, "max": 10}}


def c_node(key, x0, x1, title, sub, rows, top=6, bottom=30, bg="#1e293b", border="#475569", conns=()):
    """rows: [(標籤, 欄位名稱)];欄位名稱 = 查詢的顯示名稱,值跟著那個欄位的單位和門檻顏色。"""
    right = 100 - x1
    h = 100 - top - bottom
    tf = 0.14 if sub else 0.4   # 標題佔方塊高度的比例
    out = [c_el(key, "rectangle", (x0, top, right, bottom), {"mode": "fixed", "fixed": ""}, bg=bg, border=border,
                conns=conns),
           c_el(key + "-t", "text", (x0 + 0.6, top + 2, right + 0.6, 100 - top - 2 - h * tf),
                {"mode": "fixed", "fixed": title}, size=15, color="#f8fafc")]
    if sub:
        out.append(c_el(key + "-s", "text", (x0 + 0.6, top + 2 + h * 0.13, right + 0.6, 100 - top - 2 - h * 0.24),
                        {"mode": "fixed", "fixed": sub}, size=11, color="#94a3b8"))
    y = top + 2 + h * (0.27 if sub else 0.45)
    step = (h * (0.70 if sub else 0.5)) / max(len(rows), 4 if sub else 1)
    for i, (label, field) in enumerate(rows):
        b = 100 - y - step
        out.append(c_el(f"{key}-l{i}", "text", (x0 + 0.6, y, right + (x1 - x0) * 0.38, b), {"mode": "fixed", "fixed": label}))
        out.append(c_el(f"{key}-v{i}", "metric-value", (x0 + (x1 - x0) * 0.6, y, right + 0.6, b),
                        {"mode": "field", "field": field, "fixed": "—"}, color={"field": field, "fixed": "#e2e8f0"},
                        align="right"))
        y += step
    return out


RED_30S = [(30000, "orange"), (55000, "red")]
NO_DATA_MAPPINGS = [{"type": "value", "options": {"-1": {"text": "沒資料", "color": "#64748b", "index": 0}}},
                    {"type": "special", "options": {"match": "nan", "result": {"text": "沒資料", "color": "#64748b", "index": 1}}}]


def t_override(ref, name, unit, steps):
    props = [{"id": "displayName", "value": name}, {"id": "unit", "value": unit},
             {"id": "decimals", "value": 0 if unit in ("none", "short") else 1}]
    if steps:
        props += [{"id": "color", "value": {"mode": "thresholds"}},
                  {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "#e2e8f0", "value": None}]
                                                 + [{"color": c, "value": v} for v, c in steps]}}]
    return {"matcher": {"id": "byFrameRefID", "options": ref}, "properties": props}


def fb(expr):
    """顯示用:沒資料時 Canvas 會顯示 Field not found,補 -1 (對應成「沒資料」)。"""
    return f"({expr}) or on() vector(-1)"


def nan(expr):
    """算差值用:補 NaN,缺一個就算出 NaN (一樣顯示「沒資料」),不會把 -1 拿去算。"""
    return f"({expr}) or on() vector(NaN)"


def worst(expr, w):
    """時間範圍內每分鐘看一次,取最高。"""
    return f"max_over_time(({expr})[{w}:1m])"


def topo_canvas(title, d, targets, fields, nodes):
    """fields: {refId: (顯示名稱, 單位, 門檻)};nodes 用顯示名稱綁欄位。"""
    return {"id": nid(), "type": "canvas", "title": title, "description": d, "datasource": MIXED, "targets": targets,
            "interval": "1m",
            "fieldConfig": {"defaults": {"color": {"mode": "fixed", "fixedColor": "#e2e8f0"}, "noValue": "—",
                                         "mappings": NO_DATA_MAPPINGS + CAP_MAPPINGS},
                            "overrides": [t_override(r, *f) for r, f in fields.items()]},
            "options": {"inlineEditing": False, "showAdvancedTypes": True, "panZoom": False, "infinitePan": False,
                        "root": {"type": "frame", "name": "root", "elements": nodes,
                                 "background": {"color": {"fixed": "transparent"}},
                                 "border": {"color": {"fixed": "transparent"}},
                                 "constraint": {"horizontal": "left", "vertical": "top"}, "placement": {}, "config": {}}}}


def front_nodes(bottom=30):
    return (c_node("client", 0.5, 8.5, "Client", "外部 / 內部呼叫", [], bottom=bottom, conns=[c_conn("lb1")])
            + c_node("lb1", 11, 19, "A10 / F5 ?", "沒有指標", [], bottom=bottom, bg="#111827", border="#64748b",
                     conns=[c_conn("kong")]))


def topology(w, when):
    """平均版:每一段的請求數、平均時間、錯誤。w = 5m (看現在) 或 $__range (整個時間範圍)。"""
    fields = {
        "KR": ("Kong 收到", "reqps", None),
        "A": ("Kong 自己處理", "ms", [(50, "orange"), (200, "red")]),
        "KP": ("Kong 等後端回應 p99", "ms", RED_30S),
        "K5": ("Kong 自己回的 5xx", "reqps", [(0.01, "red")]),
        "B": ("Kong ↔ Router 之間", "ms", [(100, "orange"), (300, "red")]),
        "L": ("Kong 送了、Router 沒收到", "reqps", [(0.5, "red")]),
        "RR": ("Router 收到", "reqps", None),
        "C": ("Router 排隊等 Pod", "ms", [(10, "orange"), (100, "red")]),
        "D": ("Router 連線到 Pod", "ms", [(10, "orange"), (100, "red")]),
        "E": ("Router 等 Pod 回應", "ms", None),
        "R5": ("Router 回的 5xx", "reqps", [(0.01, "red")]),
        "O": ("Router 收到、不是 Kong 來的", "reqps", [(0.5, "orange")]),
        "AR": ("App 收到", "reqps", None),
        "F": ("App 自己處理", "ms", None),
        "AM": ("App 最慢的一筆", "ms", RED_30S),
        "PR": ("Ready Pod（最少）", "none", None),
        "A5": ("App 回的 5xx", "reqps", [(0.01, "red")]),
    }
    targets = [
        K(nan(k_avg(K_UP, w)), "", "X", hide=True, instant=True),
        K(nan(kong_sent(w)), "", "HKS", hide=True, instant=True),
        O(nan(router_recv(w)), "", "HRR", hide=True, instant=True),
        O(nan(r_avg("queue", w)), "", "HC", hide=True, instant=True),
        O(nan(r_avg("connect", w)), "", "HD", hide=True, instant=True),
        O(nan(r_avg("response", w)), "", "HE", hide=True, instant=True),
        K(fb(f'sum(rate(kong_http_requests_total{{{KS}}}[{w}]))'), "", "KR", instant=True),
        K(fb(k_avg(K_SELF, w)), "", "A", instant=True),
        K(fb(k_pct(0.99, K_UP, w)), "", "KP", instant=True),
        K(f'sum(rate(kong_http_requests_total{{{KS}, source="kong", code=~"5.."}}[{w}])) or on() vector(0)', "", "K5", instant=True),
        M(pos("$X - ($HC + $HD + $HE)"), "B"),
        M(pos("$HKS - $HRR"), "L"),
        O(fb(router_recv(w)), "", "RR", instant=True),
        O(fb(r_avg("queue", w)), "", "C", instant=True),
        O(fb(r_avg("connect", w)), "", "D", instant=True),
        O(fb(r_avg("response", w)), "", "E", instant=True),
        O(f'sum(rate(haproxy_backend_http_responses_total{{{RS}, code=~"5.*"}}[{w}])) or on() vector(0)', "", "R5", instant=True),
        M(pos("$HRR - $HKS"), "O"),
        O(fb(app_recv(w)), "", "AR", instant=True),
        O(fb(app_avg(w)), "", "F", instant=True),
        O(fb(worst(APP_MAX, w)), "", "AM", instant=True),
        O(fb(f'min_over_time((sum(kube_pod_status_ready{{{POD}, condition="true"}}))[{w}:1m])'), "", "PR", instant=True),
        O(f'sum(rate(http_server_requests_seconds_count{{{APP}, status=~"5.."}}[{w}])) or on() vector(0)', "", "A5", instant=True),
    ]
    F = {r: f[0] for r, f in fields.items()}
    nodes = front_nodes()
    nodes += c_node("kong", 22, 40, "Kong", when, [
        ("收到", F["KR"]), ("自己處理（平均）", F["A"]), ("等後端回應 p99", F["KP"]), ("自己回的 5xx", F["K5"])],
        conns=[c_conn("lb2")])
    nodes += c_node("lb2", 43, 57, "A10 / F5 / 網路", "沒有指標，用差值推算", [
        ("多花的時間", F["B"]), ("沒到 Router", F["L"])], bg="#111827", border="#64748b", conns=[c_conn("router")])
    nodes += c_node("router", 60, 78, "OCP Router", "HAProxy，只有平均", [
        ("收到", F["RR"]), ("排隊等 Pod", F["C"]), ("連線到 Pod", F["D"]), ("等 Pod 回應", F["E"]), ("回的 5xx", F["R5"])],
        conns=[c_conn("app")])
    nodes += c_node("app", 81, 99.5, "AP Pod", "Micrometer", [
        ("收到", F["AR"]), ("自己處理（平均）", F["F"]), ("最慢的一筆", F["AM"]), ("Ready Pod（最少）", F["PR"]),
        ("回的 5xx", F["A5"])])
    nodes += c_node("other", 60, 78, "其他來源", "", [("不是從 Kong 來的", F["O"])], top=72, bottom=1,
                    bg="#111827", border="#64748b", conns=[c_conn("router", 0, 1, 0, -1)])
    return targets, fields, nodes


def topology_tail(w):
    """慢請求版:p95 / p99 和時間範圍內最差的那一分鐘。Router 沒有百分位數,用每分鐘平均的最高值。"""
    fields = {
        "OVR": ("等後端超過 30 秒的請求", "short", [(1, "red")]),
        "T99": ("總耗時 p99", "ms", RED_30S),
        "U95": ("等後端回應 p95", "ms", RED_30S),
        "U99": ("等後端回應 p99", "ms", RED_30S),
        "UW": ("等後端回應 p99 最慢的一分鐘", "ms", RED_30S),
        "S99": ("Kong 自己處理 p99", "ms", [(100, "orange"), (500, "red")]),
        "GAP99": ("App 以外 p99（估算）", "ms", [(500, "orange"), (3000, "red")]),
        "GW": ("Kong ↔ Router 之間 最慢的一分鐘", "ms", [(100, "orange"), (300, "red")]),
        "LW": ("沒到 Router 最多的一分鐘", "reqps", [(0.5, "red")]),
        "RW": ("Router 等 Pod 回應 最高", "ms", RED_30S),
        "QW": ("Router 排隊等 Pod 最高", "ms", [(10, "orange"), (100, "red")]),
        "CW": ("Router 連線到 Pod 最高", "ms", [(10, "orange"), (100, "red")]),
        "R5N": ("Router 回的 5xx 總數", "short", [(1, "red")]),
        "P95": ("App p95", "ms", RED_30S),
        "P99": ("App p99", "ms", RED_30S),
        "AMW": ("App 最慢的一筆 最高", "ms", RED_30S),
        "THW": ("App 執行緒忙碌 最高", "percentunit", [(0.7, "orange"), (0.9, "red")]),
        "GCW": ("GC 最長停頓", "ms", [(500, "orange"), (2000, "red")]),
        "A5N": ("App 回的 5xx 總數", "short", [(1, "red")]),
    }
    reduce_max = lambda ref, expr: {"refId": ref, "datasource": EXPR, "type": "reduce", "expression": expr,
                                    "reducer": "max", "settings": {"mode": "dropNN"}, "hide": False}
    targets = [
        # 每分鐘的 Kong ↔ Router 之間 / 沒到 Router (兩個資料來源相減,再取最高)
        K(nan(k_avg(K_UP, "1m")), "", "GX", hide=True),
        O(nan(r_avg("queue", "1m")), "", "GC", hide=True),
        O(nan(r_avg("connect", "1m")), "", "GD", hide=True),
        O(nan(r_avg("response", "1m")), "", "GE", hide=True),
        K(nan(kong_sent("1m")), "", "GKS", hide=True),
        O(nan(router_recv("1m")), "", "GRR", hide=True),
        dict(M(pos("$GX - ($GC + $GD + $GE)"), "GS"), hide=True),
        dict(M(pos("$GKS - $GRR"), "LS"), hide=True),
        reduce_max("GW", "GS"),
        reduce_max("LW", "LS"),
        K(nan(k_pct(0.99, K_UP, w)), "", "HU99", hide=True, instant=True),
        O(nan(app_pct(0.99, w)), "", "HP99", hide=True, instant=True),
        M(pos("$HU99 - $HP99"), "GAP99"),
        K(fb(over_timeout("30", w, "increase")), "", "OVR", instant=True),
        K(fb(k_pct(0.99, K_TOTAL, w)), "", "T99", instant=True),
        K(fb(k_pct(0.95, K_UP, w)), "", "U95", instant=True),
        K(fb(k_pct(0.99, K_UP, w)), "", "U99", instant=True),
        K(fb(worst(k_pct(0.99, K_UP, "1m"), w)), "", "UW", instant=True),
        K(fb(k_pct(0.99, K_SELF, w)), "", "S99", instant=True),
        O(fb(worst(ROUTER_MAX_POD, w)), "", "RW", instant=True),
        O(fb(worst(f"max(haproxy_backend_http_average_queue_latency_milliseconds{{{RS}}})", w)), "", "QW", instant=True),
        O(fb(worst(f"max(haproxy_backend_http_average_connect_latency_milliseconds{{{RS}}})", w)), "", "CW", instant=True),
        O(f'sum(increase(haproxy_backend_http_responses_total{{{RS}, code=~"5.*"}}[{w}])) or on() vector(0)', "", "R5N",
          instant=True),
        O(fb(app_pct(0.95, w)), "", "P95", instant=True),
        O(fb(app_pct(0.99, w)), "", "P99", instant=True),
        O(fb(worst(APP_MAX, w)), "", "AMW", instant=True),
        O(fb(worst(f"max(max by (pod) (tomcat_threads_busy_threads{{{POD}}}) "
                   f"/ (max by (pod) (tomcat_threads_config_max_threads{{{POD}}}) > 0))", w)), "", "THW", instant=True),
        O(fb(worst(f"1000 * max(jvm_gc_pause_seconds_max{{{POD}}})", w)), "", "GCW", instant=True),
        O(f'sum(increase(http_server_requests_seconds_count{{{APP}, status=~"5.."}}[{w}])) or on() vector(0)', "", "A5N",
          instant=True),
    ]
    F = {r: f[0] for r, f in fields.items()}
    nodes = front_nodes(bottom=5)
    nodes += c_node("kong", 22, 40, "Kong", "整個時間範圍", [
        ("等後端回應 p95", F["U95"]), ("等後端回應 p99", F["U99"]), ("p99 最慢的一分鐘", F["UW"]),
        ("總耗時 p99", F["T99"]), ("自己處理 p99", F["S99"]), ("超過 30 秒（筆）", F["OVR"])],
        bottom=5, conns=[c_conn("lb2")])
    nodes += c_node("lb2", 43, 57, "A10 / F5 / 網路", "沒有指標，用差值推算", [
        ("App 以外 p99", F["GAP99"]), ("多花（最慢一分鐘）", F["GW"]), ("沒到（最多一分鐘）", F["LW"])],
        bottom=5, bg="#111827", border="#64748b", conns=[c_conn("router")])
    nodes += c_node("router", 60, 78, "OCP Router", "只有平均：每分鐘平均的最高值", [
        ("等 Pod 回應 最高", F["RW"]), ("排隊等 Pod 最高", F["QW"]), ("連線到 Pod 最高", F["CW"]), ("回的 5xx 總數", F["R5N"])],
        bottom=5, conns=[c_conn("app")])
    nodes += c_node("app", 81, 99.5, "AP Pod", "p95 / p99 需開 histogram", [
        ("p95", F["P95"]), ("p99", F["P99"]), ("最慢的一筆", F["AMW"]), ("執行緒忙碌 最高", F["THW"]),
        ("GC 最長停頓", F["GCW"]), ("回的 5xx 總數", F["A5N"])], bottom=5)
    return targets, fields, nodes


def segments(w, inst):
    """依請求經過的順序:Kong 自己處理 → Kong ↔ Router 之間 → Router 排隊等 Pod → 連線到 Pod → 等 Pod 回應,
    再加兩條參考線 (App 自己處理、總耗時)。Grafana 依 refId 排序,所以 refId 照這個順序編;X 是只拿來算差值的隱藏查詢。
    總耗時 = Kong 自己處理 + 等後端回應:只算有送到後端的請求,跟各段同一群請求、同一段時間、依請求數加權
    (Kong 的 request latency 包含 Kong 自己擋下的 401 / 429,會被拉低,不能拿來跟各段比)。"""
    return [
        K(k_avg(K_SELF, w), "Kong 自己處理", "A", instant=inst),
        M(pos("$X - ($C + $D + $E)"), "B"),
        O(r_avg("queue", w), "Router 排隊等 Pod", "C", instant=inst),
        O(r_avg("connect", w), "Router 連線到 Pod", "D", instant=inst),
        O(r_avg("response", w), "Router 等 Pod 回應", "E", instant=inst),
        O(app_avg(w), "App 自己處理", "F", instant=inst),
        M("$A + $X", "G"),
        K(k_avg(K_UP, w), "Kong 等後端回應", "X", hide=True, instant=inst),
    ]



GAP_NAME = name_ref("B", "Kong ↔ Router 之間（A10/F5、網路）")
NO_APP_HIST = "沒資料：App 沒開 Micrometer histogram（management.metrics.distribution.percentiles-histogram.http.server.requests=true）"
TAIL_GAP = name_ref("H", "App 以外造成的慢（Kong 等後端回應 p99 − App p99，估算）",
                    [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}, {"id": "custom.lineWidth", "value": 3}])


def block(title):
    """區塊標題本身就說明這區在看什麼,不另外放文字框佔版面。"""
    B.row(title)


# ---- A 現在
block("A. 現在（最近 5 分鐘，不管上方時間）：紅字的方塊 = 有問題的那一段")
tg, fl, nd = topology(W5, "最近 5 分鐘")
B.line(topo_canvas("鏈路拓樸：最近 5 分鐘（平均）",
                   desc("同一條路徑每一段最近 5 分鐘的請求數、時間和錯誤。A10 / F5 沒有指標：時間用「Kong 等後端回應 − Router 量到的」推算，"
                        "請求數用「Kong 送往後端 − Router 收到」推算。數字變橘 / 紅 = 超過參考門檻。",
                        "先看哪個方塊有紅字，再往下看那一段的細節。"), tg, fl, nd), h=12)
B.line(
    bar("最近 5 分鐘：每段平均花多久",
        desc("一個請求在每一段平均花的時間，由上到下是請求經過的順序。最後兩條是參考：App 自己處理（包含在 Router 等 Pod 回應裡）、"
             "總耗時 = Kong 自己處理 + 等後端回應，只算有送到後端的請求（跟各段同一群請求；Kong 自己擋下的 401 / 429 不算）。"
             "Router、App 也會收到不經過 Kong 的請求（拓樸圖的「其他來源」），那部分很大時它們的平均要打折看。",
             "最長的那一條就是時間主要花的地方。App 自己處理沒資料 = App 沒有 Micrometer 指標。"),
        segments(W5, True),
        overrides=[GAP_NAME, name_ref("F", "（其中）App 自己處理"), name_ref("G", "總耗時（有送到後端的）")]),
    bar("最近 5 分鐘：各段 p95 / p99",
        desc("有直方圖的段才算得出百分位數：Kong 和 App（需開 Micrometer histogram）；Router 只有平均。"
             "**App 以外造成的慢** = Kong 等後端回應 p99 − App p99（估算，百分位數不能真的相減，只當方向參考）。",
             "Kong 等後端回應 p99 很高、App p99 不高 → 慢在 App 以外；兩個都高 → App 自己慢。"),
        [K(k_pct(0.95, K_SELF, W5), "Kong 自己處理 p95", "A", instant=True),
         K(k_pct(0.99, K_SELF, W5), "Kong 自己處理 p99", "B", instant=True),
         K(k_pct(0.95, K_UP, W5), "Kong 等後端回應 p95", "C", instant=True),
         K(k_pct(0.99, K_UP, W5), "Kong 等後端回應 p99", "D", instant=True),
         O(app_pct(0.95, W5), "App 自己處理 p95", "E", instant=True),
         O(app_pct(0.99, W5), "App 自己處理 p99", "F", instant=True),
         K(k_pct(0.99, K_TOTAL, W5), "總耗時 p99（全部請求）", "G", instant=True),
         M(pos("$D - $F"), "H")],
        overrides=[name_ref("H", "App 以外造成的慢（估算）")]),
    h=10)

# ---- B 整段平均
block("B. 整段平均（跟著上方時間，可拖曳回看）：找哪個時間點、哪一段變慢")
tg, fl, nd = topology("$__range", "整個時間範圍")
B.line(topo_canvas("鏈路拓樸：整個時間範圍（平均）",
                   desc("跟 A 的拓樸圖一樣，但算的是上方選的整個時間範圍。請求數是那段時間的平均每秒。",
                        "A 正常、這張有紅字 → 問題發生在過去某個時段，看下面趨勢圖找時間點。"), tg, fl, nd), h=12)
B.line(
    ts("每段平均耗時（各段各自的值，沒有疊加）",
       desc("每一段的平均時間各畫一條線，線的高度就是那一段自己的值。**總耗時** = Kong 自己處理 + 等後端回應，"
            "只算有送到後端的請求，跟各段是同一群請求。",
            "哪一條突然升高，問題就在那一段；對照右邊的請求數和下面的 5xx 看同一個時間點。"),
       segments(RI, False),
       overrides=[GAP_NAME, name_ref("F", "App 自己處理（含在 Router 等 Pod 回應裡）", DASHED),
                  name_ref("G", "總耗時（有送到後端的）", DASHED)]),
    h=10, widths=[24])
B.line(
    ts("每段收到的請求數（req/s）",
       desc("Kong 送往後端的請求、Router 收到的請求、App 收到的請求。同一條路徑三條線應該差不多。",
            "Kong 比 Router 多 → 請求在中間沒進到 Router；Router 比 Kong 多 → 有其他來源直接打 Router；"
            "App 比 Router 少 → Router 自己回了（例如沒有可用 Pod 的 503）。差多少看 ⑤。"),
       [K(kong_sent(RI), "Kong 送往後端", "A"), O(router_recv(RI), "Router 收到", "B"), O(app_recv(RI), "App 收到", "C")],
       unit="reqps"),
    ts("5xx 是誰產生的（req/s）",
       desc("**Kong 自己回的** = Kong 沒拿到後端回應，通常是逾時（504）或連不上（502）；"
            "**Router 回的** 包含 App 回的 5xx 和 Router 自己回的（沒有可用 Pod 503、逾時 504）。",
            "只有 Kong 自己回的在上升 → 問題在 Kong 和 Router 之間或後端太慢；Router 的上升但 App 的沒有 → Router 自己回的；"
            "App 的上升 → App 有錯。沒有 source label 的舊版 Kong 分不出是誰產生的。"),
       [K(f'sum(rate(kong_http_requests_total{{{KS}, source="kong", code=~"5.."}}[{RI}]))', "Kong 自己回的", "A"),
        K(f'sum(rate(kong_http_requests_total{{{KS}, source="service", code=~"5.."}}[{RI}]))', "Kong 收到後端回的", "B"),
        O(f'sum(rate(haproxy_backend_http_responses_total{{{RS}, code=~"5.*"}}[{RI}]))', "Router 回的（含 App）", "C"),
        O(f'sum(rate(http_server_requests_seconds_count{{{APP}, status=~"5.."}}[{RI}]))', "App 回的", "D")],
       unit="reqps"),
    h=10)

# ---- C 慢請求
block("C. 整段慢請求（p95 = 最慢的 5%、p99 = 最慢的 1%）：平均正常、偶爾很慢時看這裡")
tg, fl, nd = topology_tail("$__range")
B.line(topo_canvas("鏈路拓樸：整個時間範圍的慢請求（p95 / p99 / 最高）",
                   desc("Kong 和 App 的 p95 / p99、時間範圍內最慢的那一分鐘、超過 Router 逾時的請求筆數。"
                        "**App 以外 p99** = Kong 等後端回應 p99 − App p99（估算）。",
                        "Kong 的 p99 很高、App 的 p99 / 最慢的一筆不高 → 慢在 App 以外（A10 / F5、網路、Router）；App 也高 → AP Pod。"
                        "有請求超過逾時（紅）→ 看 D。"), tg, fl, nd), h=10)
B.line(
    ts("各段 p99 走勢（紅線 = App 以外造成的慢）",
       desc("Kong 等後端回應、App、Kong 自己處理的 p99，以及 Kong 和 App 的差（紅線）。",
            "紅線往上飆、App p99 沒變 → 慢請求發生在 App 以外（A10 / F5、網路、Router、Pod 內排隊），對照 ⑤ 和 ③；"
            "App p99 跟著飆 → App 自己變慢，看 ④。"),
       [K(k_pct(0.99, K_TOTAL, RI), "總耗時 p99（全部請求）", "A"), K(k_pct(0.99, K_UP, RI), "Kong 等後端回應 p99", "D"),
        O(app_pct(0.99, RI), "App 自己處理 p99", "F"), K(k_pct(0.99, K_SELF, RI), "Kong 自己處理 p99", "G"),
        M(pos("$D - $F"), "H")],
       overrides=[TAIL_GAP], no_value=NO_APP_HIST),
    ts("慢請求有多慢：p99 是平均的幾倍",
       desc("每段的 p99 是平均的幾倍。1～3 倍算正常；倍數大代表大部分請求很快、但有一小部分特別慢。",
            "Kong 等後端回應的倍數比 App 的大很多 → 慢請求是 App 以外造成的（網路、LB、Router 排隊、Pod 內排隊）；"
            "App 的倍數大 → App 有特定慢請求（看下面依 URI、依 Pod）。"),
       [K(f"({k_pct(0.99, K_UP, RI)}) / (({k_avg(K_UP, RI)}) > 0)", "Kong 等後端回應", "A"),
        O(f"({app_pct(0.99, RI)}) / (({app_avg(RI)}) > 0)", "App 自己處理", "B"),
        K(f"({k_pct(0.99, K_SELF, RI)}) / (({k_avg(K_SELF, RI)}) > 0)", "Kong 自己處理", "C")],
       unit="none"),
    h=10)
B.line(
    table("時間範圍內最差的 p99（依 Kong API）",
          desc("每個 Kong Service / Route 在整個時間範圍內 p99 最高的那一分鐘，以及平均。",
               "等後端 p99 最高很大、平均不大 → 那個 API 有偶發的慢請求；Kong 處理 p99 高 → 那個 API 的 plugin 慢。"),
          [("service", "Service"), ("route", "Route")],
          [("等後端 p99 最高", K(f"max_over_time(({k_pct(0.99, K_UP, '1m', ', service, route')})[$__range:1m])"), None),
           ("總耗時 p99 最高", K(f"max_over_time(({k_pct(0.99, K_TOTAL, '1m', ', service, route')})[$__range:1m])"), None),
           ("Kong 處理 p99 最高", K(f"max_over_time(({k_pct(0.99, K_SELF, '1m', ', service, route')})[$__range:1m])"),
            [{"id": "custom.minWidth", "value": 150}]),
           ("等後端 平均", K(f'sum by (service, route) (rate({K_UP}_sum{{{KS}}}[$__range])) '
                          f'/ (sum by (service, route) (rate({K_UP}_count{{{KS}}}[$__range])) > 0)'), None),
           ("每秒請求", K(f'sum by (service, route) (rate(kong_http_requests_total{{{KS}}}[$__range]))'),
            [{"id": "unit", "value": "reqps"}, {"id": "decimals", "value": 1}])],
          sort="等後端 p99 最高", key_width=160),
    h=7, widths=[24])
B.line(
    ts("App p99 依 URI（前 10，找哪個 API 慢）",
       desc("App 自己記錄的 p99，依 URI 分開。", "某個 URI 特別高 → 那支 API 本身慢（DB、下游服務）；全部一起高 → App 整體或 Pod 資源問題。"),
       [O(f'topk(10, {app_pct(0.99, RI, ", uri")})', "{{uri}}", "A")], no_value=NO_APP_HIST),
    ts("App p99 依 Pod（找哪個 Pod 有慢請求）",
       desc("App 自己記錄的 p99，依 Pod 分開。", "只有一個 Pod 特別高 → 那個 Pod 的 Node、GC 或分到的資料有問題。"),
       [O(app_pct(0.99, RI, ", pod"), "{{pod}}", "A")], no_value=NO_APP_HIST))

# ---- D 逾時
def while_504(source):
    """有 504 的那幾分鐘,Kong 等後端回應最長大約多久 (p99.9);504 是逾時造成的話,這個值 ≈ 逾時設定。"""
    return (f"max_over_time(({k_pct(0.999, K_UP, '1m')} and on() "
            f"(sum(rate(kong_http_requests_total{{{KS}, source=\"{source}\", code=\"504\"}}[1m])) > 0))[$__range:1m])")


block("D. 逾時（p99 卡在同一個秒數）：是誰的逾時、卡在哪一段")
TIMEOUT_GUIDE = """**認出逾時**（不用知道設定值）：p99 變成一條**平線** = 卡在逾時；「有 504 時等後端最長」≈ 逾時值；實際要多久看 **App 最慢的一筆**。
顯示 **≥ 60 秒（上限）** = 超過 Kong 直方圖最大一格（60 秒），實際多久量不到。30～60 秒之間的 p99 是在格子裡內插估的，只能當「大概在 30～60 秒」。

| 看到 | 誰的逾時 / 卡在哪 |
|---|---|
| **後端回的 504**（通常約 30 秒） | Router 等 Pod 逾時 → AP Pod 慢 |
| **Kong 自己回的 504**，App 最慢的一筆也一樣久 | AP Pod 慢（或它的 DB / 下游服務） |
| **Kong 自己回的 504**，App 快、執行緒 ≈ 100% | Pod 執行緒用完，在 Pod 裡排隊 |
| **Kong 自己回的 504**（通常約 60 秒），App、Router 都正常 | 沒到 Router：A10 / F5、網路 |
| **499** | Client 自己先放棄（Client 的逾時比 Kong 短，例如壓測工具） |
| **Kong 自己回的 502 / 503** | 中間設備斷線、連不上後端 |

確認單筆：暫時開 Router access log，`sH` = Router 等 Pod 逾時、`CH` = Kong 先斷線、沒有那筆 = 沒到 Router。"""
B.line(
    bar("時間範圍內最慢：每一段的最高值",
        desc("整個時間範圍內，每一段最慢的時候（每分鐘看一次）。Kong 是 p99；Router 只有平均，取最慢那台 Router Pod；"
             "App 是 Micrometer 記的最慢一筆（不用開 histogram）。最後兩條：**有 504 的那幾分鐘**，Kong 等後端回應最長大約多久（p99.9）"
             "—— 504 是逾時造成的話，這就是逾時設定。",
             "Kong 等後端回應很大、App 最慢的一筆也一樣大 → AP Pod；App 很小 → 慢在 App 以外（F5、網路、Router、Pod 裡排隊）。"
             "「有 504 時」那兩條沒資料 = 這段時間沒有 504。"),
        [K(f"max_over_time(({k_pct(0.99, K_SELF, '1m')})[$__range:1m])", "Kong 自己處理 p99", "A", instant=True),
         K(f"max_over_time(({k_pct(0.99, K_UP, '1m')})[$__range:1m])", "Kong 等後端回應 p99", "B", instant=True),
         O(f"max_over_time(({ROUTER_MAX_POD})[$__range:1m])", "Router 等 Pod 回應（最慢 Router Pod 的平均）", "C",
           instant=True),
         O(f"max_over_time(({APP_MAX})[$__range:1m])", "App 最慢的一筆", "D", instant=True),
         K(while_504("kong"), "Kong 自己回 504 時，等後端最長（≈ Kong 的逾時）", "E", instant=True),
         K(while_504("service"), "後端回 504 時，等後端最長（≈ Router / F5 的逾時）", "F", instant=True)]),
    text(TIMEOUT_GUIDE),
    h=13, widths=[9, 15])
SLOW_TREND = (
    ts("最慢的請求走勢",
       desc("Kong 等後端回應 p99、Router 最慢 Pod 的平均、App 最慢的一筆（對數刻度）。",
            "Kong 的線變成一條平線 → 那個高度就是逾時；App 的線沒跟著上去 → 卡在 App 以外；一起上去 → AP Pod 慢。"),
       [K(k_pct(0.99, K_UP, RI), "Kong 等後端回應 p99", "A"),
        O(ROUTER_MAX_POD, "Router 等 Pod 回應（最慢 Router Pod 的平均）", "B"),
        O(APP_MAX, "App 最慢的一筆", "C")],
       log=True))
B.line(
    ts("碰到逾時的請求（req/s）",
       desc("**等後端超過 N 秒** = Kong 等後端回應超過 N 秒的請求（用 Kong 直方圖的格子算，Kong 預設有 5、10、30、60 秒）。"
            "逾時不一定是 504：**Kong 自己回的 504** = Kong 等到自己的逾時；**後端回的 504** = Router 或 F5 先逾時；"
            "**499** = Client 自己先放棄；**Kong 自己回的 502 / 503** = 中間設備斷線、連不上。",
            "504 出現時，哪一條「超過 N 秒」也跟著出現，逾時大概就是 N 到下一格之間（例如超過 30 秒有、超過 60 秒沒有 → 逾時在 30～60 秒）。"
            "後端回的 504 → Router 等 Pod 逾時；只有 Kong 自己回的 504 → 後面完全沒回應，看左邊 App 和 ⑤。"),
       [K(over_timeout("5", RI), "等後端超過 5 秒", "A"),
        K(over_timeout("10", RI), "等後端超過 10 秒", "B"),
        K(over_timeout("30", RI), "等後端超過 30 秒", "C"),
        K(over_timeout("60", RI), "等後端超過 60 秒", "D"),
        K(f'sum(rate(kong_http_requests_total{{{KS}, source="kong", code="504"}}[{RI}]))', "Kong 自己回的 504", "E"),
        K(f'sum(rate(kong_http_requests_total{{{KS}, source="service", code="504"}}[{RI}]))', "後端回的 504", "F"),
        K(f'sum(rate(kong_http_requests_total{{{KS}, code="499"}}[{RI}]))', "499 Client 先放棄", "G"),
        K(f'sum(rate(kong_http_requests_total{{{KS}, source="kong", code=~"502|503"}}[{RI}]))', "Kong 自己回的 502 / 503", "H")],
       unit="reqps",
       overrides=[name_ref(r, n, [{"id": "custom.lineWidth", "value": 3}, {"id": "custom.fillOpacity", "value": 0},
                                  {"id": "color", "value": {"mode": "fixed", "fixedColor": c}}])
                  for r, n, c in (("E", "Kong 自己回的 504", "red"), ("F", "後端回的 504", "orange"),
                                  ("G", "499 Client 先放棄", "purple"), ("H", "Kong 自己回的 502 / 503", "yellow"))]),
    h=10, widths=[24])
B.line(
    SLOW_TREND,
    ts("App 最慢的一筆（依 Pod）",
       desc("Micrometer 記錄的每個 Pod 最近最慢的一筆請求（http_server_requests_seconds_max，約 2 分鐘的視窗）。"
            "請求結束才記錄，Kong 逾時放棄後 App 還在跑的那筆，跑完一樣會記到。",
            "只有一個 Pod 很高 → 那個 Pod；全部都高 → App 共用的 DB / 下游服務。"),
       [O(f"1000 * max by (pod) (http_server_requests_seconds_max{{{APP}}})", "{{pod}}", "A")],
       no_value="沒資料：App 沒有 Micrometer（http_server_requests_seconds）指標"))
B.line(
    ts("App 執行緒忙碌 ÷ 上限（Tomcat，依 Pod）",
       desc("每個 Pod 的 Tomcat 處理執行緒用了幾成。用完時新請求在 Pod 裡排隊，App 的計時看不到這段等待。",
            "接近 100% → Pod 處理不過來，請求在 Pod 裡排隊；加 Pod 或找出卡住執行緒的原因（DB、下游服務）。"),
       [O(f"max by (pod) (tomcat_threads_busy_threads{{{POD}}}) / (max by (pod) (tomcat_threads_config_max_threads{{{POD}}}) > 0)",
          "{{pod}}", "A")],
       unit="percentunit",
       thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 0.9}],
       no_value="沒資料：Spring Boot 要開 server.tomcat.mbeanregistry.enabled=true；Undertow / WebFlux 沒有這個指標"),
    ts("GC 最長停頓（依 Pod）",
       desc("每個 Pod 最近最長的一次 GC 停頓（jvm_gc_pause_seconds_max）。停頓期間這個 Pod 所有請求都卡住。",
            "停頓到秒級 → 記憶體不夠或 heap 設定問題，對照 OCP 壓測影響儀表板的記憶體。"),
       [O(f"1000 * max by (pod) (jvm_gc_pause_seconds_max{{{POD}}})", "{{pod}}", "A")],
       no_value="沒資料：App 沒有 Micrometer JVM 指標"))

B.row("② Kong：Kong 自己處理、各節點、慢請求")
B.line(
    ts("Kong 延遲 p95 / p99（總耗時 / 等後端回應 / Kong 自己處理）",
       desc("Kong 的百分位數。**等後端回應** = Kong 把請求送出去，到收到後端回應的時間（包含 A10/F5、Router、App）；**Kong 自己處理** = 跑 plugin、路由等 Kong 本身的時間（plugin 呼叫外部服務，例如認證伺服器、Redis，也算在這裡）。"
            "虛線是常見的逾時：Router 預設 30s、Kong read_timeout 預設 60s。",
            "等後端回應 p99 高、Kong 自己處理低 → Kong 只是在等後端；Kong 自己處理高 → Kong 本身或 plugin 呼叫的外部服務慢。p99 卡在虛線附近 → 碰到逾時。"),
       [K(k_pct(0.99, K_TOTAL, RI), "總耗時 p99", "A"), K(k_pct(0.99, K_UP, RI), "等後端回應 p99", "B"),
        K(k_pct(0.95, K_UP, RI), "等後端回應 p95", "C"), K(k_pct(0.99, K_SELF, RI), "Kong 自己處理 p99", "D")],
       thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 30000}, {"color": "red", "value": 60000}],
       log=True),
    ts("Kong 回應數（依狀態碼、誰產生）",
       desc("Kong 每秒回了多少，依狀態碼和誰回的：Kong 自己回的（沒拿到後端回應）、後端回的、plugin 直接擋下（沒送到後端）。",
            "502 / 504 Kong 自己回的 = 連不上或等不到後端；401 / 403 / 429 Kong 自己回的或 plugin 直接擋下 = 被 Kong plugin 擋下，沒有送到後端。"),
       [K('label_replace(label_replace(label_replace('
          f'sum by (code, source) (rate(kong_http_requests_total{{{KS}}}[{RI}])) > 0, '
          '"by", "Kong 自己回的", "source", "kong"), "by", "後端回的", "source", "service|"), '
          '"by", "plugin 直接擋下", "source", "exit")', "{{code}} {{by}}", "A")],
       unit="reqps"))
B.line(
    ts("各 Kong 節點：等後端回應 p99（找哪台 Kong 異常）",
       desc("每台 Kong 節點自己的等後端回應 p99。", "只有一台特別高 → 那台 Kong 到後端的路徑（所在網段、LB）有問題；全部一起高 → 問題在後面。"),
       [K(k_pct(0.99, K_UP, RI, ", instance"), "{{instance}}", "A")], log=True),
    ts("各 Kong 節點：每秒請求數",
       desc("每台 Kong 節點處理多少請求。", "差很多 → 前面的 LB（A10 / F5）分配不平均。"),
       [K(f'sum by (instance) (rate(kong_http_requests_total{{{KS}}}[{RI}]))', "{{instance}}", "A")], unit="reqps"))
B.line(
    ts("Kong 連線狀態（nginx）",
       desc("每台 Kong 節點的連線：全部、讀請求中、處理 / 回應中、閒置（連線保留著等下一個請求）。",
            "處理 / 回應中持續升高 → 很多請求卡著在等後端；全部突然暴增 → 前面湧入大量連線或連線沒有重用。"),
       [K('label_replace(label_replace(label_replace(label_replace('
          'sum by (instance, state) (kong_nginx_connections_total{state=~"active|reading|writing|waiting"}), '
          '"st", "全部", "state", "active"), "st", "讀請求中", "state", "reading"), '
          '"st", "處理 / 回應中", "state", "writing"), "st", "閒置", "state", "waiting")',
          "{{instance}} {{st}}", "A")], unit="none", rows=8))

# ---- ③ Router
B.row("③ OCP Router（HAProxy）：排隊、連線、各 Router Pod")
B.line(
    ts("Router 平均耗時：排隊 / 連線到 Pod / 等 Pod 回應",
       desc("Router 量到的三段平均（各 Router Pod 依流量加權）。排隊 = 等可用的 Pod 連線；連線到 Pod = 建立 TCP 連線；等 Pod 回應 = 送出到收到回應標頭。",
            "排隊高 → Pod 不夠或 maxconn 太小；連線高 → Pod 網路或 Pod 忙到 accept 不了；等 Pod 回應高 → 看 ④ 是哪個 Pod、App 自己慢不慢。"),
       [O(r_avg("queue", RI), "排隊", "A"), O(r_avg("connect", RI), "連線到 Pod", "B"), O(r_avg("response", RI), "等 Pod 回應", "C")],
       stack=True),
    ts("各 Router Pod：等 Pod 回應平均（找哪台 Router 異常）",
       desc("每個 Router Pod 自己量到的等 Pod 回應平均。", "只有一台特別高 → 那台 Router 所在的 Node 有問題（CPU、網路）。"),
       [O(f'max by (pod) (haproxy_backend_http_average_response_latency_milliseconds{{{RS}}})', "{{pod}}", "A")]))
B.line(
    ts("各 Router Pod：每秒回應數（LB 分配是否平均）",
       desc("每個 Router Pod 處理多少這條 route 的請求。", "差很多 → 前面的 LB（A10 / F5）只往部分 Router 送，或某台 Router 被 LB 判定不健康。"),
       [O(f'sum by (pod) (rate(haproxy_backend_http_responses_total{{{RS}}}[{RI}]))', "{{pod}}", "A")], unit="reqps"),
    ts("Router 錯誤：連不上 Pod / 回應錯誤 / 重試 / 排隊中",
       desc("Router 連 Pod 失敗、讀 Pod 回應失敗、重試的次數（每秒），以及目前在排隊的請求數。",
            "連不上 Pod 上升 → Pod 掛了或網路斷；回應錯誤上升 → Pod 回到一半斷線；排隊中 > 0 → Pod 處理不及。"),
       [O(f'sum(rate(haproxy_backend_connection_errors_total{{{RS}}}[{RI}]))', "連不上 Pod", "A"),
        O(f'sum(rate(haproxy_backend_response_errors_total{{{RS}}}[{RI}]))', "回應錯誤", "B"),
        O(f'sum(rate(haproxy_backend_retry_warnings_total{{{RS}}}[{RI}]))', "重試", "C"),
        O(f'sum(haproxy_backend_current_queue{{{RS}}})', "排隊中（個）", "D")],
       unit="short"))
B.line(
    ts("Router 對 Pod 開新連線的比例（新連線 ÷ 請求數）",
       desc("Router 每處理一個請求，平均要對 Pod 開幾條新連線。接近 0 = 連線有重用；接近 1 = 每個請求都重新連線。",
            "比例突然升高、Router 連線到 Pod 的時間也上升 → 連線一直被關掉重開（Pod 或中間設備沒保持連線）。"),
       [O(f'sum(rate(haproxy_backend_connections_total{{{RS}}}[{RI}])) / (sum(rate(haproxy_backend_http_responses_total{{{RS}}}[{RI}])) > 0)',
          "新連線 ÷ 請求數", "A")], unit="percentunit"),
    ts("Router Pod：CPU 用量（核）與目前連線數",
       desc("Router Pod（openshift-ingress）的 CPU，以及每台 Router 目前的連線數（右軸）。Router 是全叢集共用，別的服務流量也會算進來。",
            "某台 Router CPU 特別高或接近上限 → 那台 Router 本身變慢，所有走它的 route 都會受影響。"),
       [O('sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="openshift-ingress", container!="", container!="POD"}[' + RI + ']))',
          "CPU {{pod}}", "A"),
        O('sum by (pod) (haproxy_frontend_current_sessions)', "連線數 {{pod}}", "B")],
       unit="none", rows=4,
       overrides=[{"matcher": {"id": "byRegexp", "options": "連線數 .*"},
                   "properties": [{"id": "custom.axisPlacement", "value": "right"},
                                  {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]}]))

# ---- ④ AP Pod
B.row("④ AP Pod：哪個 Pod 慢、App 自己慢不慢")
NO_SERVER = ("沒資料：Router 沒有輸出 Pod 層級指標。Router 後面的 Pod 總數超過 ROUTER_METRICS_HAPROXY_SERVER_THRESHOLD"
             "（預設 500）時只輸出 route 層級；TCP passthrough 的 route 也沒有。改看下方 App 自己量的每個 Pod 時間")
B.line(
    ts("Router 看到的每個 Pod：等回應平均（找哪個 Pod 慢）",
       desc("Router 等每個 AP Pod 回應的平均時間（取各 Router Pod 中最高的）。", "只有一個 Pod 特別高 → 那個 Pod 的 Node、GC 或分到的資料有問題；全部都高 → App 整體慢。"),
       [O(f'max by (exported_pod) (haproxy_server_http_average_response_latency_milliseconds{{{RS}}})', "{{exported_pod}}", "A")],
       no_value=NO_SERVER),
    ts("Router 看到的每個 Pod：5xx（req/s）",
       desc("Router 從每個 AP Pod 收到的 5xx。", "集中在某個 Pod → 那個 Pod 有問題（設定、資料、資源）；平均分散 → App 整體或下游的問題。"),
       [O(f'sum by (exported_pod) (rate(haproxy_server_http_responses_total{{{RS}, code=~"5.*"}}[{RI}])) > 0', "{{exported_pod}}", "A")],
       unit="reqps", no_value=NO_SERVER))
B.line(
    ts("App 自己量的處理時間：每個 Pod 平均（需 Micrometer）",
       desc("App 自己記錄的處理時間（http_server_requests_seconds，不含 /actuator）。這是請求進到 App 之後的時間。",
            "跟上面 Router 看到的比：Router 的比 App 的高很多 → 請求在 Pod 裡排隊（thread pool 滿）或 Pod 網路慢。沒資料 = App 沒有 Micrometer 指標。"),
       [O(f'1000 * sum by (pod) (rate(http_server_requests_seconds_sum{{{APP}}}[{RI}])) / (sum by (pod) (rate(http_server_requests_seconds_count{{{APP}}}[{RI}])) > 0)',
          "{{pod}}", "A")],
       no_value="沒資料：App 沒有 Micrometer（http_server_requests_seconds）指標"),
    ts("Pod CPU throttling（各 Pod）",
       desc("CPU 想用但被 limit 擋下的時間比例。", "超過 25% → App 被 CPU limit 卡住，處理時間會變長；調高 CPU limit 或加 Pod。"),
       [O(f'sum by (pod) (max by (pod, container) (rate(container_cpu_cfs_throttled_periods_total{{{CT}}}[{RI}])))'
          f' / (sum by (pod) (max by (pod, container) (rate(container_cpu_cfs_periods_total{{{CT}}}[{RI}]))) > 0)', "{{pod}}", "A")],
       unit="percentunit"))
B.line(
    ts("Pod 狀態：Router 判定可用、Ready、重啟",
       desc("Router 判定可以送流量的 Pod 數（haproxy_server_up，Router 沒輸出 Pod 層級指標時沒有這條）、Ready 的 Pod 數，以及每 5 分鐘重啟次數。",
            "Router 可用數掉下來 → 流量集中到剩下的 Pod，排隊和回應時間會上升；有重啟 → 看 OCP 壓測影響評估 Dashboard 的 2B 重啟原因。"),
       [O(f'count(max by (exported_pod) (haproxy_server_up{{{RS}}}) == 1)', "Router 判定可用", "A"),
        O(f'sum(kube_pod_status_ready{{{POD}, condition="true"}})', "Ready", "B"),
        O(f'sum(increase(kube_pod_container_status_restarts_total{{{POD}}}[5m]))', "重啟（每 5 分鐘）", "C")],
       unit="none"))

# ---- ⑤ external
B.row("⑤ 中間沒有指標的段：A10 / F5、網路（用差值推算）")
B.line(
    ts("Kong ↔ Router 之間多花的時間（= Kong 等後端回應 − Router 量到的）",
       desc("Kong 等後端回應的平均，扣掉 Router 量到的排隊、連線、等 Pod 回應。剩下的就是 Kong 和 Router 之間：A10 / F5、網路、TLS 握手、Router 前端。",
            "平常有一個固定的量（網路來回）；突然變大而 Router 各段沒變 → 問題在中間設備或網路。"
            "兩邊選的不是同一條路徑時這個數字沒有意義。"),
       [K(k_avg(K_UP, RI), "Kong 等後端回應", "A"),
        O(r_avg("queue", RI), "", "B", hide=True), O(r_avg("connect", RI), "", "C", hide=True),
        O(r_avg("response", RI), "", "D", hide=True),
        M("$B + $C + $D", "E"), M(pos("$A - $E"), "F")],
       overrides=[name_ref("E", "Router 量到的（排隊 + 連線 + 等 Pod 回應）"),
                  name_ref("F", "Kong ↔ Router 之間", [{"id": "custom.lineWidth", "value": 3},
                                                       {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}])]),
    ts("請求數對不上的部分（req/s）",
       desc("**Kong 送了、Router 沒收到** = Kong 送往後端 − Router 收到；**Router 收到、不是 Kong 來的** = Router 收到 − Kong 送往後端。",
            "**先排除假警報**：Kong 選的 Service / Route 和 OCP Route 不是同一條路徑（Kong 一個 Service 可能對到好幾條 OCP route）；"
            "OCP route 是 TLS passthrough（Router 不記 HTTP 回應數，Router 收到 = 0）。看右邊的表確認。"
            "排除後：前者 > 0 → 請求在中間被擋、逾時或被導到別處（同時看 B 的 5xx 是誰產生的）；"
            "後者 > 0 → 有其他來源直接打這條 route（內部呼叫、繞過 Kong、LB health check）。"),
       [K(kong_sent(RI), "", "A", hide=True), O(router_recv(RI), "", "B", hide=True),
        M(pos("$A - $B"), "C"), M(pos("$B - $A"), "D")],
       unit="reqps",
       overrides=[name_ref("C", "Kong 送了、Router 沒收到", [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]),
                  name_ref("D", "Router 收到、不是 Kong 來的", [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}])]))
B.line(
    table("名稱有關鍵字的 route 在 Router 上的流量（找 passthrough、選錯 route）",
          desc("名稱裡有關鍵字的每條 route（不管上方 OCP Route 選了什麼；清空關鍵字 = 整個 Namespace）在 Router 上的 backend、HTTP 回應數、收到 / 送出的流量。"
               "backend 開頭是 be_http（HTTP）、be_edge_http（edge TLS）、be_secure（reencrypt）、be_tcp（**TLS passthrough**）。",
               "**有 bytes、但 HTTP 回應數是 0 或沒有** → passthrough，Router 只看得到 TCP，請求數、回應時間都沒有，"
               "只能看 Kong 的等後端回應和 App 自己的時間；**Kong 的流量跟某條沒選到的 route 對得上** → 上方 OCP Route 選錯了。"),
          [("route", "Route"), ("backend", "Backend")],
          [("HTTP 回應", O(f'sum by (route, backend) (rate(haproxy_backend_http_responses_total{{exported_namespace=~"$namespace", route=~"(?i).*(${{keyword}}).*"}}[5m]))'),
            [{"id": "unit", "value": "reqps"}, {"id": "decimals", "value": 1}]),
           ("收到", O(f'sum by (route, backend) (rate(haproxy_backend_bytes_in_total{{exported_namespace=~"$namespace", route=~"(?i).*(${{keyword}}).*"}}[5m]))'),
            [{"id": "unit", "value": "Bps"}]),
           ("送出", O(f'sum by (route, backend) (rate(haproxy_backend_bytes_out_total{{exported_namespace=~"$namespace", route=~"(?i).*(${{keyword}}).*"}}[5m]))'),
            [{"id": "unit", "value": "Bps"}]),
           ("新連線", O(f'sum by (route, backend) (rate(haproxy_backend_connections_total{{exported_namespace=~"$namespace", route=~"(?i).*(${{keyword}}).*"}}[5m]))'),
            [{"id": "unit", "value": "cps"}, {"id": "decimals", "value": 1}])],
          sort="收到", key_width=190),
    ts("Kong 自己回的 502 / 503 / 504（連不上 / 逾時）",
       desc("Kong 沒拿到後端回應而自己回的錯誤，依狀態碼。502 = 連線失敗或後端回了不完整的回應；503 = 沒有可用的後端；504 = 等後端回應逾時。",
            "跟「Kong ↔ Router 之間」同時上升 → 中間設備或網路問題；Router 那邊沒看到同樣數量的錯誤 → 請求根本沒到 Router。"),
       [K(f'sum by (code) (rate(kong_http_requests_total{{{KS}, source="kong", code=~"502|503|504"}}[{RI}])) > 0', "{{code}}", "A")],
       unit="reqps", no_value="0"))


# ---------------------------------------------------------------- variables
def var(name, label, ds, query, all_value, desc):
    """多選 + All。清單本身已經用名稱關鍵字篩過,All = 清單裡全部 (all_value 為 None 時由 Grafana 把清單串成 regex)。"""
    return {"type": "query", "name": name, "label": label, "description": desc, "datasource": ds,
            "query": {"query": query, "refId": f"{name}-var"}, "definition": query, "regex": "",
            "refresh": 2, "sort": 1, "multi": True, "includeAll": True, "allValue": all_value,
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "hide": 0, "options": []}


# 名稱關鍵字:不分大小寫、名稱裡有這段文字就算;可以用 | 放多個 (例外名稱)。
# 直接寫在變數清單的查詢裡 (各版 Grafana 都會代入),不放在 All 的 allValue (舊版 Grafana 不會再代入一次)。
KW = "(?i).*($keyword).*"


dash = {
    "uid": "kong-router-ap-chain",
    "title": "Kong → Router → AP 延遲鏈路分析",
    "description": "把一個請求的時間拆成 Kong、Kong ↔ Router 之間（A10/F5、網路）、Router、AP Pod 各段，找出慢在哪段、錯在哪段。",
    "tags": ["kong", "ocp", "router", "latency"],
    "timezone": "browser", "schemaVersion": 41, "version": 1, "editable": True, "graphTooltip": 1,
    "refresh": "1m", "time": {"from": "now-1h", "to": "now"},
    "links": [{"title": "OCP 壓測影響評估（單一 Deployment）", "type": "link", "icon": "dashboard",
               "url": "/d/ocp-loadtest-impact/?var-datasource=${ocp_ds}&${namespace:queryparam}&var-workload=${deployment:text}",
               "keepTime": True, "targetBlank": False}],
    "templating": {"list": [
        {"type": "datasource", "name": "kong_ds", "label": "Kong 資料來源", "query": "prometheus", "regex": "",
         "current": {}, "hide": 0, "refresh": 1},
        {"type": "datasource", "name": "ocp_ds", "label": "OCP 資料來源", "query": "prometheus",
         "regex": "/^Prometheus.*OCP$/", "current": {}, "hide": 0, "refresh": 1},
        {"type": "textbox", "name": "keyword", "label": "名稱關鍵字",
         "description": "同一條路徑共同的名稱（不分大小寫）。下面的清單只會列出名稱裡有這段文字的；名稱不像的例外用 | 加進來，例如 order|ord-api。",
         "query": "", "current": {"text": "", "value": ""}, "hide": 0},
        var("kong_service", "Kong Service", DSK, f'label_values(kong_http_requests_total{{service=~"{KW}"}}, service)', None,
            "清單 = 名稱裡有關鍵字的 Service；All = 清單裡全部。"),
        var("kong_route", "Kong Route", DSK, 'label_values(kong_http_requests_total{service=~"$kong_service"}, route)', ".*",
            "清單跟著上面的 Service；All = 那些 Service 底下的全部 Route。"),
        var("namespace", "OCP Namespace", DSO,
            f'label_values(haproxy_backend_http_responses_total{{route=~"{KW}"}}, exported_namespace)', ".+",
            "清單 = 有名稱符合關鍵字的 Route 的 Namespace；All = 不限。同樣名稱出現在多個 Namespace（例如 dev / prod）時，選要看的那個。"),
        var("router_route", "OCP Route", DSO,
            f'label_values(haproxy_backend_http_responses_total{{exported_namespace=~"$namespace", route=~"{KW}"}}, route)', None,
            "清單 = 名稱裡有關鍵字的 Route；All = 清單裡全部。"),
        var("deployment", "AP Deployment", DSO,
            f'label_values(kube_deployment_spec_replicas{{namespace=~"$namespace", deployment=~"{KW}"}}, deployment)', None,
            "清單 = 名稱裡有關鍵字的 Deployment；All = 清單裡全部。"),
    ]},
    "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                              "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                              "name": "Annotations & Alerts", "type": "dashboard"}]},
    "panels": B.panels,
}

with open(OUT, "w", encoding="utf-8", newline="\n") as f:
    json.dump(dash, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("panels:", sum(len(p["panels"]) if p["type"] == "row" else 1 for p in B.panels))
