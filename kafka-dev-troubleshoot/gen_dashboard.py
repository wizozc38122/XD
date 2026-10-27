# 產生「Kafka 問題排查（開發人員）」兩個 Grafana dashboard JSON:
#   輸出.json       問題排查:全部跟著上方時間範圍走 (回看過去、評估壓測)
#   輸出-live.json  即時現況:最新狀態,隱藏時間選擇器
# 用法: python gen_dashboard.py 輸出.json [datasource 名稱 regex] [--expand] [--scrape=15s]
# 資料來源:jmx_exporter (官方 examples/kafka-kraft-3_0_0.yml 或自訂規則都支援) + danielqsj/kafka-exporter + node_exporter
import json, sys

EXPAND = "--expand" in sys.argv  # lab 截圖用:收合的區塊全部展開
# kafka-exporter 的 scrape 間隔 (例如 15s、30s、1m),要跟 Prometheus 設定一致
SCRAPE = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--scrape=")), "15s")
SCRAPE_S = int(SCRAPE[:-1]) * {"s": 1, "m": 60}[SCRAPE[-1]]
ARGS = [a for a in sys.argv if not a.startswith("--")]
OUT = ARGS[1]
OUT_LIVE = OUT[:-5] + "-live.json" if OUT.endswith(".json") else OUT + "-live"

DS = {"type": "prometheus", "uid": "${datasource}"}
C = 'cluster="$cluster"'
CT = C + ',topic=~"$topic"'
CG = CT + ',consumergroup=~"$consumergroup"'
RI = "$__rate_interval"
PR = "$__range"  # 整個所選時間範圍
# 比較區間 (上方變數,值是秒數,顯示文字是 5m / 15m / 1h):速度、Lag 變化、追上需要全部用同一段。
# 變數名稱不沿用舊的 rate_window:舊網址帶的 var-rate_window=15m 不是秒數,換名字讓它被忽略、用預設值
WS = "${compare_window}"
RW = WS + "s"
WT = "${compare_window:text}"
# 頭尾平均用的長度:至少 4 次 scrape,才平均得掉 commit 造成的鋸齒
SMOOTH = f"{max(60, 4 * SCRAPE_S)}s"
# BrokerTopicMetrics 有一條不帶 topic 的總計,另外排除內部 topic
BT = C + ',topic=~"$topic",topic!="",topic!~"__.*"'
DISK = 'device!~"loop.*|ram.*|dm-.*"'
FS = 'fstype!~"tmpfs|overlay|squashfs|proc|sysfs|cgroup2?|nsfs|autofs"'
NIC = 'device!~"lo|veth.*|docker.*|br-.*|cali.*|flannel.*|cni.*"'
LINK_TAG = "kafka-dev-troubleshoot"  # 兩個 dashboard 用這個 tag 互相連結

_id = [0]
def nid():
    _id[0] += 1
    return _id[0]

def desc(what, normal, *bad, cols=()):
    # bad:每條一個「看到什麼 → 代表什麼 / 怎麼做」;cols:表格欄位說明 (欄名, 說明)
    out = what + "\n\n"
    if cols:
        out += "\n".join(f"- **{k}**：{v}" for k, v in cols) + "\n\n"
    return out + f"**正常**：{normal}\n\n" + "\n".join(f"- {b}" for b in bad)

def tgt(expr, legend="__auto", ref="A", instant=False, fmt=None):
    t = {"datasource": DS, "editorMode": "code", "expr": expr, "legendFormat": legend, "refId": ref,
         "range": not instant, "instant": instant}
    if fmt:
        t["format"] = fmt
    return t

def steps(*pairs):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in pairs]}

# gridPos 由 Board.line() 決定,這裡先不給
def ts(title, d, targets, unit="short", th=None, th_style="off", min_=None, max_=None,
       overrides=None, stack=False, fill=10, legend_calcs=("mean", "max", "lastNotNull"), no_value=None,
       draw="line", decimals=None):
    custom = {"drawStyle": draw, "lineWidth": 1, "fillOpacity": fill, "showPoints": "never" if draw == "line" else "always",
              "pointSize": 6, "spanNulls": False, "axisSoftMin": 0, "thresholdsStyle": {"mode": th_style},
              "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    defaults = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"},
                "thresholds": th or steps(("green", None))}
    if min_ is not None: defaults["min"] = min_
    if max_ is not None: defaults["max"] = max_
    if no_value: defaults["noValue"] = no_value
    if decimals is not None: defaults["decimals"] = decimals
    return {"id": nid(), "type": "timeseries", "title": title, "description": d, "datasource": DS,
            "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
            "options": {"legend": {"displayMode": "table", "placement": "bottom", "showLegend": True,
                                   "calcs": list(legend_calcs), "sortBy": "Max", "sortDesc": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}

def stat(title, d, expr, unit="none", th=None, mappings=None, no_value=None, decimals=None, calc=None):
    # calc 有給 = 看整個所選時間範圍 (range 查詢,依 calc 取 max / min,畫小走勢圖);沒給 = 只看最後一刻
    defaults = {"unit": unit, "color": {"mode": "thresholds"}, "thresholds": th or steps(("blue", None)),
                "mappings": mappings or []}
    if no_value: defaults["noValue"] = no_value
    if decimals is not None: defaults["decimals"] = decimals
    return {"id": nid(), "type": "stat", "title": title, "description": d, "datasource": DS,
            "targets": [tgt(expr, instant=not calc)],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"reduceOptions": {"calcs": [calc or "lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "area" if calc else "none", "textMode": "value",
                        "justifyMode": "center", "orientation": "auto", "wideLayout": True,
                        "showPercentChange": False}}

def bar(title, d, expr, legend, unit, decimals=None, th=None, no_value=None):
    defaults = {"unit": unit, "min": 0, "color": {"mode": "thresholds" if th else "continuous-BlYlRd"},
                "thresholds": th or steps(("green", None))}
    if decimals is not None: defaults["decimals"] = decimals
    if no_value: defaults["noValue"] = no_value
    return {"id": nid(), "type": "bargauge", "title": title, "description": d, "datasource": DS,
            "targets": [tgt(expr, legend, instant=True)],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                        "valueMode": "text", "namePlacement": "left", "sizing": "auto",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}

def col(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}

def table(title, d, targets, rename, order, overrides=(), sort=None, no_value=None):
    tr = [{"id": "merge", "options": {}},
          {"id": "organize", "options": {"excludeByName": {"Time": True},
                                         "indexByName": {k: i for i, k in enumerate(order)},
                                         "renameByName": rename}}]
    if sort:
        tr.append({"id": "sortBy", "options": {"sort": [{"field": sort, "desc": True}]}})
    defaults = {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": False, "minWidth": 60},
                "thresholds": steps(("green", None))}
    if no_value: defaults["noValue"] = no_value
    return {"id": nid(), "type": "table", "title": title, "description": d, "datasource": DS,
            "targets": targets, "transformations": tr,
            "fieldConfig": {"defaults": defaults, "overrides": list(overrides)},
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}}

KEYNAME = {"consumergroup": "Consumer Group", "topic": "Topic", "partition": "Partition"}
def mtable(title, d, keys, cols, base=None, sort=None, no_value=None):
    # 多個查詢合成一張表 (每個查詢一欄);keys:每列的 label;cols:(欄名, 查詢, 欄位設定)。
    # base:每一列都會有的查詢 (值不重要)。某欄整個查詢沒資料時 Grafana 會把整欄拿掉,
    # 所以每欄都補「這一列沒資料 → NaN」,NaN 顯示成那欄的 noValue 文字 (預設 —)
    assert len(cols) >= 2  # 只有一個查詢時欄位叫 Value,不是 Value #A
    refs = [chr(65 + i) for i in range(len(cols))]
    rename = {**{k: KEYNAME[k] for k in keys}, **{f"Value #{r}": n for r, (n, _, _) in zip(refs, cols)}}
    overrides = [col("Consumer Group", [{"id": "custom.width", "value": 160}])]
    targets = []
    for r, (name, expr, props) in zip(refs, cols):
        if base:
            expr = f"({expr}) or ({base} + NaN)"
            props = [dict(x) for x in props]
            na = next((x["value"] for x in props if x["id"] == "noValue"), "—")
            special = {"type": "special", "options": {"match": "nan", "result": {"text": na, "color": "#8e8e8e", "index": 99}}}
            m = next((x for x in props if x["id"] == "mappings"), None)
            if m:
                m["value"] = m["value"] + [special]
            else:
                props.append({"id": "mappings", "value": [special]})
        targets.append(tgt(expr, ref=r, instant=True, fmt="table"))
        if props:
            overrides.append(col(name, props))
    return table(title, d, targets, rename, list(keys) + [f"Value #{r}" for r in refs], overrides,
                 sort=sort or cols[0][0], no_value=no_value)

def keep_positive(p, field):
    # 只留這欄 > 0 的列
    p["transformations"].insert(1, {"id": "filterByValue", "options": {
        "filters": [{"fieldName": field, "config": {"id": "greater", "options": {"value": 0}}}],
        "type": "include", "match": "all"}})
    return p

def text(content):
    return {"id": nid(), "type": "text", "title": "", "options": {"mode": "markdown", "content": content}}

def gp(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}

class Board:
    # 由上往下排:row() 開一個區塊,line() 排一列 (平分寬度)。收合區塊裡的 panel 放在 row 的 panels 裡
    def __init__(self):
        self.panels, self.y, self.inner, self.iy = [], 0, None, 0
    def row(self, title, collapsed=False):
        r = {"id": nid(), "type": "row", "title": title, "collapsed": collapsed,
             "gridPos": gp(0, self.y, 24, 1), "panels": []}
        self.panels.append(r)
        self.y += 1
        self.inner, self.iy = (r["panels"], self.y) if collapsed else (None, 0)
    def line(self, *ps, h=None):
        # 預設高度:表格 / 長條 8 格;走勢圖 10 格 (圖例放在下面)
        h = h or (8 if all(p["type"] in ("table", "bargauge") for p in ps) else 10)
        y = self.iy if self.inner is not None else self.y
        w = 24 // len(ps)
        for i, p in enumerate(ps):
            p["gridPos"] = gp(i * w, y, w, h)
            (self.inner if self.inner is not None else self.panels).append(p)
        if self.inner is not None:
            self.iy += h
        else:
            self.y += h
    def count(self):
        return sum(len(p["panels"]) if p["type"] == "row" else 1 for p in self.panels if p["type"] != "text")

def vmap(d):
    return [{"type": "value", "options": {k: {"text": t, "color": c, "index": i}
                                          for i, (k, (t, c)) in enumerate(d.items())}}]

def cell(kind):
    return {"id": "custom.cellOptions", "value": {"type": kind}}

ALERT_COUNT = steps(("green", None), ("red", 1))
WARN_COUNT = steps(("green", None), ("orange", 1))
PCT = steps(("green", None), ("yellow", 0.7), ("red", 0.85))
D0 = {"id": "decimals", "value": 0}
D1 = {"id": "decimals", "value": 1}

# 兩種 jmx_exporter 規則寫法都支援,用 or 接起來 (同一個環境只會有一邊有資料):
# - 新:規則有拆 label (kafka-jmx-prod-suggest.yml),例如 kafka_server_client_quota_byte_rate{resource,user}
# - 舊:官方範例 / KRaft 通用規則 type=(.+) 先配到,帳號 / listener / client 軟體被寫進 metric 名稱 → label_replace 拆出來,
#   帳號名稱裡的 - . 會變成 _ (例如 order-app 顯示成 order_app)
def by_user(kind, attr):
    # kind: produce / fetch / request;attr: byte_rate / throttle_time / request_time
    new = f'kafka_server_client_quota_{attr}{{{C},resource="{kind.capitalize()}"}}'
    sel = f'{{__name__=~"kafka_server_{kind}_user_.+_{attr}",{C}}}'
    old = f'label_replace({sel}, "user", "$1", "__name__", "kafka_server_{kind}_user_(.+?)(_client_id_.*)?_{attr}")'
    return f'({new} or {old})'

def by_listener(attr):
    new = f'kafka_server_socket_server_metrics_{attr}{{{C}}}'
    sel = f'{{__name__=~"kafka_server_socket_server_metrics_listener_.+_networkprocessor_[0-9]+_{attr}",{C}}}'
    old = (f'label_replace({sel}, "listener", "$1", "__name__", '
           f'"kafka_server_socket_server_metrics_listener_(.+)_networkprocessor_[0-9]+_{attr}")')
    return f'({new} or {old})'

CLIENT_SW_NEW = (f'label_join(kafka_server_socket_server_metrics_client_connections{{{C}}}, '
                 f'"client", " ", "clientsoftwarename", "clientsoftwareversion")')
CLIENT_SW = (f'label_replace({{__name__=~"kafka_server_socket_server_metrics_clientsoftwarename_.+_connections",{C}}}, '
             f'"client", "$1 $2", "__name__", "kafka_server_socket_server_metrics_clientsoftwarename_(.+)_clientsoftwareversion_(.+)_listener_(.+)_networkprocessor_[0-9]+_connections")')
CLIENT_SW_L = (f'({CLIENT_SW_NEW} or label_replace({CLIENT_SW}, "listener", "$1", "__name__", '
               f'".+_listener_(.+)_networkprocessor_[0-9]+_connections"))')

def authz(kind):
    # authorizer 每分鐘 allowed / denied 次數,全部 broker 加總
    new = f'kafka_server_authorizer_authorization_{kind}_rate_per_minute{{{C},role="broker"}}'
    old = f'{{__name__=~"kafka_server_plugins_config_authorizer_.+_role_broker_authorization_{kind}_rate_per_minute",{C}}}'
    return f'(sum(max_over_time({new}[2m])) or sum(max_over_time({old}[2m])))'
ACLS = (f'(max({{__name__=~"kafka_server_authorizer_acls_total(_count)?",{C},role="broker"}}) or '
        f'max({{__name__=~"kafka_server_plugins_config_authorizer_.+_role_broker_acls_total",{C}}}))')
NOQUOTA = "沒有資料：Kafka 要設定 quota 才會記錄每個帳號的流量（見 README）"
def nojmx(mbean):
    return f"沒有資料：jmx exporter 設定沒收 {mbean}（見 README）"

# ---------------------------------------------------------------- 查詢
# ① 健康
AUTHZ_ERR = f'sum(rate(kafka_network_requestmetrics_errors_total{{{C},error=~".*AUTHORIZATION_FAILED"}}[2m])) * 60'
LOGIN_FAIL = f'sum({by_listener("failed_authentication_rate")}) * 60'
IDLE_GROUPS = (f"count((sum by(consumergroup)(clamp_min(kafka_consumergroup_lag{{{C}}}, 0)) > 0) and on(consumergroup) "
               f"(max by(consumergroup)(kafka_consumergroup_members{{{C}}}) == 0))")
# (標題, 說明, 正常, 不正常時, 查詢, 即時另外用的查詢, 期間最糟取 max 或 min, 其他參數)
HEALTH = [
    ("Broker 數", "叢集裡的 broker 台數。", "等於部署台數。", ["變少 → 有 broker 掛了"],
     f"max(kafka_brokers{{{C}}})", None, "min", dict(no_value="exporter 沒資料")),
    ("離線 Partition", "沒有 leader、不能讀寫的 partition 數。", "0。", ["> 0 → 讀寫會失敗或 timeout，立刻找維運"],
     f"max(kafka_controller_kafkacontroller_offlinepartitionscount{{{C}}})", None, "max", dict(th=ALERT_COUNT)),
    ("ISR 不足", "同步副本數少於 min.insync.replicas 的 partition 數。", "0。",
     ["> 0 → acks=all 寫入會收到 NOT_ENOUGH_REPLICAS"],
     f"sum(kafka_server_replicamanager_underminisrpartitioncount{{{C}}})", None, "max", dict(th=ALERT_COUNT)),
    ("副本不足", "有副本沒跟上 leader 的 partition 數。", "0（broker 重啟時短暫 > 0 正常）。",
     ["長時間 > 0 → 某台 broker 掛了或太慢"],
     f"sum(kafka_server_replicamanager_underreplicatedpartitions{{{C}}})", None, "max", dict(th=WARN_COUNT)),
    ("Active Controller", "負責管理叢集的 controller 數。", "剛好 1。", ["0 → 建 topic、換 leader 都會失敗"],
     f"sum(kafka_controller_kafkacontroller_activecontrollercount{{{C}}})", None, "min",
     dict(th=steps(("red", None), ("green", 1), ("red", 2)))),
    ("權限錯誤（次/分）", "client 收到 *_AUTHORIZATION_FAILED 的次數。", "0。", ["> 0 → 看 ④ 權限被拒"],
     AUTHZ_ERR, None, "max", dict(th=WARN_COUNT, decimals=0, no_value="0")),
    ("登入失敗（次/分）", "SASL 登入失敗次數。", "0。", ["> 0 → 密碼錯、帳號被刪或 mechanism 設錯，看 ④ 登入失敗"],
     LOGIN_FAIL, f'max_over_time(({LOGIN_FAIL})[5m:30s])', "max", dict(th=WARN_COUNT, decimals=0, no_value="未收集")),
    ("沒人讀的 Group", "有 lag、但沒有 consumer 連著的 group 數。", "0（跑完就停的批次程式也算）。",
     ["> 0 → 程式掛了或停了，訊息一直堆"],
     IDLE_GROUPS, None, "max", dict(th=WARN_COUNT, no_value="0")),
]

# ② Consumer
LAG = f'kafka_consumergroup_lag{{{CG}}}'
COMMITTED = f'kafka_consumergroup_current_offset{{{CG}}}'
END = f'kafka_topic_partition_current_offset{{{CT}}}'
MEMBERS_RAW = f'kafka_consumergroup_members{{{C},consumergroup=~"$consumergroup"}}'
MEMBERS = f'max by(consumergroup)({MEMBERS_RAW})'
# kafka-exporter:group 沒 commit 過的 partition,commit 位置和 lag 都回 -1;-1 → 真的位置會被當成一口氣讀了整段,要排除
# offset 是 gauge:被往回重設時 rate() 會當成 counter reset 算出暴增,有往回的區間也不算
CONS_RATE = (f'sum by(consumergroup,topic)(rate({COMMITTED}[{RW}]) unless resets({COMMITTED}[{RW}]) > 0 '
             f'and min_over_time({COMMITTED}[{RW}]) >= 0)')
PROD_RATE = f'sum by(topic)(rate({END}[{RW}]))'
# 區間頭尾相減:頭尾各取一小段平均 (抹掉 commit 造成的鋸齒),只算頭尾都 commit 過的 partition。
# exporter 算 lag 時的 log end 是另外跟 broker 要的,不是 topic 的 current_offset,
# 所以寫入用 lag + commit 位置還原「算 lag 用的 log end」→ 寫入 − 消費 = Lag 變化 每個 partition 都成立
def change(m, span=RW):
    valid = (f'(min_over_time({COMMITTED}[{SMOOTH}]) >= 0 and on(consumergroup,topic,partition) '
             f'min_over_time({COMMITTED}[{SMOOTH}] offset {span}) >= 0)')
    return (f'(avg_over_time({m}[{SMOOTH}]) - avg_over_time({m}[{SMOOTH}] offset {span}) '
            f'and on(consumergroup,topic,partition) {valid})')
def d_lag(span=RW):
    return f'sum by(consumergroup,topic)({change(LAG, span)})'
def d_cons(span=RW):
    return f'sum by(consumergroup,topic)({change(COMMITTED, span)})'
def d_prod(span=RW):
    return f'sum by(consumergroup,topic)({change(LAG, span)} + {change(COMMITTED, span)})'
D_LAG = d_lag()
FLAT = 10  # Lag 變化在 ±10 筆內視為持平
def retained(sel):
    return f'(kafka_topic_partition_current_offset{{{sel}}} - kafka_topic_partition_oldest_offset{{{sel}}})'
LAGS = f'sum by(consumergroup,topic)(clamp_min({LAG}, 0))'
# 還沒 commit 過的 partition 數:lag 看不到 (算 0),寫入 / 消費 / Lag 變化 也不算它們
NO_COMMIT = f'count by(consumergroup,topic)({COMMITTED} == -1) or {LAGS} * 0'
# 還落後 = lag ÷ 寫入速度 (估算);比較區間內沒有寫入就沒有值 (不能拿 0 除)
BEHIND = f'({LAGS} / on(topic) group_left() ({PROD_RATE} > 0))'
# 預估追上需要 = lag ÷ 每秒減少量;lag 沒在減少就沒有值
CATCHUP = f'({LAGS} / (0 - {D_LAG} > {FLAT}) * {WS})'
RATIO = (f'max by(consumergroup,topic)(clamp_min({LAG}, 0) / on(topic,partition) group_left() '
         f'clamp_min({retained(CT)}, 1))')
# 只看有成員的時候:沒人讀的 group 遲早都是 100%,% 沒意義
RATIO_ACTIVE = f'({RATIO} and on(consumergroup) {MEMBERS} > 0)'
# 追上需要,依序判斷 (or 左邊優先):lag 0 → 已跟上 (-1);沒有 consumer → 沒人在讀 (-2);
# lag 不到 30 秒的寫入量 → 已跟上 (-1);lag 有在減少 → 秒數;lag 在增加 → 越落越多 (-3);持平 → 沒在追上 (-4);
# 比較區間前還沒有資料 → 沒有值
CATCHUP_STATE = (f'(({LAGS} == 0) * 0 - 1) '
                 f'or ({LAGS} * 0 - 2 and on(consumergroup) ({MEMBERS} == 0)) '
                 f'or ({LAGS} * 0 - 1 and on(consumergroup,topic) ({LAGS} * {WS} < {d_prod()} * 30)) '
                 f'or {CATCHUP} '
                 f'or ({LAGS} * 0 - 3 and on(consumergroup,topic) ({D_LAG} > {FLAT})) '
                 f'or ({LAGS} * 0 - 4 and on(consumergroup,topic) {D_LAG})')
# Lag 佔保留量:有成員 → %;沒成員且 > 100% → 已掉資料 (沒人讀) (-3);沒成員且有 lag → 沒人在讀 (-2);其餘 (lag 0) → %
RATIO_STATE = (f'{RATIO_ACTIVE} '
               f'or (({RATIO} > 1) * 0 - 3 and on(consumergroup) ({MEMBERS} == 0)) '
               f'or (({RATIO} > 0) * 0 - 2 and on(consumergroup) ({MEMBERS} == 0)) '
               f'or {RATIO}')
# 每張表的列:現在有的 group × topic / 期間出現過的 group × topic / topic
NOW_ROWS = f'{LAGS} * 0'
P_BASE = f'max by(consumergroup,topic)(max_over_time({LAG}[{PR}])) * 0'

# ② Topic
NOINT = ',topic!~"__.*"'
SIZE1 = f'sum by(topic)(max by(topic,partition)(kafka_log_log_size{{{CT}{NOINT}}}))'  # 單份大小:每個 partition 取最大的副本
TOPIC_RATE = f'rate(kafka_topic_partition_current_offset{{{CT}{NOINT}}}[{RW}])'
TOPIC_ROWS = f'max by(topic)(kafka_topic_partitions{{{CT}{NOINT}}}) * 0'

# ---------------------------------------------------------------- 欄位設定
SECONDS = [{"id": "unit", "value": "s"}]
LAG_CHANGE = [D0, {"id": "thresholds", "value": steps(("green", None), ("orange", FLAT))}, cell("color-text")]
MEMBERS_COL = [D0, {"id": "thresholds", "value": steps(("red", None), ("green", 1))}, cell("color-text")]
def pct_col(mappings):
    return [{"id": "unit", "value": "percentunit"}, {"id": "min", "value": 0}, {"id": "max", "value": 1},
            {"id": "thresholds", "value": PCT}, cell("gauge"), {"id": "noValue", "value": "—"},
            {"id": "mappings", "value": mappings + [{"type": "range", "options": {
                "from": 1.000001, "to": None, "result": {"text": "已掉資料", "color": "red", "index": 9}}}]}]
ONE_DAY = {"type": "range", "options": {"from": 86400, "to": None,
                                        "result": {"text": "> 1 天", "color": "orange", "index": 8}}}

RATIO_HELP = ("還沒讀的筆數 ÷ partition 還留著的筆數（最糟的 partition）。接近 100% = 快讀不到就被刪；"
              "> 100% = 已掉資料。partition 只留幾筆時常常是 100%，要搭配 Lag 看（細節見 README）")
BEHIND_HELP = "Lag ÷ 近期平均寫入速度，≈ 正在處理多久以前的資料（估算；寫入忽快忽慢時只是概略值）"

# ================================================================ 即時現況 dashboard
live = Board()
live.line(text("**即時現況**：永遠是最新狀態（每 30 秒更新），不受時間影響。"
               "要回看過去、評估壓測 → 右上 **Kafka 問題排查**。"
               f"「近 {WT}」= 上方 **比較區間**。每個面板的 ⓘ 有說明。"), h=2)

live.row("Kafka 本身有沒有壞")
hp = [stat(t, desc(w, n, *b), le or e, **kw) for t, w, n, b, e, le, fn, kw in HEALTH]
live.line(*hp[:4], h=3)
live.line(*hp[4:], h=3)

live.row("Consumer 追得上嗎")
live.line(
    mtable(f"追得上嗎（速度 = 近 {WT}）",
           desc("每個 group × topic 現在落後多少、多久追上。", "成員 ≥ 1、追上需要「已跟上」。",
                "沒人在讀 → 程式沒在跑", "越落越多 → 處理太慢：加 consumer（不超過 partition 數）或加快處理",
                cols=[("追上需要", "Lag ÷ lag 每秒減少多少。已跟上 = 沒有 lag 或不到 30 秒的寫入量；"
                                  f"持平 = lag 增減在 ±{FLAT} 筆內（沒在追上）；資料不足 = 比較區間開頭還沒有資料"),
                      ("還落後", BEHIND_HELP)]),
           ("consumergroup", "topic"),
           [("Lag", LAGS, [D0]),
            ("成員", f'max by(consumergroup,topic)({LAG}) * 0 + on(consumergroup) group_left() {MEMBERS}', MEMBERS_COL),
            ("追上需要", CATCHUP_STATE,
             SECONDS + [{"id": "noValue", "value": "資料不足"}, cell("color-text"),
                        {"id": "thresholds", "value": steps(("orange", None))},
                        {"id": "mappings", "value": vmap({"-1": ("已跟上", "green"), "-2": ("沒人在讀", "red"),
                                                          "-3": ("越落越多", "red"),
                                                          "-4": ("持平", "orange")}) + [ONE_DAY]}]),
            ("還落後", BEHIND,
             SECONDS + [{"id": "noValue", "value": "沒有新寫入"}, cell("color-text"),
                        {"id": "thresholds", "value": steps(("green", None), ("yellow", 60), ("red", 600))}])],
           base=NOW_ROWS),
    mtable(f"近 {WT} 寫入 / 消費（筆）",
           desc(f"比較區間頭尾相減的筆數（頭尾各取 {SMOOTH} 平均），**寫入 − 消費 = Lag 變化**（四捨五入可能差 1）。",
                "Lag 變化 ≤ 0。", "Lag 變化 > 0 → 越落越多", "消費是負數 → offset 被往回重設（重讀）",
                cols=[("寫入、消費", "只算這個 group 有 commit 過的 partition，所以可能比 Topic 表少")]),
           ("consumergroup", "topic"),
           [("寫入", d_prod(), [D0]), ("消費", d_cons(), [D0]), ("Lag 變化", D_LAG, LAG_CHANGE)],
           base=NOW_ROWS))
live.line(
    mtable("會不會沒讀就被刪",
           desc("沒讀的資料離被 Kafka 刪掉還有多遠。", "遠低於 70%、沒 commit 的 Partition 0。",
                "接近 100% → 加快消費；已掉資料 → 沒讀的已被刪，consumer 依 auto.offset.reset 跳過",
                "沒 commit 的 Partition > 0 → 這些 partition 的 lag 看不到（exporter 回報 -1）",
                cols=[("Lag 佔保留量", RATIO_HELP + "。沒人在讀 = 成員 0，% 沒意義不顯示")]),
           ("consumergroup", "topic"),
           [("Lag 佔保留量", RATIO_STATE, pct_col(vmap({"-2": ("沒人在讀", "#8e8e8e"), "-3": ("已掉資料（沒人讀）", "orange")}))),
            ("沒 commit 的 Partition", NO_COMMIT,
             [D0, {"id": "thresholds", "value": steps(("text", None), ("orange", 1))}, cell("color-text")])],
           base=NOW_ROWS),
    keep_positive(mtable(
        "有 lag 的 Partition（10 分鐘內有沒有前進）",
        desc("有 lag 的 partition，以及 commit 位置 10 分鐘內前進幾筆。", "前進 > 0。",
             "前進 0（紅）→ 卡住：某筆一直處理失敗，或沒有 consumer 分到", "只有一兩個 partition lag 特別大 → key 集中（熱點）"),
        ("consumergroup", "topic", "partition"),
        [("Lag", f'max by(consumergroup,topic,partition)({LAG}) > 0', [D0]),
         # 直接相減:offset 是 gauge,increase() 遇到往回重設會算錯;10 分鐘前還沒 commit 過 (-1) 不算
         ("10 分鐘前進", f'max by(consumergroup,topic,partition)({COMMITTED} - {COMMITTED} offset 10m '
                       f'and on(consumergroup,topic,partition) {COMMITTED} offset 10m >= 0)',
          [D0, {"id": "thresholds", "value": steps(("red", None), ("green", 1))}, cell("color-background"),
           {"id": "noValue", "value": "10 分鐘前還沒 commit"}])],
        base=f'max by(consumergroup,topic,partition)({LAG}) * 0', no_value="沒有 lag"), "Value #A"))

live.row("Topic")
live.line(
    mtable("Topic 規模",
           desc("每個 topic 的 partition、副本、大小。", "副本不足 0。", "副本不足 > 0 → 某台 broker 掛了或太慢",
                cols=[("磁碟（單份）", "不含副本；未收集 = jmx exporter 沒收 kafka.log:type=Log,name=Size")]),
           ("topic",),
           [("Partition", f'max by(topic)(kafka_topic_partitions{{{CT}{NOINT}}})', []),
            ("副本數", f'max by(topic)(kafka_topic_partition_replicas{{{CT}{NOINT}}})', []),
            ("磁碟（單份）", SIZE1, [{"id": "unit", "value": "bytes"}, {"id": "noValue", "value": "未收集"}]),
            ("保留訊息", f'sum by(topic)({retained(CT + NOINT)})', [{"id": "unit", "value": "short"}]),
            ("副本不足", f'sum by(topic)(kafka_topic_partition_under_replicated_partition{{{CT}{NOINT}}})',
             [{"id": "thresholds", "value": ALERT_COUNT}, cell("color-text")])],
           base=TOPIC_ROWS, sort="Partition"),
    mtable(f"Topic 流量（近 {WT} 平均）",
           desc("每個 topic 的寫入 / 讀取速度。", "不均度接近 1。",
                "不均度 > 2 → 熱點 partition（key 集中）", "讀取遠大於寫入 → 很多 group 讀，或有程式從頭重讀",
                cols=[("不均度", "最忙的 partition ÷ 平均，1 = 平均分散")]),
           ("topic",),
           [("寫入（筆/秒）", f'sum by(topic)(rate(kafka_server_brokertopicmetrics_messagesin_total{{{BT}}}[{RW}]))', [D1]),
            ("寫入", f'sum by(topic)(rate(kafka_server_brokertopicmetrics_bytesin_total{{{BT}}}[{RW}]))',
             [{"id": "unit", "value": "Bps"}]),
            ("讀取", f'sum by(topic)(rate(kafka_server_brokertopicmetrics_bytesout_total{{{BT}}}[{RW}]))',
             [{"id": "unit", "value": "Bps"}]),
            ("不均度", f'max by(topic)({TOPIC_RATE}) / (avg by(topic)({TOPIC_RATE}) > 0)',
             [D1, {"id": "noValue", "value": "—"}, cell("color-text"),
              {"id": "thresholds", "value": steps(("green", None), ("yellow", 1.5), ("red", 2))}])],
           base=TOPIC_ROWS))

live.row("連線與副本")
live.line(
    keep_positive(table(
        "連線的 client 程式",
        desc("連線中的 client 函式庫與版本（client 自己回報）。", "版本一致。",
             "很舊的版本 → 可能有已知 bug", "unknown → 太舊的 client，或連線還沒完成"),
        [tgt(f'sum by(client,listener)({CLIENT_SW_L})', ref="A", instant=True, fmt="table")],
        {"client": "Client 程式 / 版本", "listener": "Listener", "Value": "連線數"}, ["client", "listener", "Value"],
        sort="連線數", no_value=nojmx("kafka.server:type=socket-server-metrics")), "Value"),
    table("副本沒跟上的 Partition",
          desc("同步副本（ISR）比副本數少的 partition。", "全部正常。",
               "同一台 broker 的 partition 都在這 → 那台掛了或太慢"),
          [tgt(f'max by(topic,partition)(kafka_topic_partition_replicas{{{CT}}}) - '
               f'max by(topic,partition)(kafka_topic_partition_in_sync_replica{{{CT}}}) > 0', ref="A", instant=True, fmt="table")],
          {"topic": "Topic", "partition": "Partition", "Value": "少幾個副本"}, ["topic", "partition", "Value"],
          sort="少幾個副本", no_value="全部正常"))
live.line(
    bar("Leader 不在首選 broker",
        desc("leader 不在首選副本上的 partition 數。", "0。", "長時間 > 0 → 負載集中在部分 broker，請維運處理"),
        f'topk(15, sum by(topic)(1 - kafka_topic_partition_leader_is_preferred{{{CT}}}) > 0)', "{{topic}}",
        "short", decimals=0, no_value="全部在首選 broker"),
    bar("磁碟幾小時後滿",
        desc("照最近 6 小時剩餘空間減少的速度推估；空間沒在減少的不列。", "> 72 小時，或不列出。",
             "< 24 小時 → 請維運擴容或調 retention"),
        f'bottomk(10, (node_filesystem_avail_bytes{{{C},{FS}}} / -deriv(node_filesystem_avail_bytes{{{C},{FS}}}[6h])) / 3600 > 0)',
        "{{instance}} {{mountpoint}}", "h", decimals=0, th=steps(("red", None), ("yellow", 24), ("green", 72)),
        no_value="空間沒在減少"))

# ================================================================ 問題排查 dashboard (跟著時間範圍)
GUIDE = """| 症狀 | 先看 |
|---|---|
| lag 一直漲、處理延遲 | ② 期間表格、**消費 vs 寫入速度**、**預估追上需要** → ③ 讀取延遲 |
| Consumer 收不到資料 | ② **Consumer 成員數**（0 = 沒人讀）→ ④ 錯誤回應；卡住的 partition 看 **即時現況** |
| 寫入失敗、timeout、NOT_ENOUGH_REPLICAS | ① 離線 Partition / ISR 不足 → ④ 錯誤回應 → ③ 寫入延遲拆解 |
| 寫入變慢 | ③ 寫入延遲拆解 → ⑤ 被限速 → ⑥ Broker 資源 |
| 權限錯誤 / 登入失敗 | ④ 權限被拒 / 登入失敗 |
| 哪個帳號流量最大、有沒有被限速 | ⑤ 各帳號 |
| Topic 滿載、partition 不均 | ② Topic 寫入、熱點 Partition |
| 會不會沒讀就被刪 | ② **Lag 佔保留量** |
| 壓測回顧 | 時間範圍選壓測前到壓測後 → ① 期間最糟 → ② 期間表格 → 走勢圖看是什麼時候 |

看不到的：哪個帳號被拒（authorizer log）、每則訊息的端到端延遲（應用程式埋點）、Topic 設定（`kafka-configs --describe`）。
面板顯示「jmx exporter 設定沒收 X」或 ⑤ 沒資料 → 見 README。"""

main = Board()
main.line(text("**問題排查**：全部跟著上方時間範圍走。**壓測回顧** → 時間範圍選壓測前到壓測後，或在任一張圖上拖曳選取。"
               "**最新狀態** → 右上 **Kafka 即時現況**。依症狀找方向 → 點開下面「使用說明」。"), h=2)
main.row("使用說明：依症狀找方向（點開）", collapsed=True)
main.line(text(GUIDE), h=11)

main.row("① Kafka 本身有沒有壞（期間最糟）")
# 每個點取前一小段最糟 (subquery 解析度 = scrape),整段再取最糟:短暫的尖峰也抓得到,小走勢圖看得出是什麼時候
hp = [stat(t, desc(f"期間{'最高' if fn == 'max' else '最低'}（每次 scrape 都看）。{w}", n, *b),
           f'{fn}_over_time(({e})[{RI}:{SCRAPE}])', calc=fn, **kw) for t, w, n, b, e, le, fn, kw in HEALTH]
main.line(*hp[:4], h=4)
main.line(*hp[4:], h=4)

main.row("② Consumer 有沒有追上、Topic 是否滿載")
main.line(
    mtable("期間落後多少",
           desc("所選時間範圍內最糟的時候。", "最高 Lag 不大、有降回來。",
                "最高還落後超過業務要求 → 消費跟不上", "最長追上需要很久 → 壓測後要很久才恢復",
                cols=[("最高還落後", BEHIND_HELP),
                      ("最長追上需要", "期間每個時間點推估「還要多久追上」的最大值；— = 期間 lag 沒在減少")]),
           ("consumergroup", "topic"),
           [("最高 Lag", f'max_over_time(({LAGS})[{PR}:{SCRAPE}])', [D0]),
            ("最高還落後", f'max_over_time(({BEHIND})[{PR}:{SMOOTH}])', SECONDS + [{"id": "noValue", "value": "—"}]),
            ("最長追上需要", f'max_over_time(({CATCHUP})[{PR}:{SMOOTH}])',
             SECONDS + [{"id": "noValue", "value": "—"}, {"id": "mappings", "value": [ONE_DAY]}])],
           base=P_BASE),
    mtable("期間寫入 / 消費（筆）",
           desc(f"範圍頭尾相減（頭尾各取 {SMOOTH} 平均），**寫入 − 消費 = Lag 變化**。", "Lag 變化 ≤ 0。",
                "Lag 變化 > 0 → 範圍結束時還有積壓",
                cols=[("寫入、消費", "只算頭尾都 commit 過的 partition；「範圍開頭還沒有」= 範圍開始時這個 group 還沒 commit 過，"
                                    "把時間範圍的開頭往後調")]),
           ("consumergroup", "topic"),
           [("期間寫入", d_prod(PR), [D0, {"id": "noValue", "value": "範圍開頭還沒有"}]),
            ("期間消費", d_cons(PR), [D0, {"id": "noValue", "value": "範圍開頭還沒有"}]),
            ("Lag 變化", d_lag(PR), LAG_CHANGE + [{"id": "noValue", "value": "範圍開頭還沒有"}])],
           base=P_BASE))
main.line(
    mtable(f"期間處理速度（筆/秒，近 {WT} 平均的最高）",
           desc("期間寫入 / 消費速度最快的時候。", "最高消費 ≥ 最高寫入。",
                "最高消費明顯低於最高寫入 → consumer 處理能力是瓶頸（最高消費 ≈ 目前配置的上限）"),
           ("consumergroup", "topic"),
           [("最高寫入/秒", f'{P_BASE} + on(topic) group_left() max_over_time(({PROD_RATE})[{PR}:{SMOOTH}])', [D1]),
            ("最高消費/秒", f'max_over_time(({CONS_RATE})[{PR}:{SMOOTH}])', [D1])],
           base=P_BASE),
    mtable("期間 consumer 與保留",
           desc("期間 consumer 有沒有斷、資料有沒有差點被刪。", "最少成員 ≥ 1、成員變動 0（或只有部署那幾次）。",
                "最少成員 0 → 有一段沒人讀", "成員變動多 → 一直 rebalance", "最高佔保留量接近 / 超過 100% → 差點 / 已經掉資料",
                cols=[("最高佔保留量", "期間 Lag 佔保留量的最高值。" + RATIO_HELP + "。只看有成員的時候；— = 期間一直沒有成員")]),
           ("consumergroup", "topic"),
           [("最少成員", f'{P_BASE} + on(consumergroup) group_left() min by(consumergroup)(min_over_time({MEMBERS_RAW}[{PR}]))',
             MEMBERS_COL),
            ("成員變動", f'{P_BASE} + on(consumergroup) group_left() max by(consumergroup)(changes({MEMBERS_RAW}[{PR}]))',
             [D0, {"id": "thresholds", "value": steps(("green", None), ("orange", 1))}, cell("color-text")]),
            ("最高佔保留量", f'max_over_time({RATIO_ACTIVE}[{PR}:{SMOOTH}])', pct_col([]))],
           base=P_BASE, sort="最高佔保留量"))
main.line(
    # 每個點取前一小段的最高值:時間範圍拉長、點變稀時,才不會剛好取到 commit 後的低點而漏掉高點
    ts("Lag（筆）",
       desc("每個 group × topic 還沒讀的訊息數。每個點取前一小段的最高值，拉長時間範圍也不會漏掉高點。",
            "小且穩定，或升高後會降回來（隨 commit 小幅鋸齒正常）。",
            "一路往上 → 消費跟不上（看右邊速度）", "突然歸零又暴增 → offset 被重設或重新部署"),
       [tgt(f'max_over_time(({LAGS})[{RI}:{SCRAPE}])', "{{consumergroup}} → {{topic}}")]),
    ts(f"消費 vs 寫入速度（筆/秒，近 {WT} 平均）",
       desc("實線 = group 每秒讀完幾筆；虛線 = topic 每秒寫入幾筆。每個點是往前「比較區間」的平均，會比 Lag 圖慢半拍。",
            "實線貼著同 topic 的虛線。", "實線長期低於虛線 → lag 會一直漲", "實線掉到 0 → 程式停了或卡住"),
       [tgt(CONS_RATE, "{{consumergroup}} → {{topic}}"),
        tgt(f'sum by(topic)({TOPIC_RATE})', "寫入 → {{topic}}", ref="B")],
       overrides=[{"matcher": {"id": "byRegexp", "options": "寫入 → .*"},
                   "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                                  {"id": "custom.lineWidth", "value": 2}]}]))
main.line(
    ts(f"預估追上需要（照近 {WT} lag 減少的速度）",
       desc("每個時間點推估「照當時的速度，還要多久追上」。只畫 lag 正在減少的時候。",
            "lag 下降時數字越來越小、最後消失（追上了）。",
            "數字一直很大 → 恢復很慢", "完全沒有線、但 Lag 很大 → lag 沒在減少，追不上"),
       [tgt(CATCHUP, "{{consumergroup}} → {{topic}}")], unit="s", fill=0),
    ts(f"還落後多久的資料（估算：Lag ÷ 近 {WT} 平均寫入速度）",
       desc(BEHIND_HELP + "。要精準請在應用程式記錄訊息時間。", "幾秒內。",
            "持續上升 → 越落越多，跟業務要求比較", "線斷掉 → 那段時間沒有新寫入"),
       [tgt(BEHIND, "{{consumergroup}} → {{topic}}")], unit="s", fill=0))
main.line(
    ts("Consumer 成員數",
       desc("每個 group 連著幾個 consumer。", "等於部署數 × 每個程式的 consumer 數。",
            "掉到 0 → 沒人在讀", "一直跳動 → 一直 rebalance（處理太久超過 max.poll.interval.ms，或 pod 一直重啟）"),
       [tgt(MEMBERS, "{{consumergroup}}")], decimals=0, fill=0),
    ts("Lag 佔保留量（有成員時）",
       desc(RATIO_HELP + "。只畫有 consumer 的時候。", "遠低於 70%。",
            "往 100% 靠近 → 快讀不到就被刪了", "超過 100% → 已掉資料"),
       [tgt(RATIO_ACTIVE, "{{consumergroup}} → {{topic}}")], unit="percentunit", th=PCT, th_style="line", fill=0))
main.line(
    ts("Rebalance 次數",
       desc("每分鐘完成幾次 rebalance、正在 rebalance 的 group 數（整個叢集）。", "接近 0，部署、擴縮時短暫增加。",
            "持續發生 → 期間整個 group 停止消費；看成員數找是哪個 group"),
       [tgt(f'sum(rate(kafka_server_group_coordinator_metrics_group_completed_rebalance_count{{{C}}}[5m])) * 60',
            "完成的 rebalance（次/分）"),
        tgt(f'sum(kafka_coordinator_group_groupmetadatamanager_numgroupspreparingrebalance{{{C}}}) + '
            f'sum(kafka_coordinator_group_groupmetadatamanager_numgroupscompletingrebalance{{{C}}})',
            "正在 rebalance 的 group", ref="B")],
       decimals=1, no_value=nojmx("kafka.server:type=group-coordinator-metrics")),
    ts(f"熱點 Partition（寫入筆/秒，近 {WT} 平均，前 10）",
       desc("寫入最多的 10 個 partition。", "同一 topic 的 partition 差不多。",
            "某個遠高於同 topic 其他 → key 集中，加 consumer 沒用，要換 key 或打散"),
       [tgt(f'topk(10, sum by(topic,partition)({TOPIC_RATE}))', "{{topic}} [{{partition}}]")]))
main.line(
    ts(f"Topic 寫入（筆/秒，近 {WT} 平均）",
       desc("每個 topic 每秒寫入幾筆。", "跟業務量同步。",
            "突然暴增 → producer 重送或迴圈", "掉到 0 → producer 停了或寫入失敗（看 ④）"),
       [tgt(f'sum by(topic)(rate(kafka_server_brokertopicmetrics_messagesin_total{{{BT}}}[{RW}]))', "{{topic}}")]),
    ts(f"Topic 寫入 / 讀取流量（bytes/s，近 {WT} 平均）",
       desc("每個 topic 每秒寫入 / 讀取多少 bytes（讀取不含副本同步）。", "讀取 ≈ 寫入 × 讀它的 group 數。",
            "筆數不多但 bytes 很高 → 單筆訊息很大"),
       [tgt(f'sum by(topic)(rate(kafka_server_brokertopicmetrics_bytesin_total{{{BT}}}[{RW}]))', "寫入 {{topic}}"),
        tgt(f'sum by(topic)(rate(kafka_server_brokertopicmetrics_bytesout_total{{{BT}}}[{RW}]))', "讀取 {{topic}}", ref="B")],
       unit="Bps"))
main.line(
    ts("Topic 磁碟大小（單份）",
       desc("每個 topic 在磁碟上的大小（不含副本）。", "到達 retention 後持平。",
            "一直長 → retention 太長或沒設 retention.bytes"),
       [tgt(SIZE1, "{{topic}}")], unit="bytes", no_value=nojmx("kafka.log:type=Log,name=Size")))

main.row("③ 延遲：寫入 / 讀取慢在哪一段")
def breakdown(req):
    parts = [("totaltimems", "總計"), ("requestqueuetimems", "排隊等處理"), ("localtimems", "broker 本地處理（寫磁碟）"),
             ("remotetimems", "等副本 / 等資料"), ("responsequeuetimems", "排隊等回應"), ("responsesendtimems", "送回 client"),
             ("throttletimems", "被限速")]
    return [tgt(f'max(kafka_network_requestmetrics_{m}{{{C},request="{req}",quantile="0.99"}})', name, ref=chr(65 + i))
            for i, (m, name) in enumerate(parts)]
TOTAL_BOLD = [{"matcher": {"id": "byName", "options": "總計"},
               "properties": [{"id": "custom.lineWidth", "value": 3}, {"id": "custom.fillOpacity", "value": 0}]}]
main.line(
    ts("寫入延遲拆解（Produce p99，最慢的 broker）",
       desc("broker 處理 Produce 的時間拆成幾段（各段 p99，不能相加）。", "幾 ms～幾十 ms；acks=all 時「等副本」佔大部分。",
            "排隊等處理高 → broker 忙不過來（⑥）", "本地處理高 → 磁碟慢", "等副本高 → follower 跟不上（⑦）",
            "這裡正常但程式覺得慢 → 問題在 client 端（linger.ms、buffer 滿、網路）"),
       breakdown("Produce"), unit="ms", overrides=TOTAL_BOLD, fill=0),
    ts("讀取延遲拆解（FetchConsumer p99，最慢的 broker）",
       desc("consumer 的 Fetch 在 broker 花的時間（各段 p99）。",
            "約 500 ms：沒新資料時 broker 會等滿 fetch.max.wait.ms（預設 500 ms）才回。",
            "遠超過 fetch.max.wait.ms 或本地處理高 → 在讀很舊的資料或磁碟慢"),
       breakdown("FetchConsumer"), unit="ms", overrides=TOTAL_BOLD, fill=0))
main.line(
    ts("每秒請求數（依類型，前 10）",
       desc("每秒收到的各類請求數（全部 broker）。", "Produce / Fetch 為主。",
            "Metadata 很高 → client 一直重連或 topic 不存在", "OffsetCommit 很高 → 每筆都 commit",
            "JoinGroup / SyncGroup 多 → 一直 rebalance"),
       [tgt(f'topk(10, sum by(request)(rate(kafka_network_requestmetrics_requests_total{{{C}}}[{RI}])))', "{{request}}")],
       unit="reqps"),
    ts("寫入 / 讀取 p99（依 broker）",
       desc("每台 broker 的 Produce / FetchConsumer 總時間 p99。", "各台差不多。",
            "只有一台特別慢 → 那台有問題（磁碟、GC、負載不均）"),
       [tgt(f'max by(instance)(kafka_network_requestmetrics_totaltimems{{{C},request="Produce",quantile="0.99"}})', "寫入 {{instance}}"),
        tgt(f'max by(instance)(kafka_network_requestmetrics_totaltimems{{{C},request="FetchConsumer",quantile="0.99"}})',
            "讀取 {{instance}}", ref="B")],
       unit="ms", fill=0))

main.row("④ 錯誤與權限：應用程式收到什麼錯誤")
AUTH_ERR = 'error=~".*AUTHORIZATION_FAILED|SASL_AUTHENTICATION_FAILED|ILLEGAL_SASL_STATE|UNSUPPORTED_SASL_MECHANISM"'
main.line(
    ts("錯誤回應（依錯誤 × 請求，每分鐘）",
       desc("broker 回給 client 的錯誤（不含 NONE）。", "接近 0；換 leader 時短暫 NOT_LEADER_OR_FOLLOWER 正常。",
            "UNKNOWN_TOPIC_OR_PARTITION → topic 打錯或沒建", "NOT_ENOUGH_REPLICAS → ISR 不足（①）",
            "MESSAGE_TOO_LARGE → 訊息太大", "REBALANCE_IN_PROGRESS / UNKNOWN_MEMBER_ID → 一直 rebalance",
            "OFFSET_OUT_OF_RANGE → 要讀的資料已被刪"),
       [tgt(f'sum by(error,request)(rate(kafka_network_requestmetrics_errors_total{{{C},error!="NONE"}}[{RI}])) * 60 > 0',
            "{{error}} ({{request}})")],
       decimals=1, draw="points"),
    ts("權限被拒（依錯誤 × 請求，每分鐘）",
       desc("被 ACL 擋下的請求；紅虛線 = authorizer 拒絕總數。", "0。",
            "TOPIC_AUTHORIZATION_FAILED → 對 topic 沒有 Describe / Write / Read",
            "GROUP_AUTHORIZATION_FAILED → 對 group 沒有 Read",
            "有值 → 確認帳號、topic、group 名稱，請維運補 ACL（Kafka 不記是哪個帳號）"),
       [tgt(f'sum by(error,request)(rate(kafka_network_requestmetrics_errors_total{{{C},{AUTH_ERR}}}[{RI}])) * 60 > 0',
            "{{error}} ({{request}})"),
        tgt(authz("denied"), "authorizer 拒絕（次/分）", ref="B")],
       decimals=1,
       overrides=[{"matcher": {"id": "byName", "options": "authorizer 拒絕（次/分）"},
                   "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                                  {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]}]))
main.line(
    ts("登入失敗（依 listener，每分鐘）",
       desc("SASL 登入失敗次數。", "0。", "有值 → 帳號 / 密碼錯、帳號被刪或 mechanism 設錯（常見：剛換密碼、設定檔用錯環境）"),
       [tgt(f'sum by(listener)({by_listener("failed_authentication_rate")}) * 60', "{{listener}}")],
       decimals=1, no_value=nojmx("kafka.server:type=socket-server-metrics")),
    ts("寫入 / 讀取失敗（依 Topic，每分鐘）",
       desc("broker 處理 Produce / Fetch 失敗的次數（不含權限被拒）。", "0。",
            "有值 → 看「錯誤回應」是哪種錯"),
       [tgt(f'sum by(topic)(rate(kafka_server_brokertopicmetrics_failedproducerequests_total{{{BT}}}[{RI}])) * 60 > 0', "寫入失敗 {{topic}}"),
        tgt(f'sum by(topic)(rate(kafka_server_brokertopicmetrics_failedfetchrequests_total{{{BT}}}[{RI}])) * 60 > 0', "讀取失敗 {{topic}}", ref="B")],
       decimals=1, no_value="0"))
main.line(
    ts("ACL 授權檢查（每分鐘）",
       desc("authorizer 每分鐘允許 / 拒絕幾次；虛線 = ACL 規則總數（右軸）。", "拒絕接近 0。",
            "ACL 總數突然變少 → 有人刪了 ACL"),
       [tgt(authz("allowed"), "允許"), tgt(authz("denied"), "拒絕", ref="B"), tgt(ACLS, "ACL 總數", ref="C")],
       no_value=nojmx("kafka.server:type=plugins"),
       overrides=[{"matcher": {"id": "byName", "options": "拒絕"},
                   "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]},
                  {"matcher": {"id": "byName", "options": "ACL 總數"},
                   "properties": [{"id": "custom.axisPlacement", "value": "right"},
                                  {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]}]))

main.row("⑤ 各帳號（專案）與連線（點開）", collapsed=True)
main.line(
    ts("各帳號寫入流量",
       desc("每個帳號每秒寫入多少 bytes（帳號名的 - 可能顯示成 _）。", "跟業務量相符。", "突然暴增 → 那個專案在重送或迴圈"),
       [tgt(f'sum by(user)({by_user("produce", "byte_rate")})', "{{user}}")], unit="Bps", no_value=NOQUOTA),
    ts("各帳號讀取流量",
       desc("每個帳號每秒讀走多少 bytes。", "≈ 讀的 topic 寫入量 × group 數。", "遠大於平常 → 從頭重讀或多開了 group"),
       [tgt(f'sum by(user)({by_user("fetch", "byte_rate")})', "{{user}}")], unit="Bps", no_value=NOQUOTA))
main.line(
    ts("各帳號佔用 broker 處理時間",
       desc("每個帳號佔用 broker 處理執行緒的比例（100% = 一整條）。", "都不高。",
            "某帳號特別高 → 請求太頻繁（每筆都送、每筆都 commit），會拖慢所有人"),
       [tgt(f'sum by(user)({by_user("request", "request_time")})', "{{user}}")], unit="percent",
       no_value="沒有資料：要設 quota，jmx exporter 也要收 kafka.server:type=Request（見 README）"),
    ts("各帳號被限速時間",
       desc("broker 因帳號超過 quota 延後回應的時間。", "0。", "> 0 → 超過流量上限，寫入 / 讀取變慢；要調高找維運"),
       [tgt(f'max by(user)({by_user("produce", "throttle_time")}) > 0', "寫入 {{user}}"),
        tgt(f'max by(user)({by_user("fetch", "throttle_time")}) > 0', "讀取 {{user}}", ref="B"),
        tgt(f'max by(user)({by_user("request", "throttle_time")}) > 0', "請求 {{user}}", ref="C")],
       unit="ms", no_value="0（沒有被限速）"))
main.line(
    ts("連線數（依 listener）",
       desc("每個 listener 的連線數。", "穩定。", "一直上升 → 程式沒關 producer / consumer", "鋸齒狀 → 一直斷線重連"),
       [tgt(f'sum by(listener)({by_listener("connection_count")})', "{{listener}}")],
       decimals=0, no_value=nojmx("kafka.server:type=socket-server-metrics")))

main.row("⑥ Broker 資源：Kafka 主機吃不吃緊（點開）", collapsed=True)
main.line(
    ts("處理執行緒閒置率", desc("broker 處理請求與網路執行緒閒著的比例。", "> 30%。", "< 20% → 忙不過來，所有請求排隊變慢"),
       [tgt(f'rate(kafka_server_kafkarequesthandlerpool_requesthandleravgidlepercent_count_total{{{C}}}[5m]) / 1e9', "處理 {{instance}}"),
        tgt(f'kafka_network_socketserver_networkprocessoravgidlepercent{{{C}}}', "網路 {{instance}}", ref="B")],
       unit="percentunit", min_=0, max_=1, th=steps(("red", None), ("transparent", 0.2)), th_style="area", fill=0),
    ts("JVM Heap 使用率", desc("broker JVM heap 使用比例。", "鋸齒狀，GC 後降下來。", "一直貼近 100% → 記憶體不足"),
       [tgt(f'sum by(instance)(jvm_memory_used_bytes{{{C},area="heap"}}) / sum by(instance)(jvm_memory_max_bytes{{{C},area="heap"}})',
            "{{instance}}")],
       unit="percentunit", min_=0, max_=1, th=PCT, th_style="line", fill=0))
main.line(
    ts("GC 暫停時間比例", desc("每秒花在 GC 的時間比例。", "< 1%。", "> 5% → broker 停頓，client 看到延遲尖峰"),
       [tgt(f'sum by(instance,gc)(rate(jvm_gc_collection_seconds_sum{{{C}}}[{RI}]))', "{{instance}} {{gc}}")],
       unit="percentunit", fill=0),
    ts("主機 CPU 使用率", desc("broker 主機 CPU 使用率。", "< 70%。", "長時間 > 85% → 主機吃緊"),
       [tgt(f'1 - avg by(instance)(rate(node_cpu_seconds_total{{{C},mode="idle"}}[{RI}]))', "{{instance}}")],
       unit="percentunit", min_=0, max_=1, th=PCT, th_style="line", fill=0))
main.line(
    ts("磁碟使用率", desc("每個檔案系統用了多少。", "< 70%。", "> 85% → 快滿，滿了 broker 會停"),
       [tgt(f'1 - node_filesystem_avail_bytes{{{C},{FS}}} / node_filesystem_size_bytes{{{C},{FS}}}', "{{instance}} {{mountpoint}}")],
       unit="percentunit", min_=0, max_=1, th=PCT, th_style="line", fill=0),
    ts("磁碟忙碌率", desc("磁碟有多少時間在處理 I/O。", "< 60%。", "接近 100% → 磁碟是瓶頸"),
       [tgt(f'rate(node_disk_io_time_seconds_total{{{C},{DISK}}}[{RI}])', "{{instance}} {{device}}")],
       unit="percentunit", min_=0, max_=1, th=PCT, th_style="line", fill=0))
main.line(
    ts("主機網路流量", desc("broker 主機網卡收 / 送流量。", "低於頻寬 70%。", "貼近頻寬上限 → 網路是瓶頸"),
       [tgt(f'sum by(instance)(rate(node_network_receive_bytes_total{{{C},{NIC}}}[{RI}]))', "收 {{instance}}"),
        tgt(f'sum by(instance)(rate(node_network_transmit_bytes_total{{{C},{NIC}}}[{RI}]))', "送 {{instance}}", ref="B")],
       unit="Bps", fill=0))

main.row("⑦ 副本與 Partition 健康（維運用，點開）", collapsed=True)
main.line(
    ts("ISR 縮減 / 擴增（每分鐘）", desc("副本被踢出 / 加回同步清單的次數。", "0。", "一直縮了又加 → 某台 broker 時快時慢"),
       [tgt(f'sum(rate(kafka_server_replicamanager_isrshrinks_total{{{C}}}[{RI}])) * 60', "縮減"),
        tgt(f'sum(rate(kafka_server_replicamanager_isrexpands_total{{{C}}}[{RI}])) * 60', "擴增", ref="B")],
       decimals=1),
    ts("副本落後最多（筆）", desc("每台 broker 當 follower 時最多落後 leader 幾筆。", "接近 0。", "持續變大 → 跟不上，可能被踢出 ISR"),
       [tgt(f'max by(instance)(kafka_server_replicafetchermanager_maxlag{{{C}}})', "{{instance}}")]))
main.line(
    ts("KRaft 狀態", desc("每台 controller 的 KRaft 角色（1 = 是這個角色）。", "一台 leader，其他 follower。",
                        "一直換 leader → controller 不穩"),
       [tgt(f'max by(instance,state)(kafka_server_raft_metrics_current_state{{{C}}})', "{{instance}} {{state}}")],
       decimals=0, legend_calcs=("lastNotNull",)),
    ts("Metadata 同步落後", desc("broker 套用叢集 metadata 落後多久。", "< 1 秒。", "持續升高 → client 可能找錯 leader"),
       [tgt(f'max by(instance)(kafka_server_broker_metadata_metrics_last_applied_record_lag_ms{{{C}}})', "{{instance}}")],
       unit="ms"))


def var_query(name, label, query, regex="", multi=False, include_all=False, refresh=1, hide=0, all_value=None):
    v = {"name": name, "label": label, "type": "query", "datasource": DS,
         "query": {"query": query, "refId": "PrometheusVariableQueryEditor-VariableQuery"},
         "definition": query, "regex": regex, "refresh": refresh, "sort": 1, "hide": hide,
         "multi": multi, "includeAll": include_all, "current": {}, "options": []}
    if all_value:
        v["allValue"] = all_value
    return v

VARS = [
    {"name": "datasource", "label": "環境", "type": "datasource", "query": "prometheus",
     "regex": ARGS[2] if len(ARGS) > 2 else "", "refresh": 1, "hide": 0, "current": {}, "options": []},
    var_query("cluster", "Cluster", "label_values(kafka_brokers, cluster)", refresh=2),
    var_query("topic", "Topic", 'label_values(kafka_topic_partitions{cluster="$cluster"}, topic)', r"/^(?!__).*/",
              multi=True, include_all=True, refresh=2, all_value=".*"),
    var_query("consumergroup", "Consumer Group",
              'label_values(kafka_consumergroup_members{cluster="$cluster"}, consumergroup)',
              multi=True, include_all=True, refresh=2, all_value=".*"),
    # 值用秒數:查詢裡要拿來除 (追上需要),顯示文字才是 5m / 15m / 1h
    {"name": "compare_window", "label": "比較區間", "type": "custom", "query": "5m : 300,15m : 900,1h : 3600",
     "description": "速度、寫入 / 消費筆數、Lag 變化、追上需要都用這一段算：短 = 反應快，長 = 穩但慢半拍",
     "current": {"text": "5m", "value": "300"}, "hide": 0, "multi": False, "includeAll": False,
     "options": [{"text": t, "value": v, "selected": v == "300"}
                 for t, v in (("5m", "300"), ("15m", "900"), ("1h", "3600"))]},
]

def expand(panels):
    # 收合列裡的 panel 拿出來,後面的 panel 往下移
    for r in [p for p in panels if p["type"] == "row" and p["panels"]]:
        inner = r["panels"]
        top = min(p["gridPos"]["y"] for p in inner)
        hgt = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in inner) - top
        for p in inner:
            p["gridPos"]["y"] += r["gridPos"]["y"] + 1 - top
        for p in panels[panels.index(r) + 1:]:
            p["gridPos"]["y"] += hgt
        i = panels.index(r)
        panels[i + 1:i + 1] = inner
        r["panels"], r["collapsed"] = [], False

def dashboard(title, uid, desc_, board, is_live):
    d = {
        "title": title, "uid": uid, "description": desc_,
        "tags": ["kafka", "troubleshooting", LINK_TAG],
        "editable": True, "graphTooltip": 1, "fiscalYearStartMonth": 0, "liveNow": False,
        "refresh": "30s", "time": {"from": "now-15m" if is_live else "now-1h", "to": "now"},
        "timepicker": {"refresh_intervals": ["10s", "30s", "1m", "5m", "15m"]},
        "timezone": "browser", "schemaVersion": 41, "version": 1,
        # 右上角連到另一個 dashboard,帶著目前選的環境 / cluster / topic / group,時間不帶
        "links": [{"type": "dashboards", "tags": [LINK_TAG], "asDropdown": False, "includeVars": True,
                   "keepTime": False, "targetBlank": False, "title": "", "icon": "external link", "tooltip": ""}],
        "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                                  "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                                  "name": "Annotations & Alerts", "type": "dashboard"}]},
        "templating": {"list": json.loads(json.dumps(VARS))},
        "panels": board.panels,
    }
    if is_live:
        # 即時現況:藏起時間選擇器,永遠看最新 (面板沒有走勢圖,也不能拖曳選時間)
        d["timepicker"]["hidden"] = True
    if EXPAND:
        expand(board.panels)
        d["uid"] += "-expand"
        d["title"] += "（lab 展開）"
    return d

for path, d in [
    (OUT, dashboard("Kafka 問題排查（開發人員）", "kafka-dev-troubleshoot",
                    "跟著時間範圍走：回看過去、評估壓測。最新狀態看「Kafka 即時現況」。", main, False)),
    (OUT_LIVE, dashboard("Kafka 即時現況（開發人員）", "kafka-dev-troubleshoot-live",
                         "最新狀態，不受時間影響。回看過去、評估壓測看「Kafka 問題排查」。", live, True)),
]:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
print("main panels", main.count(), "live panels", live.count(), "max id", _id[0])
