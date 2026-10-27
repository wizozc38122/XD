# 產生「Kafka 問題排查（開發人員）」Grafana dashboard JSON
# 用法: python gen_dashboard.py 輸出.json [datasource 名稱 regex] [--expand] [--scrape=15s]
# 資料來源:jmx_exporter (官方 examples/kafka-kraft-3_0_0.yml 或自訂規則都支援) + danielqsj/kafka-exporter + node_exporter
import json, sys

EXPAND = "--expand" in sys.argv  # lab 截圖用:⑥⑦ 不收合
# kafka-exporter 的 scrape 間隔 (例如 15s、30s、1m),要跟 Prometheus 設定一致
SCRAPE = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--scrape=")), "15s")
SCRAPE_S = int(SCRAPE[:-1]) * {"s": 1, "m": 60}[SCRAPE[-1]]
ARGS = [a for a in sys.argv if not a.startswith("--")]

DS = {"type": "prometheus", "uid": "${datasource}"}
C = 'cluster="$cluster"'
CT = C + ',topic=~"$topic"'
CG = CT + ',consumergroup=~"$consumergroup"'
RI = "$__rate_interval"
# ② 的比較區間 (上方變數,值是秒數,顯示文字是 5m / 15m / 1h):速度、Lag 變化、追上需要全部用同一段。
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

_id = [0]
def nid():
    _id[0] += 1
    return _id[0]

def desc(what, normal, *bad, cols=()):
    # bad:每條一個「看到什麼 → 代表什麼 / 怎麼做」;cols:表格欄位說明 (欄名, 說明)
    out = f"**是什麼**：{what}\n\n"
    if cols:
        out += "**欄位**：\n" + "\n".join(f"- **{k}**：{v}" for k, v in cols) + "\n\n"
    return out + f"**正常**：{normal}\n\n**不正常時**：\n" + "\n".join(f"- {b}" for b in bad)

def tgt(expr, legend="__auto", ref="A", instant=False, fmt=None):
    t = {"datasource": DS, "editorMode": "code", "expr": expr, "legendFormat": legend, "refId": ref,
         "range": not instant, "instant": instant}
    if fmt:
        t["format"] = fmt
    return t

def steps(*pairs):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in pairs]}

def ts(title, d, targets, gp, unit="short", th=None, th_style="off", min_=None, max_=None,
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
            "gridPos": gp, "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
            "options": {"legend": {"displayMode": "table", "placement": "right", "showLegend": True,
                                   "calcs": list(legend_calcs), "sortBy": "Max", "sortDesc": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}

def stat(title, d, expr, gp, unit="none", th=None, mappings=None, no_value=None, decimals=None, calc=None):
    # calc 有給 = 看整個所選時間範圍 (range 查詢,依 calc 取 max / min,畫小走勢圖);沒給 = 只看最後一刻
    defaults = {"unit": unit, "color": {"mode": "thresholds"}, "thresholds": th or steps(("blue", None)),
                "mappings": mappings or []}
    if no_value: defaults["noValue"] = no_value
    if decimals is not None: defaults["decimals"] = decimals
    return {"id": nid(), "type": "stat", "title": title, "description": d, "datasource": DS, "gridPos": gp,
            "targets": [tgt(expr, instant=not calc)],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"reduceOptions": {"calcs": [calc or "lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "area" if calc else "none", "textMode": "value",
                        "justifyMode": "center", "orientation": "auto", "wideLayout": True,
                        "showPercentChange": False}}

def bar(title, d, expr, legend, gp, unit, decimals=None, th=None):
    defaults = {"unit": unit, "min": 0, "color": {"mode": "thresholds" if th else "continuous-BlYlRd"},
                "thresholds": th or steps(("green", None))}
    if decimals is not None: defaults["decimals"] = decimals
    return {"id": nid(), "type": "bargauge", "title": title, "description": d, "datasource": DS, "gridPos": gp,
            "targets": [tgt(expr, legend, instant=True)],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                        "valueMode": "text", "namePlacement": "left", "sizing": "auto",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}

def col(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}

def table(title, d, targets, gp, rename, order, overrides=(), sort=None, exclude=(), no_value=None, filterable=True, min_width=None):
    ex = {"Time": True}
    ex.update({k: True for k in exclude})
    tr = [{"id": "merge", "options": {}},
          {"id": "organize", "options": {"excludeByName": ex,
                                         "indexByName": {k: i for i, k in enumerate(order)},
                                         "renameByName": rename}}]
    if sort:
        tr.append({"id": "sortBy", "options": {"sort": [{"field": sort[0], "desc": sort[1]}]}})
    defaults = {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": filterable},
                "thresholds": steps(("green", None))}
    if no_value: defaults["noValue"] = no_value
    if min_width: defaults["custom"]["minWidth"] = min_width
    return {"id": nid(), "type": "table", "title": title, "description": d, "datasource": DS, "gridPos": gp,
            "targets": targets, "transformations": tr,
            "fieldConfig": {"defaults": defaults, "overrides": list(overrides)},
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}}

# 即時面板:固定看最新狀態,不管上方選的時間範圍 (Grafana 面板的 Relative time 覆寫)。
# 表格 / 單一數字是 instant 查詢,只看時間範圍結束那一刻;沒有覆寫的話,選過去的時間就是看那時候
def live(p):
    p["timeFrom"] = "5m"
    p["hideTimeOverride"] = True
    return p

PR = "$__range"
LIVE_ROW = "（即時：最新狀態，不受上方時間範圍影響）"
RANGE_ROW = "（所選時間範圍：回看過去、評估壓測）"

def text(content, gp):
    return {"id": nid(), "type": "text", "title": "", "gridPos": gp,
            "options": {"mode": "markdown", "content": content}}

def row(title, y, collapsed=False, panels=None):
    return {"id": nid(), "type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": panels or []}

def gp(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}

def vmap(d):
    return [{"type": "value", "options": {k: {"text": t, "color": c, "index": i}
                                          for i, (k, (t, c)) in enumerate(d.items())}}]

def cell(kind):
    return {"id": "custom.cellOptions", "value": {"type": kind}}

ALERT_COUNT = steps(("green", None), ("red", 1))
WARN_COUNT = steps(("green", None), ("orange", 1))
PCT = steps(("green", None), ("yellow", 0.7), ("red", 0.85))

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
NOQUOTA = "沒有資料：Kafka 要有設定 quota 才會記錄每個帳號的流量（請維運設定預設帳號 quota，見最上方說明）"
def nojmx(mbean):
    return f"沒有資料：jmx exporter 設定沒收 {mbean}（見最上方說明）"

GUIDE = """### 這個 Dashboard 怎麼用
上方先選 **環境 (Prometheus)** → **Cluster**，再視需要選 **Topic** / **Consumer Group**（預設全部）。每張圖標題旁的 **ⓘ** 都有「是什麼／正常／不正常時」說明；② 的速度、Lag 變化都用 **比較區間** 這一段算，標題會寫是哪一段。

**兩種區塊**：① ② 的 **即時** 那一排、以及標題有「（即時）」的面板，永遠是最新狀態，不管上方選什麼時間；其他都跟著上方時間範圍走。**回看 / 壓測**：時間範圍選壓測開始前到結束後（或在任一張圖上拖曳選取），看 ① **所選時間範圍內最糟**、② **期間總結** 和下面的走勢圖。

| 症狀 | 先看哪裡 |
|---|---|
| 消費很慢、資料處理延遲、lag 一直漲 | ② **Consumer Group 現況**（Lag 變化、追上需要）→ **消費 vs 寫入速度** → ④ 讀取延遲 |
| Consumer 收不到資料 | ② **成員數**（0 = 沒有程式在讀）、**卡住的 partition** → ④ **錯誤回應** |
| 寫入失敗、timeout、NOT_ENOUGH_REPLICAS | ① 健康總覽（離線 partition / ISR 不足）→ ④ **錯誤回應** → ③ **寫入延遲拆解** |
| 寫入變慢 | ③ **寫入延遲拆解**（慢在排隊、寫磁碟還是等副本）→ ⑤ **被限速** → ⑥ Broker 資源 |
| 權限錯誤（TOPIC / GROUP_AUTHORIZATION_FAILED） | ④ **權限被拒（依錯誤 / 請求類型）** |
| 登入失敗（SaslAuthenticationException） | ④ **登入失敗** |
| 想知道哪個帳號（專案）流量最大、有沒有被限速 | ⑤ **各帳號** 開頭的圖表、**連線的 client 程式** |
| Topic 是否滿載、partition 不均、磁碟夠不夠 | ② **Topic 總表** → **熱點 partition** → **磁碟幾小時後滿** |
| 資料會不會還沒讀就被刪掉 | ② Consumer Group 現況 **Lag 佔保留量**（有人在讀、接近 100% 就快掉資料；超過 = 已掉資料） |
| 壓測後回顧：期間有沒有跟不上、掉資料、rebalance | ② **期間總結**（最高 Lag、最高消費/秒、最少成員、最高 Lag 佔保留量）→ 下面的 Lag / 速度圖看是什麼時候 |

- **這裡看不到的**：哪個帳號被拒（看 broker 的 authorizer log）、每則訊息的端到端延遲（要應用程式埋點）、Topic 設定（`kafka-configs --describe`）
- **面板顯示「jmx exporter 設定沒收 X」**：請維運在 jmx exporter 設定的 `includeObjectNames` 加上 X
- **⑤ 各帳號沒資料**：broker 要設預設帳號 quota（設很大就不會限速），例如：

```
kafka-configs --alter --entity-type users --entity-default \\
  --add-config producer_byte_rate=1073741824,consumer_byte_rate=1073741824,request_percentage=10000
```"""

panels = []
y = 0
panels.append(text(GUIDE, gp(0, y, 24, 21)))
y += 21

# ① 健康總覽
AUTHZ_ERR = f'sum(rate(kafka_network_requestmetrics_errors_total{{{C},error=~".*AUTHORIZATION_FAILED"}}[2m])) * 60'
LOGIN_FAIL = f'sum({by_listener("failed_authentication_rate")}) * 60'
IDLE_GROUPS = (f"count((sum by(consumergroup)(clamp_min(kafka_consumergroup_lag{{{C}}}, 0)) > 0) and on(consumergroup) "
               f"(max by(consumergroup)(kafka_consumergroup_members{{{C}}}) == 0))")
# (標題, 是什麼, 正常, 不正常時, 查詢, 即時另外用的查詢, 期間最糟取 max 或 min, 其他參數)
HEALTH = [
    ("Broker 數", "在叢集裡的 broker 台數。", "等於部署台數。", ["變少 → 有 broker 掛了，看旁邊幾格是否也變紅"],
     f"max(kafka_brokers{{{C}}})", None, "min", dict(no_value="exporter 沒資料")),
    ("離線 Partition", "沒有 leader、完全不能讀寫的 partition 數。", "0。",
     ["> 0 → 這些 partition 讀寫都會失敗或 timeout，立刻找維運"],
     f"max(kafka_controller_kafkacontroller_offlinepartitionscount{{{C}}})", None, "max", dict(th=ALERT_COUNT)),
    ("ISR 不足（寫入會失敗）", "同步副本數少於 min.insync.replicas 的 partition 數。", "0。",
     ["> 0 → 用 acks=all 寫入會收到 NOT_ENOUGH_REPLICAS"],
     f"sum(kafka_server_replicamanager_underminisrpartitioncount{{{C}}})", None, "max", dict(th=ALERT_COUNT)),
    ("副本不足", "有副本沒跟上 leader 的 partition 數。", "0（broker 重啟時短暫 > 0 正常）。",
     ["長時間 > 0 → 某台 broker 掛了或太慢，資料少一份備份；讀寫通常還正常"],
     f"sum(kafka_server_replicamanager_underreplicatedpartitions{{{C}}})", None, "max", dict(th=WARN_COUNT)),
    ("Active Controller", "負責管理叢集的 controller 數（期間那格取最低）。", "剛好 1。",
     ["0 → 建 topic、換 leader 都會失敗", "> 1 → 不該發生，找維運"],
     f"sum(kafka_controller_kafkacontroller_activecontrollercount{{{C}}})", None, "min",
     dict(th=steps(("red", None), ("green", 1), ("red", 2)))),
    ("權限錯誤（次/分）", "client 收到 *_AUTHORIZATION_FAILED 的次數（每分鐘，全部 broker）。", "0。",
     ["> 0 → 有程式用了沒權限的帳號 / topic / group，看 ④ 權限被拒"],
     AUTHZ_ERR, None, "max", dict(th=WARN_COUNT, decimals=0, no_value="0")),
    ("登入失敗（次/分）", "SASL 登入失敗次數（每分鐘；即時那格取近 5 分鐘最高）。", "0。",
     ["> 0 → 有程式密碼錯、帳號被刪或 mechanism 設錯，看 ④ 登入失敗",
      "「未收集」→ jmx exporter 沒收 kafka.server:type=socket-server-metrics"],
     LOGIN_FAIL, f'max_over_time(({LOGIN_FAIL})[5m:30s])', "max", dict(th=WARN_COUNT, decimals=0, no_value="未收集")),
    ("沒人讀的 Group", "有 lag、但沒有任何 consumer 連著的 group 數。", "0（跑完就停的批次程式也會算進來）。",
     ["> 0 → 程式掛了或停了，訊息一直堆；看 ② 成員數"],
     IDLE_GROUPS, None, "max", dict(th=WARN_COUNT, no_value="0")),
]
H = 3
panels.append(row("① 健康總覽：Kafka 本身有沒有壞" + LIVE_ROW, y)); y += 1
for i, (title, what, normal, bad, expr, live_expr, fn, kw) in enumerate(HEALTH):
    panels.append(live(stat(title, desc(what, normal, *bad), live_expr or expr,
                            gp(i % 4 * 6, y + i // 4 * H, 6, H), **kw)))
y += 2 * H
panels.append(row("① 健康總覽：期間最糟" + RANGE_ROW, y)); y += 1
for i, (title, what, normal, bad, expr, live_expr, fn, kw) in enumerate(HEALTH):
    word = "最高" if fn == "max" else "最低"
    # 每個點取前一小段最糟 (subquery 解析度 = scrape),整段再取最糟:短暫的尖峰也抓得到,小走勢圖看得出是什麼時候
    panels.append(stat(title, desc(f"**所選時間範圍內{word}**（每次 scrape 都看，短暫尖峰也抓得到；小圖是期間走勢）。{what}",
                                   normal, *bad),
                       f'{fn}_over_time(({expr})[{RI}:{SCRAPE}])', gp(i % 4 * 6, y + i // 4 * H, 6, H), calc=fn, **kw))
y += 2 * H

# ② Consumer 與 Topic
panels.append(row("② Consumer 有沒有追上、Topic 是否滿載" + LIVE_ROW, y)); y += 1
LAG = f'kafka_consumergroup_lag{{{CG}}}'
COMMITTED = f'kafka_consumergroup_current_offset{{{CG}}}'
END = f'kafka_topic_partition_current_offset{{{CT}}}'
# kafka-exporter:group 沒 commit 過的 partition,commit 位置和 lag 都回 -1;-1 → 真的位置會被當成一口氣讀了整段,要排除
# offset 是 gauge:被往回重設時 rate() 會當成 counter reset 算出暴增,有往回的區間也不算
CONS_RATE = (f'sum by(consumergroup,topic)(rate({COMMITTED}[{RW}]) unless resets({COMMITTED}[{RW}]) > 0 '
             f'and min_over_time({COMMITTED}[{RW}]) >= 0)')
PROD_RATE = f'sum by(topic)(rate({END}[{RW}]))'
# 比較區間頭尾相減:頭尾各取一小段平均 (抹掉 commit 造成的鋸齒),中間剛好隔一個比較區間。
# 只算頭尾都 commit 過的 partition。exporter 算 lag 時的 log end 是另外跟 broker 要的,不是 topic 的 current_offset,
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
D_LAG, D_CONS, D_PROD = d_lag(), d_cons(), d_prod()
FLAT = 10  # Lag 變化在 ±10 筆內視為持平
def retained(sel):
    return f'(kafka_topic_partition_current_offset{{{sel}}} - kafka_topic_partition_oldest_offset{{{sel}}})'
RETAINED = retained(CT)
LAGS = f'sum by(consumergroup,topic)(clamp_min({LAG}, 0))'
# 還沒 commit 過的 partition 數:lag 看不到 (算 0),寫入 / 消費 / Lag 變化 也不算它們
NO_COMMIT = f'count by(consumergroup,topic)({COMMITTED} == -1) or {LAGS} * 0'
MEMBERS = f'max by(consumergroup)(kafka_consumergroup_members{{{C},consumergroup=~"$consumergroup"}})'
# 還落後 = lag / 寫入速度 (只用在圖上,是估算);比較區間內沒有寫入就沒有值 (不能拿 0 除)
BEHIND = f'({LAGS} / on(topic) group_left() ({PROD_RATE} > 0))'
RATIO = f'max by(consumergroup,topic)(clamp_min({LAG}, 0) / on(topic,partition) group_left() clamp_min({RETAINED}, 1))'
# 只看有成員的時候:沒人讀的 group 遲早都是 100%,% 沒意義
RATIO_ACTIVE = f'({RATIO} and on(consumergroup) {MEMBERS} > 0)'
# 單份大小 (不含副本):每個 partition 取最大的那份副本 (jmx exporter 要收 kafka.log:type=Log,name=Size)
SIZE1 = f'sum by(topic)(max by(topic,partition)(kafka_log_log_size{{{CT},topic!~"__.*"}}))'
cg_targets = [
    tgt(f'sum by(consumergroup,topic)(clamp_min({LAG}, 0))', ref="A", instant=True, fmt="table"),
    tgt(f'max by(consumergroup,topic)({LAG}) * 0 + on(consumergroup) group_left() '
        f'max by(consumergroup)(kafka_consumergroup_members{{{C},consumergroup=~"$consumergroup"}})',
        ref="B", instant=True, fmt="table"),
    tgt(D_PROD, ref="C", instant=True, fmt="table"),
    tgt(D_CONS, ref="D", instant=True, fmt="table"),
    tgt(D_LAG, ref="E", instant=True, fmt="table"),
    # 追上要多久,依序判斷 (or 左邊優先):lag 0 → 已跟上 (-1);沒有 consumer → 沒人在讀 (-2);
    # lag 不到 30 秒的寫入量 → 已跟上 (-1);lag 有在減少 → lag ÷ 每秒減少量;lag 在增加 → 越落越多 (-3);
    # 持平 → 沒在追上 (-4);比較區間前還沒有資料 → 沒有值
    tgt(f'(({LAGS} == 0) * 0 - 1) '
        f'or ({LAGS} * 0 - 2 and on(consumergroup) ({MEMBERS} == 0)) '
        f'or ({LAGS} * 0 - 1 and on(consumergroup,topic) ({LAGS} * {WS} < {D_PROD} * 30)) '
        f'or ({LAGS} / (0 - {D_LAG} > {FLAT}) * {WS}) '
        f'or ({LAGS} * 0 - 3 and on(consumergroup,topic) ({D_LAG} > {FLAT})) '
        f'or ({LAGS} * 0 - 4 and on(consumergroup,topic) {D_LAG})',
        ref="F", instant=True, fmt="table"),
    # lag 佔保留量:取最糟的 partition。沒人讀的 group 遲早都是 100%,% 沒意義 → 依序 (or 左邊優先):
    # 有成員 → %;沒成員且 > 100% → 已掉資料 (沒人讀) (-3);沒成員且有 lag → 沒人在讀 (-2);其餘 (lag 0) → %
    tgt(f'({RATIO} and on(consumergroup) ({MEMBERS} > 0)) '
        f'or (({RATIO} > 1) * 0 - 3 and on(consumergroup) ({MEMBERS} == 0)) '
        f'or (({RATIO} > 0) * 0 - 2 and on(consumergroup) ({MEMBERS} == 0)) '
        f'or {RATIO}',
        ref="G", instant=True, fmt="table"),
    tgt(NO_COMMIT, ref="H", instant=True, fmt="table"),
]
panels.append(live(table(
    f"Consumer Group 現況（寫入 / 消費 / Lag 變化 = 近 {WT}）",
    desc("每個 group × topic 現在追得怎樣。寫入、消費、Lag 變化都是上方「比較區間」頭尾相減的筆數"
         f"（頭尾各取 {SMOOTH} 平均，抹掉 commit 造成的起伏），所以一定對得上：**寫入 − 消費 = Lag 變化**（四捨五入可能差 1）。",
         "成員 ≥ 1、追上需要顯示「已跟上」。",
         "成員 0 → 程式沒在跑",
         "追不上（越落越多）→ 處理太慢：加 consumer（不能超過 partition 數）或加快處理",
         "Lag 佔保留量接近 100%（有成員）→ 沒讀到的資料快被刪掉；「已掉資料」→ 已經刪了；「沒人在讀」→ 先處理成員 0（各情境見欄位說明）",
         "想看更長的走勢、或過去某段時間 → 看下方「所選時間範圍」的期間總結與 Lag 圖",
         "沒 commit 的 Partition > 0 → 這些 partition 的 lag 看不到；確認是不是沒有 consumer 分到，或程式只在處理到資料時才 commit",
         cols=[("Lag", "現在還沒讀的訊息數。已處理但還沒 commit 的也算，所以會隨 commit 小幅起伏"),
               ("成員", "連著的 consumer 數，0 = 沒人在讀"),
               ("寫入、消費", "比較區間內寫進 / 讀完（commit）幾筆，只算這個 group 有 commit 過的 partition，"
                           "所以可能比 Topic 總表少（看「沒 commit 的 Partition」）。消費是負數 = offset 被往回重設（重讀）"),
               ("Lag 變化", f"比較區間內 lag 增減幾筆（= 寫入 − 消費）。正數 = 越落越多；負數 = 正在追上；±{FLAT} 筆內視為持平"),
               ("追上需要", "Lag ÷ 比較區間內 lag 減少的速度。"
                          "已跟上 = 沒有 lag，或 lag 不到 30 秒的寫入量；沒人在讀 = 有 lag 但沒有 consumer；"
                          f"追不上（越落越多）= Lag 變化 > {FLAT}；沒在追上（持平）= Lag 變化在 ±{FLAT} 內；"
                          "> 1 天 = lag 減少得很慢；資料不足 = 比較區間開頭還沒有這個 group 的資料"),
               ("Lag 佔保留量", "還沒讀的筆數 ÷ Kafka 這個 partition 現在還留著的筆數（取最糟的 partition）。"
                            "例：還留著 1,000 筆、其中 950 筆沒讀 = 95%。只用 offset 算，看不出 Kafka 什麼時候會刪"
                            "（retention 設多少要用 `kafka-configs --describe --entity-type topics --entity-name <topic>` 查）。\n"
                            "  - **0% ~ 70%**：離會被刪的資料還很遠\n"
                            "  - **接近 100%**（有成員）：快讀不到就被刪了 → 加快消費\n"
                            "  - **100% 不一定很急**：Kafka 是整個 segment 一起刪，還在寫的那個 segment 不會刪。"
                            "partition 只留幾筆時（例：留 6 筆、6 筆都沒讀 = 100%）通常要等好幾天才會刪，搭配 Lag 筆數看\n"
                            "  - **已掉資料**（有成員、超過 100%）：沒讀的已經被刪了，consumer 會收到 OFFSET_OUT_OF_RANGE，"
                            "依 auto.offset.reset 跳到最舊或最新\n"
                            "  - **沒人在讀**（成員 0、有 lag）：沒人讀的 group 遲早都會到 100%，% 沒意義，所以不顯示。"
                            "確認程式該不該在跑；不用了就刪掉 group（`kafka-consumer-groups --delete --group <group>`）\n"
                            "  - **已掉資料（沒人讀）**（成員 0、超過 100%）：沒人讀的期間資料已被刪；程式重開時會依 auto.offset.reset 跳過被刪的部分"),
               ("沒 commit 的 Partition", "這個 group 在這個 topic 還沒 commit 過的 partition 數。"
                                        "kafka-exporter 對這些 partition 回報 lag = -1，所以 Lag 算 0、寫入 / 消費 / Lag 變化也不算它們。"
                                        "整個 topic 都沒 commit 過時，exporter 不會列出這個 group × topic")]),
    cg_targets, gp(0, y, 24, 9),
    rename={"consumergroup": "Consumer Group", "topic": "Topic", "Value #A": "Lag", "Value #B": "成員",
            "Value #C": "寫入", "Value #D": "消費", "Value #E": "Lag 變化", "Value #F": "追上需要",
            "Value #G": "Lag 佔保留量", "Value #H": "沒 commit 的 Partition"},
    order=["consumergroup", "topic", "Value #A", "Value #B", "Value #C", "Value #D", "Value #E", "Value #F",
           "Value #G", "Value #H"],
    sort=("Lag", True), filterable=False, min_width=60,
    overrides=[
        col("Consumer Group", [{"id": "custom.width", "value": 150}]),
        col("成員", [{"id": "thresholds", "value": steps(("red", None), ("green", 1))}, cell("color-text"),
                     {"id": "custom.width", "value": 70}]),
        col("寫入", [{"id": "decimals", "value": 0}]),
        col("消費", [{"id": "decimals", "value": 0}]),
        col("Lag 變化", [{"id": "decimals", "value": 0},
                         {"id": "thresholds", "value": steps(("green", None), ("orange", FLAT))}, cell("color-text")]),
        # 沒有值 = 比較區間開頭還沒有資料
        col("追上需要", [{"id": "unit", "value": "s"}, {"id": "noValue", "value": "資料不足"},
                         {"id": "mappings", "value": vmap({"-1": ("已跟上", "green"), "-2": ("沒人在讀", "red"),
                                                           "-3": ("追不上（越落越多）", "red"),
                                                           "-4": ("沒在追上（持平）", "orange")}) + [
                             {"type": "range", "options": {"from": 86400, "to": None,
                                                           "result": {"text": "> 1 天（減少得很慢）", "color": "orange", "index": 4}}}]},
                         cell("color-text"),
                         {"id": "thresholds", "value": steps(("orange", None))}]),
        col("Lag 佔保留量", [{"id": "unit", "value": "percentunit"}, {"id": "min", "value": 0}, {"id": "max", "value": 1},
                            {"id": "thresholds", "value": PCT}, cell("gauge"), {"id": "custom.width", "value": 160},
                            # > 100% = consumer 停的位置比最舊還留著的資料更舊 → 沒讀的已被刪
                            {"id": "mappings", "value": vmap({"-2": ("沒人在讀", "#8e8e8e"),
                                                              "-3": ("已掉資料（沒人讀）", "orange")}) + [
                                {"type": "range", "options": {
                                    "from": 1.000001, "to": None, "result": {"text": "已掉資料", "color": "red", "index": 2}}}]}]),
        col("沒 commit 的 Partition", [{"id": "decimals", "value": 0}, {"id": "custom.width", "value": 110},
                                      {"id": "thresholds", "value": steps(("text", None), ("orange", 1))}, cell("color-text")]),
    ])))
y += 9
stuck_t = [
    tgt(f'max by(consumergroup,topic,partition)({LAG}) > 0', ref="A", instant=True, fmt="table"),
    # 直接相減:offset 是 gauge,increase() 遇到往回重設會當成 counter reset 算錯
    tgt(f'max by(consumergroup,topic,partition)({COMMITTED} - {COMMITTED} offset 10m '
        f'and on(consumergroup,topic,partition) {COMMITTED} offset 10m >= 0)',
        ref="B", instant=True, fmt="table"),
]
panels.append(live(table(
    "有 lag 的 Partition（10 分鐘內有沒有前進）",
    desc("有 lag 的 partition，以及 10 分鐘內 consumer 在上面前進了幾筆（現在的 commit 位置 − 10 分鐘前）。", "前進 > 0（綠色）。",
         "Lag > 0 但前進 0（紅色）→ 這個 partition 卡住：某筆訊息一直處理失敗，或沒有 consumer 分到",
         "前進是負數 → offset 被往回重設（重讀）", "前進沒有值 → 10 分鐘前這個 partition 還沒 commit 過",
         "只有一兩個 partition lag 特別大 → key 集中（熱點）"),
    stuck_t, gp(0, y, 9, 9),
    rename={"consumergroup": "Consumer Group", "topic": "Topic", "partition": "Partition",
            "Value #A": "Lag（筆）", "Value #B": "10 分鐘前進（筆）"},
    order=["consumergroup", "topic", "partition", "Value #A", "Value #B"], sort=("Lag（筆）", True),
    overrides=[col("10 分鐘前進（筆）", [{"id": "thresholds", "value": steps(("red", None), ("green", 1))},
                                       cell("color-background"), {"id": "decimals", "value": 0}])],
    no_value="沒有 lag")))
# 只留 Lag > 0 的列 (B 會列出所有 partition)
panels[-1]["transformations"].insert(1, {"id": "filterByValue", "options": {
    "filters": [{"fieldName": "Value #A", "config": {"id": "greater", "options": {"value": 0}}}],
    "type": "include", "match": "all"}})
topic_t = [
    tgt(f'max by(topic)(kafka_topic_partitions{{{CT},topic!~"__.*"}})', ref="A", instant=True, fmt="table"),
    tgt(f'max by(topic)(kafka_topic_partition_replicas{{{CT},topic!~"__.*"}})', ref="B", instant=True, fmt="table"),
    tgt(f'sum by(topic)(rate(kafka_server_brokertopicmetrics_messagesin_total{{{BT}}}[{RW}]))', ref="C", instant=True, fmt="table"),
    tgt(f'sum by(topic)(rate(kafka_server_brokertopicmetrics_bytesin_total{{{BT}}}[{RW}]))', ref="D", instant=True, fmt="table"),
    tgt(f'sum by(topic)(rate(kafka_server_brokertopicmetrics_bytesout_total{{{BT}}}[{RW}]))', ref="E", instant=True, fmt="table"),
    # 不均度 = 最忙 partition ÷ 平均
    tgt(f'max by(topic)(rate(kafka_topic_partition_current_offset{{{CT},topic!~"__.*"}}[{RW}])) / '
        f'(avg by(topic)(rate(kafka_topic_partition_current_offset{{{CT},topic!~"__.*"}}[{RW}])) > 0)', ref="F", instant=True, fmt="table"),
    # 單份大小 (每個 partition 取最大的那份副本)
    tgt(SIZE1, ref="G", instant=True, fmt="table"),
    tgt(f'sum by(topic)({retained(CT + ',topic!~"__.*"')})', ref="H", instant=True, fmt="table"),
    tgt(f'sum by(topic)(kafka_topic_partition_under_replicated_partition{{{CT},topic!~"__.*"}})', ref="I", instant=True, fmt="table"),
]
panels.append(live(table(
    f"Topic 總表（流量 = 近 {WT} 平均）",
    desc("每個 topic 的規模與流量（比較區間平均）。", "不均度接近 1、副本不足 0。",
         "不均度 > 2 → 熱點 partition（看下方熱點圖）",
         "每個 partition 寫入長期超過幾 MB/s → consumer 容易跟不上，考慮加 partition",
         "讀取遠大於寫入 → 很多 group 讀同一份，或有程式一直從頭重讀",
         cols=[("不均度", "最忙的 partition ÷ 平均，1 = 平均分散"),
               ("磁碟（單份）", "不含副本，實際佔用 ≈ × 副本數；「未收集」= jmx exporter 沒收 kafka.log:type=Log,name=Size"),
               ("保留訊息", "Kafka 目前還留著的訊息數")]),
    topic_t, gp(9, y, 15, 9),
    rename={"topic": "Topic", "Value #A": "Partition", "Value #B": "副本數", "Value #C": "寫入（筆/秒）",
            "Value #D": "寫入", "Value #E": "讀取", "Value #F": "不均度", "Value #G": "磁碟（單份）",
            "Value #H": "保留訊息", "Value #I": "副本不足"},
    order=["topic", "Value #A", "Value #B", "Value #C", "Value #D", "Value #E", "Value #F", "Value #G", "Value #H", "Value #I"],
    sort=("寫入", True),
    overrides=[
        col("寫入（筆/秒）", [{"id": "decimals", "value": 1}]),
        col("寫入", [{"id": "unit", "value": "Bps"}]), col("讀取", [{"id": "unit", "value": "Bps"}]),
        col("不均度", [{"id": "decimals", "value": 1}, {"id": "noValue", "value": "—"},
                       {"id": "thresholds", "value": steps(("green", None), ("yellow", 1.5), ("red", 2))}, cell("color-text")]),
        col("磁碟（單份）", [{"id": "unit", "value": "bytes"}, {"id": "noValue", "value": "未收集"}]),
        col("保留訊息", [{"id": "unit", "value": "short"}]),
        col("副本不足", [{"id": "thresholds", "value": ALERT_COUNT}, cell("color-text")]),
    ])))
y += 9

panels.append(row("② Consumer 有沒有追上、Topic 是否滿載" + RANGE_ROW, y)); y += 1
# 期間總結:整個所選時間範圍 ($__range) 裡發生了什麼
P_BASE = f'max by(consumergroup,topic)(max_over_time({LAG}[{PR}])) * 0'
P_MEM = f'kafka_consumergroup_members{{{C},consumergroup=~"$consumergroup"}}'
period_t = [
    tgt(f'max_over_time(({LAGS})[{PR}:{SCRAPE}])', ref="A", instant=True, fmt="table"),
    tgt(d_prod(PR), ref="B", instant=True, fmt="table"),
    tgt(d_cons(PR), ref="C", instant=True, fmt="table"),
    tgt(d_lag(PR), ref="D", instant=True, fmt="table"),
    tgt(f'{P_BASE} + on(topic) group_left() max_over_time(({PROD_RATE})[{PR}:{SMOOTH}])', ref="E", instant=True, fmt="table"),
    tgt(f'max_over_time(({CONS_RATE})[{PR}:{SMOOTH}])', ref="F", instant=True, fmt="table"),
    tgt(f'{P_BASE} + on(consumergroup) group_left() min by(consumergroup)(min_over_time({P_MEM}[{PR}]))',
        ref="G", instant=True, fmt="table"),
    tgt(f'{P_BASE} + on(consumergroup) group_left() max by(consumergroup)(changes({P_MEM}[{PR}]))',
        ref="H", instant=True, fmt="table"),
    tgt(f'max_over_time({RATIO_ACTIVE}[{PR}:{SMOOTH}])', ref="I", instant=True, fmt="table"),
]
panels.append(table(
    f"期間總結（整個所選時間範圍；速度 = 近 {WT} 平均的最高）",
    desc("整個所選時間範圍裡，每個 group × topic 發生了什麼。壓測回顧：時間範圍選壓測開始前到結束後，或在下方任一張圖上拖曳選取。"
         f"期間寫入 / 消費 / Lag 變化 = 範圍頭尾相減（頭尾各取 {SMOOTH} 平均），**期間寫入 − 期間消費 = Lag 變化**。"
         "時間範圍很長（例如 7 天）時會算比較久。",
         "最高 Lag 不大或有降回來、Lag 變化 ≤ 0、最少成員 ≥ 1、成員變動 0（或只有部署那幾次）、最高 Lag 佔保留量遠低於 100%。",
         "最高 Lag 很大、Lag 變化 > 0 → 期間消費跟不上，結束時還有積壓；看下方 Lag 圖是什麼時候開始累積",
         "最高消費/秒 明顯低於 最高寫入/秒 → consumer 的處理能力就是瓶頸（最高消費/秒 ≈ 目前配置能處理的上限）",
         "最少成員 0 → 期間有一段沒人讀；成員變動次數多 → 期間一直 rebalance（看下方成員數圖）",
         "最高 Lag 佔保留量接近 / 超過 100% → 期間差點 / 已經掉資料",
         cols=[("最高 Lag", "期間 lag 的最高值（每次 scrape 都看）"),
               ("期間寫入、期間消費", "範圍內寫進 / 讀完（commit）幾筆，只算頭尾都 commit 過的 partition"),
               ("Lag 變化", f"範圍結束時比開始時多幾筆 lag（= 期間寫入 − 期間消費）；±{FLAT} 筆內視為持平"),
               ("最高寫入/秒、最高消費/秒", "「比較區間」平均速度在期間內的最高值"),
               ("最少成員、成員變動", "期間最少有幾個 consumer、成員數變了幾次"),
               ("最高 Lag 佔保留量", "期間最糟的時候，只看有成員的時候；— = 期間一直沒有成員")]),
    period_t, gp(0, y, 24, 9),
    rename={"consumergroup": "Consumer Group", "topic": "Topic", "Value #A": "最高 Lag", "Value #B": "期間寫入",
            "Value #C": "期間消費", "Value #D": "Lag 變化", "Value #E": "最高寫入/秒", "Value #F": "最高消費/秒",
            "Value #G": "最少成員", "Value #H": "成員變動", "Value #I": "最高 Lag 佔保留量"},
    order=["consumergroup", "topic"] + [f"Value #{c}" for c in "ABCDEFGHI"],
    sort=("最高 Lag", True), filterable=False, min_width=60,
    overrides=[
        col("Consumer Group", [{"id": "custom.width", "value": 150}]),
        col("期間寫入", [{"id": "decimals", "value": 0}]),
        col("期間消費", [{"id": "decimals", "value": 0}]),
        col("Lag 變化", [{"id": "decimals", "value": 0},
                         {"id": "thresholds", "value": steps(("green", None), ("orange", FLAT))}, cell("color-text")]),
        col("最高寫入/秒", [{"id": "decimals", "value": 1}]),
        col("最高消費/秒", [{"id": "decimals", "value": 1}]),
        col("最少成員", [{"id": "decimals", "value": 0}, {"id": "custom.width", "value": 80},
                         {"id": "thresholds", "value": steps(("red", None), ("green", 1))}, cell("color-text")]),
        col("成員變動", [{"id": "decimals", "value": 0}, {"id": "custom.width", "value": 80},
                         {"id": "thresholds", "value": steps(("green", None), ("orange", 1))}, cell("color-text")]),
        col("最高 Lag 佔保留量", [{"id": "unit", "value": "percentunit"}, {"id": "min", "value": 0}, {"id": "max", "value": 1},
                                 {"id": "thresholds", "value": PCT}, cell("gauge"), {"id": "custom.width", "value": 160},
                                 {"id": "noValue", "value": "—"},
                                 {"id": "mappings", "value": [{"type": "range", "options": {
                                     "from": 1.000001, "to": None, "result": {"text": "已掉資料", "color": "red", "index": 0}}}]}]),
    ]))
y += 9


panels += [
    # 每個點取前一小段的最高值:時間範圍拉長、點變稀時,才不會剛好取到 commit 後的低點而漏掉高點
    ts("Lag（依 Group × Topic，每點取該段最高）",
       desc("每個 group 在每個 topic 還沒讀的訊息數。每個點是前一小段（至少 4 次 scrape，時間範圍拉長時跟著變長）裡的最高值。",
            "小且穩定，或升高後會降回來；隨 consumer commit 小幅鋸齒是正常的。",
            "一路往上 → 消費跟不上（看右邊速度）", "突然歸零又暴增 → offset 被重設或重新部署"),
       [tgt(f'max_over_time((sum by(consumergroup,topic)(clamp_min({LAG}, 0)))[{RI}:{SCRAPE}])', "{{consumergroup}} → {{topic}}")],
       gp(0, y, 12, 8)),
    ts(f"消費 vs 寫入速度（筆/秒，近 {WT} 平均）",
       desc("實線 = 每個 group 每秒讀完幾筆；虛線（寫入 →）= topic 每秒寫入幾筆。每個點都是那個時間點往前「比較區間」的平均，"
            "所以會比 Lag 圖慢半拍；跟上方表格用同一段區間。",
            "實線貼著同 topic 的虛線。",
            "實線長期低於虛線 → lag 會一直漲", "實線掉到 0 → 程式停了或卡住（看成員數、有 lag 的 Partition）",
            "實線斷掉 → 那段期間 offset 被往回重設，速度算不出來"),
       [tgt(CONS_RATE, "{{consumergroup}} → {{topic}}"),
        tgt(f'sum by(topic)(rate(kafka_topic_partition_current_offset{{{CT},topic!~"__.*"}}[{RW}]))', "寫入 → {{topic}}", ref="B")],
       gp(12, y, 12, 8),
       overrides=[{"matcher": {"id": "byRegexp", "options": "寫入 → .*"},
                   "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                                  {"id": "custom.lineWidth", "value": 2}]}]),
]
y += 8
panels += [
    ts(f"還落後多久的資料（估算：Lag ÷ 近 {WT} 平均寫入速度）",
       desc("Lag ÷ 平均寫入速度 ≈ 正在處理多久以前寫進來的資料。Kafka 指標沒有訊息時間，只能用平均速度換算：寫入忽快忽慢時只是概略值，"
            "要精準請在應用程式記錄訊息時間。",
            "幾秒內。",
            "持續上升 → 越落越多，跟業務要求（例如 1 分鐘內處理完）比較", "寫入很少或忽快忽慢時數字會偏大 → 改看 Lag",
            "線斷掉 → 那段時間沒有新寫入，算不出來"),
       [tgt(BEHIND,
            "{{consumergroup}} → {{topic}}")],
       gp(0, y, 6, 8), unit="s"),
    ts("Lag 佔保留量（最糟的 partition，有成員時）",
       desc("還沒讀的筆數 ÷ Kafka 這個 partition 還留著的筆數，取最糟的 partition。只畫有 consumer 的時候（沒人讀的 group 遲早都會到 100%）。",
            "遠低於 70%。",
            "往 100% 靠近 → 快讀不到就被刪了，加快消費", "超過 100% → 沒讀的已經被刪了（已掉資料）",
            "100% 不一定很急：Kafka 是整個 segment 一起刪，partition 只留幾筆時常常是 100%，搭配 Lag 看"),
       [tgt(RATIO_ACTIVE, "{{consumergroup}} → {{topic}}")],
       gp(6, y, 6, 8), unit="percentunit", th=PCT, th_style="line", fill=0),
    ts("Consumer 成員數",
       desc("每個 group 連著幾個 consumer。", "等於部署數 × 每個程式的 consumer 數。",
            "掉到 0 → 沒人在讀",
            "一直跳動 → 一直 rebalance（處理太久超過 max.poll.interval.ms，或 pod 一直重啟）",
            "比 partition 數多 → 多出來的會閒置"),
       [tgt(f'max by(consumergroup)(kafka_consumergroup_members{{{C},consumergroup=~"$consumergroup"}})', "{{consumergroup}}")],
       gp(12, y, 6, 8), decimals=0),
    ts("Rebalance 次數",
       desc("每分鐘完成幾次 rebalance，以及正在 rebalance 的 group 數（整個叢集）。", "接近 0，部署、擴縮時短暫增加。",
            "持續發生 → 有 consumer 一直加入 / 離開，期間整個 group 停止消費；看成員數找是哪個 group"),
       [tgt(f'sum(rate(kafka_server_group_coordinator_metrics_group_completed_rebalance_count{{{C}}}[5m])) * 60',
            "完成的 rebalance（次/分）"),
        tgt(f'sum(kafka_coordinator_group_groupmetadatamanager_numgroupspreparingrebalance{{{C}}}) + '
            f'sum(kafka_coordinator_group_groupmetadatamanager_numgroupscompletingrebalance{{{C}}})',
            "正在 rebalance 的 group", ref="B")],
       gp(18, y, 6, 8), decimals=1, no_value=nojmx("kafka.server:type=group-coordinator-metrics")),
]
y += 8
panels.append(ts(
    f"熱點 Partition：每個 partition 的寫入（筆/秒，近 {WT} 平均，前 10）",
    desc("寫入最多的 10 個 partition。", "同一 topic 的 partition 差不多。",
         "某個遠高於同 topic 其他 → key 集中（固定 key、同一個 tenant），這個 partition 的 consumer 會特別慢；"
         "加 consumer 沒用，要換 key 或打散"),
    [tgt(f'topk(10, sum by(topic,partition)(rate(kafka_topic_partition_current_offset{{{CT},topic!~"__.*"}}[{RW}])))',
         "{{topic}} [{{partition}}]")],
    gp(0, y, 6, 8)))
panels += [
    ts(f"Topic 寫入（筆/秒，近 {WT} 平均）",
       desc("每個 topic 每秒寫入幾筆。", "跟業務量同步。",
            "突然暴增 → producer 重送或迴圈", "掉到 0 → producer 停了或寫入失敗（看 ④）"),
       [tgt(f'sum by(topic)(rate(kafka_server_brokertopicmetrics_messagesin_total{{{BT}}}[{RW}]))', "{{topic}}")],
       gp(6, y, 6, 8)),
    ts(f"Topic 寫入 / 讀取流量（bytes/s，近 {WT} 平均）",
       desc("每個 topic 每秒寫入 / 讀取多少 bytes（讀取不含副本同步）。", "讀取 ≈ 寫入 × 讀它的 group 數。",
            "筆數不多但 bytes 很高 → 單筆訊息很大，留意 max.request.size / fetch.max.bytes"),
       [tgt(f'sum by(topic)(rate(kafka_server_brokertopicmetrics_bytesin_total{{{BT}}}[{RW}]))', "寫入 {{topic}}"),
        tgt(f'sum by(topic)(rate(kafka_server_brokertopicmetrics_bytesout_total{{{BT}}}[{RW}]))', "讀取 {{topic}}", ref="B")],
       gp(12, y, 6, 8), unit="Bps"),
    ts("Topic 磁碟大小（單份）",
       desc("每個 topic 在磁碟上的大小（不含副本）。", "到達 retention 後持平。",
            "一直長 → retention 太長或沒設 retention.bytes，會吃滿磁碟（看 ⑥ 磁碟幾小時後滿）"),
       [tgt(SIZE1, "{{topic}}")],
       gp(18, y, 6, 8), unit="bytes", no_value=nojmx("kafka.log:type=Log,name=Size（每個 partition 一條）")),
]
y += 8

# ③ 延遲
panels.append(row("③ 延遲：寫入 / 讀取慢在哪一段", y)); y += 1
def breakdown(req):
    parts = [("totaltimems", "總計"), ("requestqueuetimems", "排隊等處理"), ("localtimems", "broker 本地處理（寫磁碟）"),
             ("remotetimems", "等副本 / 等資料"), ("responsequeuetimems", "排隊等回應"), ("responsesendtimems", "送回 client"),
             ("throttletimems", "被限速")]
    return [tgt(f'max(kafka_network_requestmetrics_{m}{{{C},request="{req}",quantile="0.99"}})', name, ref=chr(65 + i))
            for i, (m, name) in enumerate(parts)]
TOTAL_BOLD = [{"matcher": {"id": "byName", "options": "總計"},
               "properties": [{"id": "custom.lineWidth", "value": 3}, {"id": "custom.fillOpacity", "value": 0}]}]
panels += [
    ts("寫入延遲拆解（Produce p99，最慢的 broker）",
       desc("broker 處理 Produce 的時間拆成幾段（每段各自的 p99，不能相加）。",
            "總計幾 ms～幾十 ms；acks=all 時「等副本」佔大部分。",
            "排隊等處理高 → broker 忙不過來（⑥ 閒置率）", "本地處理高 → 磁碟慢", "等副本高 → follower 跟不上（⑦）",
            "被限速 > 0 → 帳號超過 quota（⑤）", "這裡都正常但程式覺得慢 → 問題在 client 端（linger.ms、buffer 滿、網路）",
            "沒有「排隊等回應 / 送回 client / 被限速」線 → jmx exporter 沒收這三項"),
       breakdown("Produce"), gp(0, y, 12, 8), unit="ms", overrides=TOTAL_BOLD, fill=0),
    ts("讀取延遲拆解（FetchConsumer p99，最慢的 broker）",
       desc("consumer 的 Fetch 在 broker 花的時間（各段 p99）。",
            "約 500 ms：沒新資料時 broker 會等滿 fetch.max.wait.ms（預設 500 ms）才回。",
            "遠超過 fetch.max.wait.ms、或本地處理高 → 在讀很舊的資料（從頭重讀）或磁碟慢"),
       breakdown("FetchConsumer"), gp(12, y, 12, 8), unit="ms", overrides=TOTAL_BOLD, fill=0),
]
y += 8
panels += [
    ts("每秒請求數（依類型，前 10）",
       desc("每秒收到的各類請求數（全部 broker）。", "Produce / Fetch 為主。",
            "Metadata 很高 → client 一直重連，或 topic 不存在一直重查", "OffsetCommit 很高 → 每筆都 commit",
            "JoinGroup / SyncGroup 多 → 一直 rebalance"),
       [tgt(f'topk(10, sum by(request)(rate(kafka_network_requestmetrics_requests_total{{{C}}}[{RI}])))', "{{request}}")],
       gp(0, y, 12, 8), unit="reqps"),
    ts("寫入 / 讀取 p99（依 broker）",
       desc("每台 broker 的 Produce / FetchConsumer 總時間 p99。", "各台差不多。",
            "只有一台特別慢 → 那台有問題（磁碟、GC、負載不均），leader 在那台的 partition 都會慢"),
       [tgt(f'max by(instance)(kafka_network_requestmetrics_totaltimems{{{C},request="Produce",quantile="0.99"}})', "寫入 {{instance}}"),
        tgt(f'max by(instance)(kafka_network_requestmetrics_totaltimems{{{C},request="FetchConsumer",quantile="0.99"}})', "讀取 {{instance}}", ref="B")],
       gp(12, y, 12, 8), unit="ms", fill=0),
]
y += 8

# ④ 錯誤與權限
panels.append(row("④ 錯誤與權限：應用程式收到什麼錯誤", y)); y += 1
AUTH_ERR = 'error=~".*AUTHORIZATION_FAILED|SASL_AUTHENTICATION_FAILED|ILLEGAL_SASL_STATE|UNSUPPORTED_SASL_MECHANISM"'
panels += [
    ts("錯誤回應（依錯誤類型 × 請求，每分鐘）",
       desc("broker 回給 client 的錯誤（不含 NONE），每分鐘幾次。",
            "接近 0；broker 重啟、換 leader 時短暫出現 NOT_LEADER_OR_FOLLOWER，client 會自動重試。",
            "UNKNOWN_TOPIC_OR_PARTITION → topic 名稱打錯或沒建", "NOT_ENOUGH_REPLICAS → ISR 不足（看 ①）",
            "\\*_AUTHORIZATION_FAILED → 沒權限（看右邊）", "MESSAGE_TOO_LARGE / RECORD_LIST_TOO_LARGE → 訊息太大",
            "REBALANCE_IN_PROGRESS / UNKNOWN_MEMBER_ID / ILLEGAL_GENERATION → consumer 一直 rebalance",
            "OFFSET_OUT_OF_RANGE → 要讀的資料已被刪掉"),
       [tgt(f'sum by(error,request)(rate(kafka_network_requestmetrics_errors_total{{{C},error!="NONE"}}[{RI}])) * 60 > 0',
            "{{error}} ({{request}})")],
       gp(0, y, 12, 9), unit="short", decimals=1, draw="points"),
    ts("權限被拒（依錯誤 × 請求，每分鐘）",
       desc("被 ACL 擋下的請求，依錯誤 × 請求類型；紅虛線 = authorizer 拒絕的總次數。", "0。",
            "TOPIC_AUTHORIZATION_FAILED → 帳號對 topic 沒有 Describe / Write / Read",
            "GROUP_AUTHORIZATION_FAILED → 對 consumer group 沒有 Read",
            "TRANSACTIONAL_ID / CLUSTER_AUTHORIZATION_FAILED → 交易或 idempotent 權限",
            "有值時 → 確認程式用的帳號、topic、group 名稱，請維運補 ACL（Kafka 不記是哪個帳號被拒）",
            "jmx exporter 沒收 FindCoordinator 的 ErrorsPerSec 時 → GROUP_AUTHORIZATION_FAILED 不會出現"),
       [tgt(f'sum by(error,request)(rate(kafka_network_requestmetrics_errors_total{{{C},{AUTH_ERR}}}[{RI}])) * 60 > 0',
            "{{error}} ({{request}})"),
        tgt(authz("denied"), "authorizer 拒絕（次/分）", ref="B")],
       gp(12, y, 12, 9), unit="short", decimals=1,
       overrides=[{"matcher": {"id": "byName", "options": "authorizer 拒絕（次/分）"},
                   "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                                  {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]}]),
]
y += 9
panels += [
    ts("登入失敗（依 listener，每分鐘）",
       desc("SASL 登入失敗次數，依 listener。", "0。",
            "有值 → 帳號 / 密碼錯、帳號被刪或 mechanism 設錯；client 會看到 SaslAuthenticationException 並一直重連"
            "（常見：剛換密碼、設定檔用錯環境）"),
       [tgt(f'sum by(listener)({by_listener("failed_authentication_rate")}) * 60', "{{listener}}")],
       gp(0, y, 8, 8), decimals=1, no_value=nojmx("kafka.server:type=socket-server-metrics")),
    ts("寫入 / 讀取失敗（依 Topic，每分鐘）",
       desc("broker 處理 Produce / Fetch 失敗的次數，依 topic（不含權限被拒）。", "0。",
            "有值 → ISR 不足、訊息格式 / 大小不合，或 partition 搬移中；看左上「錯誤回應」是哪種錯"),
       [tgt(f'sum by(topic)(rate(kafka_server_brokertopicmetrics_failedproducerequests_total{{{BT}}}[{RI}])) * 60 > 0', "寫入失敗 {{topic}}"),
        tgt(f'sum by(topic)(rate(kafka_server_brokertopicmetrics_failedfetchrequests_total{{{BT}}}[{RI}])) * 60 > 0', "讀取失敗 {{topic}}", ref="B")],
       gp(8, y, 8, 8), decimals=1, no_value="0"),
    ts("ACL 授權檢查（每分鐘）",
       desc("authorizer 每分鐘允許 / 拒絕幾次（全部 broker）；虛線 = ACL 規則總數（右軸）。", "拒絕接近 0。",
            "ACL 總數突然變少 → 有人刪了 ACL，拒絕通常接著變多"),
       [tgt(authz("allowed"), "允許"),
        tgt(authz("denied"), "拒絕", ref="B"),
        tgt(ACLS, "ACL 總數", ref="C")],
       gp(16, y, 8, 8), no_value=nojmx("kafka.server:type=plugins"),
       overrides=[{"matcher": {"id": "byName", "options": "拒絕"},
                   "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]},
                  {"matcher": {"id": "byName", "options": "ACL 總數"},
                   "properties": [{"id": "custom.axisPlacement", "value": "right"},
                                  {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]}]),
]
y += 8

# ⑤ 各帳號
panels.append(row("⑤ 各帳號（專案）與連線的程式", y)); y += 1
panels += [
    ts("各帳號寫入流量",
       desc("每個帳號每秒寫入多少 bytes（依 jmx exporter 規則，帳號名的 - 可能顯示成 _）。", "跟各專案業務量相符。",
            "某帳號突然暴增 → 那個專案在重送或迴圈寫入（對照 ② Topic 寫入）"),
       [tgt(f'sum by(user)({by_user("produce", "byte_rate")})', "{{user}}")],
       gp(0, y, 8, 8), unit="Bps", no_value=NOQUOTA),
    ts("各帳號讀取流量",
       desc("每個帳號每秒讀走多少 bytes。", "≈ 它讀的 topic 寫入量 × group 數。",
            "遠大於平常 → 從頭重讀（offset 被重設）或多開了 group"),
       [tgt(f'sum by(user)({by_user("fetch", "byte_rate")})', "{{user}}")],
       gp(8, y, 8, 8), unit="Bps", no_value=NOQUOTA),
    ts("各帳號佔用 broker 處理時間",
       desc("每個帳號佔用 broker 處理執行緒的比例（100% = 一整條執行緒）。", "都不高。",
            "某帳號特別高 → 請求太頻繁（每筆都送、每筆都 commit、一直查 Metadata），會拖慢所有人"),
       [tgt(f'sum by(user)({by_user("request", "request_time")})', "{{user}}")],
       gp(16, y, 8, 8), unit="percent",
       no_value="沒有資料：要有設定 quota，jmx exporter 也要收 kafka.server:type=Request（見最上方說明）"),
]
y += 8
cs_t = [tgt(f'sum by(client,listener)({CLIENT_SW_L})', ref="A", instant=True, fmt="table")]
panels += [
    ts("各帳號被限速時間",
       desc("broker 因帳號超過 quota 而延後回應的時間（每個請求平均）。", "0。",
            "> 0 → 這個帳號超過流量上限，寫入 / 讀取會變慢；要調高請找維運"),
       [tgt(f'max by(user)({by_user("produce", "throttle_time")}) > 0', "寫入 {{user}}"),
        tgt(f'max by(user)({by_user("fetch", "throttle_time")}) > 0', "讀取 {{user}}", ref="B"),
        tgt(f'max by(user)({by_user("request", "throttle_time")}) > 0', "請求 {{user}}", ref="C")],
       gp(0, y, 8, 8), unit="ms", no_value="0（沒有被限速）"),
    ts("連線數（依 listener）",
       desc("每個 listener 的連線數（全部 broker）。", "穩定。",
            "一直上升 → 程式沒關 producer / consumer（每次都 new 一個）", "鋸齒狀 → 一直斷線重連"),
       [tgt(f'sum by(listener)({by_listener("connection_count")})', "{{listener}}")],
       gp(8, y, 8, 8), decimals=0, no_value=nojmx("kafka.server:type=socket-server-metrics")),
    live(table("連線的 client 程式（即時）",
          desc("目前連線的 client 函式庫與版本（client 自己回報），依 listener。", "版本一致，都是允許的版本。",
               "很舊的版本 → 可能不支援新功能或有已知 bug", "unknown → 太舊的 client，或連線還沒完成"),
          cs_t, gp(16, y, 8, 8), no_value=nojmx("kafka.server:type=socket-server-metrics"),
          rename={"client": "Client 程式 / 版本", "listener": "Listener", "Value": "連線數"},
          order=["client", "listener", "Value"], sort=("連線數", True),
          overrides=[col("Listener", [{"id": "custom.width", "value": 100}]),
                     col("連線數", [{"id": "custom.width", "value": 80}])])),
]
# 連線數 0 的 (舊連線) 不顯示
panels[-1]["transformations"].insert(1, {"id": "filterByValue", "options": {
    "filters": [{"fieldName": "Value", "config": {"id": "greater", "options": {"value": 0}}}],
    "type": "include", "match": "all"}})
y += 8

# ⑥ Broker 資源 (預設收合)
p6 = []
yy = y + 1
p6 += [
    ts("處理執行緒閒置率", desc("broker 處理請求與網路執行緒閒著的比例。", "> 30%。",
                         "< 20% → broker 忙不過來，所有請求排隊變慢（③ 排隊等處理升高）"),
       [tgt(f'rate(kafka_server_kafkarequesthandlerpool_requesthandleravgidlepercent_count_total{{{C}}}[5m]) / 1e9', "處理 {{instance}}"),
        tgt(f'kafka_network_socketserver_networkprocessoravgidlepercent{{{C}}}', "網路 {{instance}}", ref="B")],
       gp(0, yy, 6, 8), unit="percentunit", min_=0, max_=1,
       th=steps(("red", None), ("transparent", 0.2)), th_style="area", fill=0),
    ts("JVM Heap 使用率", desc("broker JVM heap 使用比例。", "鋸齒狀，GC 後降下來。",
                           "一直貼近 100% 且 GC 變多 → 記憶體不足，會有延遲尖峰"),
       [tgt(f'sum by(instance)(jvm_memory_used_bytes{{{C},area="heap"}}) / sum by(instance)(jvm_memory_max_bytes{{{C},area="heap"}})', "{{instance}}")],
       gp(6, yy, 6, 8), unit="percentunit", min_=0, max_=1, th=PCT, th_style="line", fill=0),
    ts("GC 暫停時間比例", desc("每秒花在 GC 的時間比例。", "< 1%。",
                          "> 5% 或出現 Old Generation → broker 停頓，client 看到延遲尖峰或 timeout"),
       [tgt(f'sum by(instance,gc)(rate(jvm_gc_collection_seconds_sum{{{C}}}[{RI}]))', "{{instance}} {{gc}}")],
       gp(12, yy, 6, 8), unit="percentunit", fill=0),
    ts("主機 CPU 使用率", desc("broker 主機 CPU 使用率。", "< 70%。",
                          "長時間 > 85% → 主機吃緊（壓縮、TLS 都吃 CPU）"),
       [tgt(f'1 - avg by(instance)(rate(node_cpu_seconds_total{{{C},mode="idle"}}[{RI}]))', "{{instance}}")],
       gp(18, yy, 6, 8), unit="percentunit", min_=0, max_=1, th=PCT, th_style="line", fill=0),
]
yy += 8
p6 += [
    ts("磁碟使用率", desc("每個檔案系統用了多少。", "< 70%。",
                     "> 85% → 快滿，滿了 broker 會停止服務（看右邊幾小時後滿）"),
       [tgt(f'1 - node_filesystem_avail_bytes{{{C},{FS}}} / node_filesystem_size_bytes{{{C},{FS}}}', "{{instance}} {{mountpoint}}")],
       gp(0, yy, 6, 8), unit="percentunit", min_=0, max_=1, th=PCT, th_style="line", fill=0),
    live(bar("磁碟幾小時後滿（即時）", desc("照最近 6 小時剩餘空間減少的速度，推估幾小時後滿；空間沒在減少的不列。",
                            "> 72 小時，或不列出。", "< 24 小時 → 請維運處理（擴容、調 retention、找 ② 長最快的 topic）"),
        f'bottomk(10, (node_filesystem_avail_bytes{{{C},{FS}}} / -deriv(node_filesystem_avail_bytes{{{C},{FS}}}[6h])) / 3600 > 0)',
        "{{instance}} {{mountpoint}}", gp(6, yy, 6, 8), "h", decimals=0,
        th=steps(("red", None), ("yellow", 24), ("green", 72)))),
    ts("磁碟忙碌率", desc("磁碟有多少時間在處理 I/O。", "< 60%。",
                     "接近 100% → 磁碟是瓶頸（③ 本地處理升高）"),
       [tgt(f'rate(node_disk_io_time_seconds_total{{{C},{DISK}}}[{RI}])', "{{instance}} {{device}}")],
       gp(12, yy, 6, 8), unit="percentunit", min_=0, max_=1, th=PCT, th_style="line", fill=0),
    ts("主機網路流量", desc("broker 主機網卡收 / 送流量。", "低於頻寬 70%。",
                       "貼近頻寬上限 → 網路是瓶頸，寫入與副本同步都會變慢"),
       [tgt(f'sum by(instance)(rate(node_network_receive_bytes_total{{{C},{NIC}}}[{RI}]))', "收 {{instance}}"),
        tgt(f'sum by(instance)(rate(node_network_transmit_bytes_total{{{C},{NIC}}}[{RI}]))', "送 {{instance}}", ref="B")],
       gp(18, yy, 6, 8), unit="Bps", fill=0),
]
panels.append(row("⑥ Broker 資源：是不是 Kafka 主機吃緊（點開）", y, collapsed=True, panels=p6)); y += 1

# ⑦ 副本與 Partition 健康 (預設收合)
p7 = []
yy = y + 1
urp_t = [tgt(f'max by(topic,partition)(kafka_topic_partition_replicas{{{CT}}}) - '
             f'max by(topic,partition)(kafka_topic_partition_in_sync_replica{{{CT}}}) > 0', ref="A", instant=True, fmt="table")]
p7 += [
    live(table("副本沒跟上的 Partition（即時）",
          desc("同步副本（ISR）比副本數少的 partition。", "沒有資料（全部正常）。",
               "同一台 broker 的 partition 都在這 → 那台掛了或太慢",
               "同步副本數 < min.insync.replicas → acks=all 寫入失敗（看 ①）"),
          urp_t, gp(0, yy, 8, 8),
          rename={"topic": "Topic", "partition": "Partition", "Value": "少幾個副本"},
          order=["topic", "partition", "Value"], sort=("少幾個副本", True), no_value="全部正常")),
    ts("ISR 縮減 / 擴增（每分鐘）", desc("副本被踢出 / 加回同步清單的次數。", "0。",
                                  "一直縮了又加 → 某台 broker 時快時慢（GC、磁碟、網路，看 ⑥）"),
       [tgt(f'sum(rate(kafka_server_replicamanager_isrshrinks_total{{{C}}}[{RI}])) * 60', "縮減"),
        tgt(f'sum(rate(kafka_server_replicamanager_isrexpands_total{{{C}}}[{RI}])) * 60', "擴增", ref="B")],
       gp(8, yy, 8, 8), decimals=1),
    ts("副本落後最多（筆）", desc("每台 broker 當 follower 時，最多落後 leader 幾筆。", "接近 0。",
                          "持續變大 → 這台跟不上，可能被踢出 ISR"),
       [tgt(f'max by(instance)(kafka_server_replicafetchermanager_maxlag{{{C}}})', "{{instance}}")],
       gp(16, yy, 8, 8)),
]
yy += 8
p7 += [
    live(bar("Leader 不在首選 broker（即時）",
        desc("leader 不在首選副本上的 partition 數。", "0。",
             "> 0 → broker 重啟後 leader 沒搬回，負載集中在部分 broker；通常會自動平衡，長時間不回來請維運處理"),
        f'topk(15, sum by(topic)(1 - kafka_topic_partition_leader_is_preferred{{{CT}}}) > 0)', "{{topic}}",
        gp(0, yy, 8, 8), "short", decimals=0)),
    ts("KRaft 狀態", desc("每台 controller 的 KRaft 角色（1 = 目前是這個角色）。", "一台 leader，其他 follower。",
                        "一直換 leader → controller 不穩，建 topic、換 leader 變慢"),
       [tgt(f'max by(instance,state)(kafka_server_raft_metrics_current_state{{{C}}})', "{{instance}} {{state}}")],
       gp(8, yy, 8, 8), decimals=0, legend_calcs=("lastNotNull",)),
    ts("Metadata 同步落後", desc("broker 套用叢集 metadata 落後多久。", "< 1 秒（會在 0～500 ms 跳動）。",
                            "持續升高 → broker 拿不到最新 metadata，client 可能找錯 leader（NOT_LEADER_OR_FOLLOWER）"),
       [tgt(f'max by(instance)(kafka_server_broker_metadata_metrics_last_applied_record_lag_ms{{{C}}})', "{{instance}}")],
       gp(16, yy, 8, 8), unit="ms"),
]
panels.append(row("⑦ 副本與 Partition 健康（維運用，點開）", y, collapsed=True, panels=p7)); y += 1


def var_query(name, label, query, regex="", multi=False, include_all=False, refresh=1, hide=0, all_value=None):
    v = {"name": name, "label": label, "type": "query", "datasource": DS,
         "query": {"query": query, "refId": "PrometheusVariableQueryEditor-VariableQuery"},
         "definition": query, "regex": regex, "refresh": refresh, "sort": 1, "hide": hide,
         "multi": multi, "includeAll": include_all, "current": {}, "options": []}
    if all_value:
        v["allValue"] = all_value
    return v

dash = {
    "title": "Kafka 問題排查（開發人員）",
    "uid": "kafka-dev-troubleshoot",
    "description": "給開發人員：應用程式遇到 Kafka 相關問題（lag、寫入失敗、權限、變慢）時，依症狀找方向。每張圖 ⓘ 有說明。",
    "tags": ["kafka", "troubleshooting"],
    "editable": True, "graphTooltip": 1, "fiscalYearStartMonth": 0, "liveNow": False,
    "refresh": "30s", "time": {"from": "now-1h", "to": "now"},
    "timepicker": {"refresh_intervals": ["10s", "30s", "1m", "5m", "15m"]},
    "timezone": "browser", "schemaVersion": 41, "version": 1, "links": [],
    "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                              "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                              "name": "Annotations & Alerts", "type": "dashboard"}]},
    "templating": {"list": [
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
         "description": "② 的速度、寫入 / 消費筆數、Lag 變化、追上需要都用這一段算：短 = 反應快，長 = 看得到較長的走勢、但慢半拍",
         "current": {"text": "5m", "value": "300"}, "hide": 0, "multi": False, "includeAll": False,
         "options": [{"text": t, "value": v, "selected": v == "300"}
                     for t, v in (("5m", "300"), ("15m", "900"), ("1h", "3600"))]},
    ]},
    "panels": panels,
}
if EXPAND:
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
    dash["uid"] += "-expand"
    dash["title"] += "（lab 展開）"
with open(ARGS[1], "w", encoding="utf-8", newline="\n") as f:
    json.dump(dash, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("panels", sum(1 for p in panels if p["type"] not in ("row", "text")) + len(p6) + len(p7), "max id", _id[0])
