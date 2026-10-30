# 產生「Redis 問題排查（開發人員）」Grafana dashboard JSON
import json, sys

DS = {"type": "prometheus", "uid": "${datasource}"}
C = 'job="$cluster"'
CI = 'job="$cluster",instance=~"$instance"'
RI = "$__rate_interval"
EXCL = 'cmd!~"info|ping|auth|hello|select|echo|replconf|psync|sync|command.*|(config|cluster|latency|slowlog|client|memory|module|acl|object|debug)[|].*"'
CIX = CI + "," + EXCL
# 錯誤這種很少發生的計數器,Redis 第一次發生時才會出現,一出現就是 N;rate / increase 會把第一個值當起點,漏掉這 N 次。
# 這段時間之前不存在的 series,整個值都算在這段時間裡。
def new_counts(sel, window):
    return f"clamp_min(({sel} - {sel} offset {window}) or {sel}, 0)"

# 只算 master (replica 的 key / 過期數跟 master 重複)
MASTER = 'and on(instance) redis_instance_info{job="$cluster",role="master"}'

_id = [0]
def nid():
    _id[0] += 1
    return _id[0]

def desc(what, normal, bad):
    return f"**看什麼**：{what}\n\n**正常**：{normal}\n\n**異常時**：{bad}"

def tgt(expr, legend="__auto", ref="A", instant=False, fmt=None):
    t = {"datasource": DS, "editorMode": "code", "expr": expr, "legendFormat": legend, "refId": ref,
         "range": not instant, "instant": instant}
    if fmt:
        t["format"] = fmt
    return t

def steps(*pairs):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in pairs]}

def ts(title, d, targets, gp, unit="short", th=None, th_style="off", min_=None, max_=None,
       overrides=None, stack=False, fill=10, legend_calcs=("mean", "max", "lastNotNull"), no_value=None):
    custom = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": fill, "showPoints": "never",
              "spanNulls": False, "axisSoftMin": 0, "thresholdsStyle": {"mode": th_style},
              "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    defaults = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"},
                "thresholds": th or steps(("green", None))}
    if min_ is not None: defaults["min"] = min_
    if max_ is not None: defaults["max"] = max_
    if no_value: defaults["noValue"] = no_value
    return {"id": nid(), "type": "timeseries", "title": title, "description": d, "datasource": DS,
            "gridPos": gp, "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
            "options": {"legend": {"displayMode": "table", "placement": "right", "showLegend": True,
                                   "calcs": list(legend_calcs), "sortBy": "Max", "sortDesc": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}

def stat(title, d, expr, gp, unit="none", th=None, mappings=None, no_value=None, decimals=None):
    defaults = {"unit": unit, "color": {"mode": "thresholds"}, "thresholds": th or steps(("blue", None)),
                "mappings": mappings or []}
    if no_value: defaults["noValue"] = no_value
    if decimals is not None: defaults["decimals"] = decimals
    return {"id": nid(), "type": "stat", "title": title, "description": d, "datasource": DS, "gridPos": gp,
            "targets": [tgt(expr, instant=True)],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "none", "textMode": "value",
                        "justifyMode": "center", "orientation": "auto", "wideLayout": True,
                        "showPercentChange": False}}

def bar(title, d, expr, gp, unit):
    return {"id": nid(), "type": "bargauge", "title": title, "description": d, "datasource": DS, "gridPos": gp,
            "targets": [tgt(expr, "{{cmd}}", instant=True)],
            "fieldConfig": {"defaults": {"unit": unit, "min": 0, "color": {"mode": "continuous-BlYlRd"},
                                         "thresholds": steps(("green", None))}, "overrides": []},
            "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                        "valueMode": "text", "namePlacement": "left", "sizing": "auto",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}

def timeline(title, d, expr, gp, mappings, show_value="auto"):
    return {"id": nid(), "type": "state-timeline", "title": title, "description": d, "datasource": DS,
            "gridPos": gp, "targets": [tgt(expr, "{{instance}}")],
            # Grafana 12 的 state-timeline 若設 color.mode=thresholds 會忽略 mappings,所以只給 mappings
            "fieldConfig": {"defaults": {"mappings": mappings, "custom": {"fillOpacity": 80, "lineWidth": 0}},
                            "overrides": []},
            "options": {"showValue": show_value, "rowHeight": 0.8, "mergeValues": True, "alignValue": "left",
                        "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "single", "sort": "none"}}}

def row(title, y, collapsed=False, panels=None):
    return {"id": nid(), "type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": panels or []}

def gp(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}

def vmap(d):
    return [{"type": "value", "options": {k: {"text": t, "color": c, "index": i}
                                          for i, (k, (t, c)) in enumerate(d.items())}}]

PCT = steps(("green", None), ("yellow", 0.7), ("red", 0.85))
ALERT_COUNT = steps(("green", None), ("red", 1))

GUIDE = """### 這個 Dashboard 怎麼用
上方先選 **環境 (Prometheus)** → **Cluster** → **節點**（預設全部）。每個圖表標題旁的 **ⓘ** 都有「看什麼／正常／異常時」說明。

| 症狀 | 先看哪裡 |
|---|---|
| 回應變慢、timeout | ② **延遲尖峰**（看類型）、**平均執行時間**、**p99 延遲** → ⑤ **Redis CPU** → ③ **慢查詢** → ⑥ 背景存檔 |
| 應用程式報錯 | ③ **錯誤回應（依類型）**、**失敗 / 被拒的指令** |
| 對方說「指令打不進去」、有在用 EVAL / Lua 腳本 | ③b **Lua 佔用主執行緒**、**單次腳本耗時**、**BUSY 錯誤**、**指令排隊的跡象** |
| 權限錯誤（NOPERM、WRONGPASS） | ③ **權限被拒（依類型）** |
| 看起來都沒滿、卻 command timeout | ⑤b **15 秒尖峰**、**最忙的那一顆核心**、**網卡使用率**、**TCP 重傳** |
| 連不上、連線被拒、連線越來越多 | ① 健康總覽 → ⑤ **連線數使用率**、**拒絕連線**、**各帳號連線數** |
| 想知道是哪個帳號（專案） | ⑤ **各帳號** 開頭的圖表：連線、重連、大回應 |
| 容器被 OOM kill、Redis 到不了 maxmemory | ④ **實際佔用（RSS）vs 資料用量 vs maxmemory**、**記憶體組成** |
| 資料不見、讀不到 | ④ **被淘汰的 key**、**命中率**、⑥ 角色變化（failover） |
| 只有某一台特別忙／特別滿 | ② **每節點 ops**、④ **每個 master 的 key 數**（熱點 key 或 hash tag 集中） |

**這裡看不到的**：各帳號的指令量、延遲、錯誤；慢查詢是哪個帳號、哪個 key；命中率差是哪個 key / 前綴；各前綴佔多少記憶體（Redis 沒有把這些統計提供給監控系統）。"""

panels = []
y = 0
panels.append({"id": nid(), "type": "text", "title": "", "gridPos": gp(0, y, 24, 11),
               "options": {"mode": "markdown", "content": GUIDE}})
y += 11

# ① 健康總覽
panels.append(row("① 健康總覽：有沒有東西壞掉", y)); y += 1
H = 4
panels += [
    stat("Cluster 狀態", desc("Redis Cluster 是否可正常服務（取所有節點裡最差的）。",
                             "OK。", "FAIL 代表有 slot 沒人負責，部分 key 讀寫會失敗；看右邊「Slots 異常」和下方節點清單。"),
         f"min(redis_cluster_state{{{C}}})", gp(0, y, 3, H),
         th=steps(("red", None), ("green", 1)),
         mappings=vmap({"1": ("OK", "green"), "0": ("FAIL", "red")}), no_value="無資料"),
    stat("離線節點", desc("Redis 沒回應，或 exporter 抓不到資料的節點數。", "0。",
                          "≥1 時看下方節點清單哪一台 Up 是「離線」。"),
         f"count((redis_up{{{C}}} == 0) or on(instance) (up{{{C}}} == 0)) or vector(0)", gp(3, y, 3, H),
         th=ALERT_COUNT),
    stat("Slots 異常", desc("處於 FAIL / PFAIL（疑似失效）的 slot 數（共 16384 個 slot）。", "0。",
                            "> 0 代表有 master 失聯，對應的 key 暫時無法存取，直到 failover 完成。"),
         f"max(redis_cluster_slots_fail{{{C}}} + redis_cluster_slots_pfail{{{C}}})", gp(6, y, 3, H),
         th=ALERT_COUNT),
    stat("複寫斷線", desc("跟 master 斷線的 replica 數。", "0。",
                          "≥1 時該 master 沒有備援，再掛就會掉資料；看 ⑥ 複寫延遲。"),
         f"count(redis_master_link_up{{{C}}} == 0) or vector(0)", gp(9, y, 3, H), th=ALERT_COUNT),
    stat("Master 數", desc("目前角色是 master 的節點數。", "跟建置時一樣（例如 3 主 3 從就是 3）。",
                           "數字變了 = 發生過 failover 或節點掉了；看 ⑥ 角色變化。"),
         f"count(redis_instance_info{{{C},role=\"master\"}})", gp(12, y, 3, H)),
    stat("最短運行時間", desc("所有節點裡運行最短的那台已經跑多久。", "很長（天以上）。",
                              "只有幾分鐘 = 最近有節點重啟；重啟會清掉沒持久化的資料、觸發完整同步。"),
         f"min(redis_uptime_in_seconds{{{C}}})", gp(15, y, 3, H), unit="s",
         th=steps(("red", None), ("yellow", 600), ("green", 3600))),
    stat("記憶體最滿的節點", desc("used_memory / maxmemory，取最高的那台。",
                                  "< 70%。", "> 85% 要注意：滿了會開始淘汰 key 或拒絕寫入（OOM）。「未設上限」表示 maxmemory=0，請看 ⑦ 主機記憶體。"),
         f"max(redis_memory_used_bytes{{{C}}} / (redis_memory_max_bytes{{{C}}} > 0))", gp(18, y, 3, H),
         unit="percentunit", th=PCT, no_value="未設上限", decimals=1),
    stat("連線數最滿的節點", desc("connected_clients / maxclients，取最高的那台。", "< 70%。",
                                  "滿了新連線會被拒絕；看 ⑤ 連線數是不是一直漲（連線洩漏）。"),
         f"max(redis_connected_clients{{{C}}} / redis_config_maxclients{{{C}}})", gp(21, y, 3, H),
         unit="percentunit", th=PCT, decimals=1),
]
y += H

# 節點清單 (table)
tbl_targets = [
    # exporter 抓不到時 redis_up 會消失,改用 up (=0) 補
    tgt(f"min by (instance) (redis_up{{{C}}}) or min by (instance) (up{{{C}}})", ref="A", instant=True, fmt="table"),
    tgt(f"max by (instance, role, redis_version) (redis_instance_info{{{C}}})", ref="B", instant=True, fmt="table"),
    tgt(f"max by (instance, master_host) (redis_master_link_up{{{C}}})", ref="C", instant=True, fmt="table"),
    tgt(f"max by (instance) (redis_uptime_in_seconds{{{C}}})", ref="D", instant=True, fmt="table"),
    tgt(f"sum by (instance) (rate(redis_commands_processed_total{{{C}}}[5m]))", ref="E", instant=True, fmt="table"),
    tgt(f"max by (instance) (redis_memory_used_bytes{{{C}}})", ref="F", instant=True, fmt="table"),
    tgt(f"max by (instance) (redis_connected_clients{{{C}}})", ref="G", instant=True, fmt="table"),
    tgt(f"sum by (instance) (redis_db_keys{{{C}}})", ref="H", instant=True, fmt="table"),
]
def col(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}
panels.append({
    "id": nid(), "type": "table", "title": "節點清單（現在）", "datasource": DS, "gridPos": gp(0, y, 24, 8),
    "description": desc("每個節點目前的角色、狀態與負載。Replica 的「跟隨 Master」是它複寫的來源；replica 的 ops 是從 master 複寫過來的寫入。",
                        "全部 Up；每台 master 都有一台 replica 且複寫「正常」；各 master 的 ops、key 數差不多。",
                        "某台 ops / key 數明顯偏高 = 熱點 key 或 hash tag 把資料集中到同一個 slot。"),
    "targets": tbl_targets,
    "transformations": [
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {
            "excludeByName": {"Time": True, "Value #B": True},
            "indexByName": {"instance": 0, "role": 1, "Value #A": 2, "master_host": 3, "Value #C": 4,
                            "redis_version": 5, "Value #D": 6, "Value #E": 7, "Value #F": 8, "Value #G": 9,
                            "Value #H": 10},
            "renameByName": {"instance": "節點", "role": "角色", "Value #A": "狀態", "master_host": "跟隨 Master",
                             "Value #C": "複寫", "redis_version": "版本", "Value #D": "運行時間",
                             "Value #E": "ops/s (5分鐘平均)", "Value #F": "記憶體", "Value #G": "連線數",
                             "Value #H": "Key 數"}}},
        {"id": "sortBy", "options": {"sort": [{"field": "節點"}]}},
    ],
    "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}},
                                 "thresholds": steps(("green", None))},
                    "overrides": [
                        col("角色", [{"id": "mappings", "value": vmap({"master": ("master", "blue"),
                                                                       "slave": ("replica", "text")})},
                                     {"id": "custom.cellOptions", "value": {"type": "color-text"}}]),
                        col("狀態", [{"id": "mappings", "value": vmap({"1": ("Up", "green"), "0": ("離線", "red")})},
                                     {"id": "custom.cellOptions", "value": {"type": "color-background"}}]),
                        col("複寫", [{"id": "mappings", "value": vmap({"1": ("正常", "green"), "0": ("斷線", "red")})},
                                     {"id": "custom.cellOptions", "value": {"type": "color-text"}}]),
                        col("運行時間", [{"id": "unit", "value": "s"}]),
                        col("角色", [{"id": "custom.width", "value": 90}]),
                        col("狀態", [{"id": "custom.width", "value": 80}]),
                        col("複寫", [{"id": "custom.width", "value": 70}]),
                        col("版本", [{"id": "custom.width", "value": 80}]),
                        col("ops/s (5分鐘平均)", [{"id": "unit", "value": "ops"}, {"id": "decimals", "value": 0}]),
                        col("記憶體", [{"id": "unit", "value": "bytes"}]),
                        col("Key 數", [{"id": "unit", "value": "short"}]),
                    ]},
    "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}})
y += 8

# ② 流量與延遲
panels.append(row("② 流量與延遲：變慢先看這裡", y)); y += 1
panels += [
    ts("每節點每秒指令數 (ops/s)",
       desc("每個節點每秒處理多少指令。", "跟業務流量同步升降；各 master 差不多。",
            "某台特別高 = 熱點 key / slot 不均；加壓但 ops 不再上升 = 到瓶頸了，看下面延遲和 ⑤ CPU。"),
       [tgt(f"sum by (instance) (rate(redis_commands_processed_total{{{CI}}}[{RI}]))", "{{instance}}")],
       gp(0, y, 12, 8), unit="ops"),
    ts("每節點平均執行時間",
       desc("Redis 內部執行一個指令平均花多久（**不含網路與排隊**；已排除 INFO、CONFIG、CLUSTER 等監控 / 管理指令）。", "通常在幾 µs ～ 幾十 µs。",
            "這裡正常但應用程式覺得慢 → 問題在網路、連線池或 client；這裡變高 → 看右邊是哪個指令、③ 慢查詢。"),
       [tgt(f"sum by (instance) (rate(redis_commands_duration_seconds_total{{{CIX}}}[{RI}])) / "
            f"sum by (instance) (rate(redis_commands_total{{{CIX}}}[{RI}]))", "{{instance}}")],
       gp(12, y, 12, 8), unit="s"),
]
y += 8
panels += [
    ts("各指令 p99 執行時間（Redis 7+）",
       desc("每種指令最慢的 1% 要多久（所選節點合併計算；已排除監控 / 管理指令）。Redis 6 以下沒有這項資料。",
            "GET / SET 這類簡單指令應在幾十 µs 內。",
            "KEYS、HGETALL、SMEMBERS、LRANGE 0 -1、大範圍 ZRANGE 偏高 = 一次處理太多資料（大 key），會卡住其他指令。"),
       [tgt(f"topk(10, histogram_quantile(0.99, sum by (le, cmd) "
            f"(rate(redis_commands_latencies_usec_bucket{{{CIX}}}[{RI}]))))", "{{cmd}}")],
       gp(0, y, 12, 8), unit="µs"),
    bar("這段時間呼叫最多的指令 Top 10",
        desc("所選時間範圍內，各指令被呼叫的總次數（已排除監控 / 管理指令）。", "跟應用程式的預期用法一致。",
             "出現預期外的指令（例如 KEYS、大量 EXISTS / TTL）通常是程式寫法問題。"),
        f"topk(10, sum by (cmd) (increase(redis_commands_total{{{CIX}}}[$__range])) > 0)", gp(12, y, 6, 8), "short"),
    bar("這段時間平均最慢的指令 Top 10",
        desc("所選時間範圍內，各指令平均執行時間（已排除監控 / 管理指令）。", "幾 µs ～ 幾十 µs。",
             "排前面且呼叫次數也多的指令最值得優化。"),
        f"topk(10, sum by (cmd) (increase(redis_commands_duration_seconds_total{{{CIX}}}[$__range])) / "
        f"(sum by (cmd) (increase(redis_commands_total{{{CIX}}}[$__range])) > 0))", gp(18, y, 6, 8), "s"),
]
y += 8
panels += [
    ts("快取命中率",
       desc("讀取時 key 存在的比例 = hits / (hits + misses)。", "當快取用時通常 > 80%（依業務而定）。",
            "突然下降 = key 過期 / 被淘汰 / 程式讀錯 key，請求會打到後端資料庫。"),
       [tgt(f"sum by (instance) (rate(redis_keyspace_hits_total{{{CI}}}[{RI}])) / "
            f"(sum by (instance) (rate(redis_keyspace_hits_total{{{CI}}}[{RI}])) + "
            f"sum by (instance) (rate(redis_keyspace_misses_total{{{CI}}}[{RI}])) > 0)", "{{instance}}")],
       gp(0, y, 8, 8), unit="percentunit", min_=0, max_=1, legend_calcs=("mean", "min", "lastNotNull")),
    ts("網路流量（正值收、負值送）",
       desc("每個節點每秒收送多少資料。", "跟 ops 同步升降。",
            "送出量遠大於收入且很高 = 有指令一次讀很大的值（大 key、HGETALL 大 hash）；接近網卡上限會變慢。"),
       [tgt(f"sum by (instance) (rate(redis_net_input_bytes_total{{{CI}}}[{RI}]))", "{{instance}} 收", "A"),
        tgt(f"-sum by (instance) (rate(redis_net_output_bytes_total{{{CI}}}[{RI}]))", "{{instance}} 送", "B")],
       gp(8, y, 8, 8), unit="Bps", fill=0),
    ts("最近最大的單次回應 / 請求",
       desc("最近幾秒內，單一 client 收到的最大回應（輸出緩衝）與送進來的最大請求（輸入緩衝）。",
            "幾 KB 以內。",
            "回應很大 = 有指令一次讀大 key（HGETALL、SMEMBERS、LRANGE 0 -1、MGET 大量 key）；請求很大 = 一次寫入大值或大批量。"
            "要知道是哪個 key，可用 `redis-cli --bigkeys` 或 `MEMORY USAGE <key>` 查。"),
       [tgt(f"max by (instance) (redis_client_recent_max_output_buffer_bytes{{{CI}}})", "{{instance}} 回應", "A"),
        tgt(f"max by (instance) (redis_client_recent_max_input_buffer_bytes{{{CI}}})", "{{instance}} 請求", "B")],
       gp(16, y, 8, 8), unit="bytes", legend_calcs=("max", "lastNotNull")),
]
y += 8
NOLAT = "沒有資料：Redis 要設定 latency-monitor-threshold（例如 100 ms）才會記錄"
LAT_DESC = desc("Redis 自己記錄的延遲尖峰：內部任何動作超過門檻（latency-monitor-threshold）就記一筆，依類型分。每個點表示那個時間附近發生過尖峰（同一次會連續顯示幾個點），確切時間看右邊表格。",
                "沒有點，或偶爾零星幾個。",
                "**command** 慢指令（程式要改：大 key、O(N) 指令）　**fast-command** 連 GET / SET 都慢（主機問題：CPU 被搶、swap）　"
                "**fork** 背景存檔複製行程（資料量大時更久）　**aof-*** 寫 AOF 檔慢（磁碟）　"
                "**expire-cycle** 一次大量 key 過期　**eviction-*** 記憶體滿在淘汰 key")
panels += [
    ts("延遲尖峰（Redis 記錄，依類型）", LAT_DESC,
       [tgt(f"redis_latency_spike_duration_seconds{{{CI}}} and on(instance, event_name) "
            f"(changes(redis_latency_spike_last{{{CI}}}[{RI}]) > 0)", "{{event_name}} @ {{instance}}")],
       gp(0, y, 14, 8), unit="s", no_value=NOLAT, legend_calcs=("max", "lastNotNull")),
]
# 尖峰是零星事件,畫成點
panels[-1]["fieldConfig"]["defaults"]["custom"].update({"drawStyle": "points", "pointSize": 9, "fillOpacity": 0})
panels += [
    {"id": nid(), "type": "table", "title": "最近一次延遲尖峰（每台、每種）", "datasource": DS, "gridPos": gp(14, y, 10, 8),
     "description": LAT_DESC,
     "targets": [tgt(f"max by (instance, event_name) (redis_latency_spike_duration_seconds{{{CI}}})", ref="A", instant=True, fmt="table"),
                 tgt(f"max by (instance, event_name) (redis_latency_spike_last{{{CI}}}) * 1000", ref="B", instant=True, fmt="table")],
     "transformations": [
         {"id": "merge", "options": {}},
         {"id": "organize", "options": {
             "excludeByName": {"Time": True},
             "indexByName": {"Value #B": 0, "instance": 1, "event_name": 2, "Value #A": 3},
             "renameByName": {"Value #B": "發生時間", "instance": "節點", "event_name": "類型", "Value #A": "耗時"}}},
         {"id": "sortBy", "options": {"sort": [{"field": "發生時間", "desc": True}]}}],
     "fieldConfig": {"defaults": {"noValue": NOLAT}, "overrides": [
         col("發生時間", [{"id": "unit", "value": "time:MM-DD HH:mm:ss"}, {"id": "custom.width", "value": 130}]),
         col("耗時", [{"id": "unit", "value": "s"}])]},
     "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}},
]
y += 8

# ③ 錯誤與慢查詢
panels.append(row("③ 錯誤與慢查詢：程式有沒有出錯", y)); y += 1
panels += [
    ts("錯誤回應（依類型）",
       desc("Redis 回給 client 的錯誤，每分鐘幾次（Redis 6.2+）。某種錯誤第一次出現也算得到。",
            "接近 0。",
            "**OOM** 記憶體滿　**MOVED / ASK** client 沒用 cluster 模式或 slot 搬移中　"
            "**NOPERM / WRONGPASS** 帳號權限或密碼　**WRONGTYPE** 程式對錯的資料型別下指令　"
            "**BUSY** Lua / Function 跑太久　**LOADING** 節點剛重啟還在載入資料"),
       [tgt(f"sum by (err) ({new_counts(f'redis_errors_total{{{CI}}}', '1m')})", "{{err}}")],
       gp(0, y, 6, 8), unit="short", legend_calcs=("max", "lastNotNull")),
    ts("失敗 / 被拒的指令（Redis 7+）",
       desc("依指令列出執行失敗（failed）與執行前就被拒（rejected，例如權限、OOM、參數錯）的次數／秒。",
            "接近 0。", "對照左邊錯誤類型，就知道是哪個指令出錯。"),
       [tgt(f"sum by (cmd) (rate(redis_commands_failed_calls_total{{{CI}}}[{RI}])) > 0", "{{cmd}} 失敗", "A"),
        tgt(f"sum by (cmd) (rate(redis_commands_rejected_calls_total{{{CI}}}[{RI}])) > 0", "{{cmd}} 被拒", "B")],
       gp(6, y, 6, 8), unit="short", legend_calcs=("max", "lastNotNull")),
    ts("新增慢查詢",
       desc("每個節點新增幾筆慢查詢（執行超過 slowlog-log-slower-than，預設 10ms）。", "0 或偶爾 1、2 筆。",
            "持續增加 → 用 `SLOWLOG GET` 看是哪個指令、哪個 key、從哪個 ip:port 來的。"),
       [tgt(f"sum by (instance) (clamp_min(delta(redis_slowlog_last_id{{{CI}}}[{RI}]), 0))", "{{instance}}")],
       gp(12, y, 6, 8), unit="short", legend_calcs=("max", "lastNotNull")),
    ts("權限被拒（依類型）",
       desc("被 ACL 擋下的次數／秒。Redis 不會在這裡記是哪個帳號。", "0。",
            "**密碼錯誤** 設定檔帳密錯或密碼剛換　**指令無權限** 程式用了帳號沒開的指令　"
            "**key 不在權限內** 讀寫了別的專案的 key 前綴。要知道是哪個帳號、哪個 key：`ACL LOG`。"),
       [tgt(f"sum(rate(redis_acl_access_denied_auth_total{{{CI}}}[{RI}]))", "密碼錯誤", "A"),
        tgt(f"sum(rate(redis_acl_access_denied_cmd_total{{{CI}}}[{RI}]))", "指令無權限", "B"),
        tgt(f"sum(rate(redis_acl_access_denied_key_total{{{CI}}}[{RI}]))", "key 不在權限內", "C"),
        tgt(f"sum(rate(redis_acl_access_denied_channel_total{{{CI}}}[{RI}]))", "channel 無權限", "D")],
       gp(18, y, 6, 8), unit="short", legend_calcs=("max", "lastNotNull")),
]
y += 8

# ③b Lua 腳本:是不是腳本佔住 Redis
SCRIPT = 'cmd=~"eval|evalsha|eval_ro|evalsha_ro|fcall|fcall_ro"'
CIS = CI + "," + SCRIPT
# 每秒有幾秒在跑腳本 = 主執行緒被腳本佔住的比例 (Redis 一次只跑一個指令;腳本裡 redis.call 的時間也算在腳本裡)
LUA_SHARE = f"sum by (instance) (rate(redis_commands_duration_seconds_total{{{CIS}}}[{RI}]))"
LUA_SHARE_1M = f"sum by (instance) (rate(redis_commands_duration_seconds_total{{{CIS}}}[1m]))"
LUA_AVG_1M = (f"sum by (instance) (rate(redis_commands_duration_seconds_total{{{CIS}}}[1m])) / "
              f"(sum by (instance) (rate(redis_commands_total{{{CIS}}}[1m])) > 0)")
NOLUA = "這段時間沒有 EVAL / EVALSHA / FCALL"
LUA_GUIDE = """**怎麼證明是 Lua 腳本卡住**：Redis 一次只跑一個指令，腳本跑完前，其他人的指令都在排隊。Redis 沒有「佇列長度」指標，用下面的證據：

- **其他指令要排多久**（最直接）：exporter 下的簡單指令平常幾 ms 回來，變成秒級 = 每個指令都在排隊。
- **Lua 佔用主執行緒**接近 100% → 排隊是腳本造成的。
- **單次腳本耗時** ms～秒級、一般指令 µs 級 → 每跑一次腳本，後面的指令就多等那麼久。
- **BUSY 錯誤** > 0 → 腳本跑超過 5 秒，其他指令直接被拒。
- 是哪支腳本、誰呼叫的：`SLOWLOG GET`。"""
panels.append(row("③b Lua 腳本（EVAL / FCALL）：是不是腳本佔住 Redis、卡住其他指令", y)); y += 1
panels.append({"id": nid(), "type": "text", "title": "", "gridPos": gp(0, y, 9, 8),
               "options": {"mode": "markdown", "content": LUA_GUIDE}})
# exporter 抓一輪要下幾個簡單指令 (INFO 等),它們跟其他 client 一樣排隊;平常幾 ms,主執行緒被佔滿時會變成秒級
QUEUE_WAIT = f"max by (instance) (redis_exporter_last_scrape_duration_seconds{{{CI}}})"
QUEUE_TH = steps(("green", None), ("yellow", 0.1), ("red", 1))
SH = 4
panels += [
    stat("其他指令要排多久（這段時間最高）",
         desc("exporter 每 15 秒連進 Redis 下幾個簡單指令（INFO 等），這些指令跟應用程式的指令一樣要排隊。"
              "這裡是時間範圍內最久的一次（最慢的節點）。",
              "幾 ms～幾十 ms。",
              "到秒級 = Redis 主執行緒被佔住，每個指令都在排隊；這就是應用程式 command timeout 的原因。"),
         f"max(max_over_time(redis_exporter_last_scrape_duration_seconds{{{CI}}}[$__range]))", gp(9, y, 5, 2 * SH),
         unit="s", th=QUEUE_TH),
    stat("Lua 佔用主執行緒（這段時間最高）",
         desc("時間範圍內，腳本佔掉主執行緒時間的最高比例（每分鐘看一次，取最忙的節點）。",
              "< 30%。", "> 80% = 那一分鐘 Redis 幾乎都在跑腳本，其他指令都在排隊。"),
         f"max(max_over_time(({LUA_SHARE_1M})[$__range:1m]))", gp(14, y, 5, SH), unit="percentunit",
         th=steps(("green", None), ("yellow", 0.3), ("red", 0.8)), no_value=NOLUA, decimals=0),
    stat("單次腳本平均耗時（這段時間最高）",
         desc("時間範圍內，每次 EVAL / EVALSHA / FCALL 平均跑多久的最高值（每分鐘看一次）。",
              "1 ms 以內。", "幾十 ms 以上 = 每次腳本都讓其他指令等那麼久；> 5 秒會讓其他指令收到 BUSY。"),
         f"max(max_over_time(({LUA_AVG_1M})[$__range:1m]))", gp(19, y, 5, SH), unit="s",
         th=steps(("green", None), ("yellow", 0.01), ("red", 0.1)), no_value=NOLUA),
]
panels += [
    stat("腳本呼叫次數（這段時間）",
         desc("時間範圍內 EVAL / EVALSHA / FCALL 總共被呼叫幾次。", "依業務。",
              "次數不多但左邊佔用很高 = 單一腳本很重（一次處理很多 key / 大迴圈）。"),
         f"sum(increase(redis_commands_total{{{CIS}}}[$__range]))", gp(14, y + SH, 5, SH), unit="short",
         no_value=NOLUA, decimals=0),
    stat("BUSY 錯誤（這段時間）",
         desc("其他指令因為腳本跑太久被 Redis 直接拒絕（BUSY Redis is busy running a script）的次數。",
              "0。", "> 0 = 有腳本跑超過 busy-reply-threshold（預設 5 秒），這段時間其他指令全部失敗。"),
         f"sum({new_counts(f'redis_errors_total{{{CI},err=\"BUSY\"}}', '$__range')}) or vector(0)", gp(19, y + SH, 5, SH),
         unit="short", th=ALERT_COUNT, decimals=0),
]
y += 8
panels += [
    ts("其他指令要排多久（exporter 實測）",
       desc("exporter 每 15 秒連進 Redis 查一輪（INFO 等幾個簡單指令）花多久。這些指令本身幾乎不花時間，"
            "多出來的都是排在別人後面等。Redis 沒有「佇列長度」指標，這條線就是排隊的實測值。黃線 0.1 秒、紅線 1 秒。",
            "幾 ms～幾十 ms，而且平穩。",
            "跟左上 Lua 佔用同時升高到秒級 = 腳本把 Redis 佔住，其他人的指令都在排隊（對方說「指令打不進去」）；"
            "Lua 不高但這條也升高 = 是別的大指令或主機問題，看 ⑤b。"),
       [tgt(QUEUE_WAIT, "{{instance}}", "A")],
       gp(0, y, 12, 8), unit="s", th=QUEUE_TH, th_style="line", legend_calcs=("max", "lastNotNull")),
    ts("指令排隊的跡象（client 端）",
       desc("Redis 忙著跑腳本時，其他 client 送來的指令會堆在輸入緩衝等著；等太久的 client 會逾時、斷線再重連。",
            "輸入緩衝幾 KB 以內；新建連線很低。",
            "跟左上 Lua 佔用同時升高 = 指令在排隊等腳本跑完（對方說「指令打不進去」就是這個）。"),
       [tgt(f"max by (instance) (redis_client_recent_max_input_buffer_bytes{{{CI}}})", "{{instance}} 輸入緩衝最大", "A"),
        tgt(f"sum by (instance) (rate(redis_connections_received_total{{{CI}}}[{RI}]))", "{{instance}} 新建連線 / 秒", "B")],
       gp(12, y, 12, 8), unit="short",
       overrides=[{"matcher": {"id": "byRegexp", "options": ".*輸入緩衝最大"},
                   "properties": [{"id": "unit", "value": "bytes"}, {"id": "custom.axisPlacement", "value": "right"}]}],
       legend_calcs=("max", "lastNotNull")),
]
y += 8
panels += [
    ts("主執行緒被 Lua 佔掉多少（每秒有幾成在跑腳本）",
       desc("每秒有多少時間在執行腳本（含腳本裡 redis.call 的時間）。虛線是主執行緒 CPU。紅線 80%。",
            "低於 30%。",
            "Lua 的線貼近 CPU 線、接近 100% = Redis 的忙碌幾乎都是腳本造成，這段時間其他指令只能排隊。"),
       [tgt(LUA_SHARE, "{{instance}} Lua", "A"),
        tgt(f"sum by (instance) (rate(redis_cpu_sys_main_thread_seconds_total{{{CI}}}[{RI}]) + "
            f"rate(redis_cpu_user_main_thread_seconds_total{{{CI}}}[{RI}])) or "
            f"sum by (instance) (rate(redis_cpu_sys_seconds_total{{{CI}}}[{RI}]) + "
            f"rate(redis_cpu_user_seconds_total{{{CI}}}[{RI}]))", "{{instance}} 主執行緒 CPU", "B")],
       gp(0, y, 12, 8), unit="percentunit", th=steps(("green", None), ("red", 0.8)), th_style="line", min_=0, max_=1,
       overrides=[{"matcher": {"id": "byRegexp", "options": ".*主執行緒 CPU"},
                   "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 6]}},
                                  {"id": "custom.fillOpacity", "value": 0}]}],
       no_value=NOLUA),
    ts("每次腳本耗時 vs 一般指令",
       desc("腳本（依指令）每次平均跑多久，和一般指令（GET / SET / HGET… 其他所有指令）的平均。Redis 7+ 另有腳本的 p99。"
            "一般指令只算在 Redis 裡執行的時間，**不含排隊**。",
            "都在 1 ms 以內。",
            "腳本是 ms～秒級、一般指令還是 µs 級，但應用程式 timeout = 一般指令卡在腳本後面排隊。"),
       [tgt(f"sum by (cmd) (rate(redis_commands_duration_seconds_total{{{CIS}}}[{RI}])) / "
            f"(sum by (cmd) (rate(redis_commands_total{{{CIS}}}[{RI}])) > 0)", "{{cmd}} 平均", "A"),
        tgt(f"histogram_quantile(0.99, sum by (le, cmd) (rate(redis_commands_latencies_usec_bucket{{{CIS}}}[{RI}]))) / 1e6",
            "{{cmd}} p99", "B"),
        tgt(f"sum(rate(redis_commands_duration_seconds_total{{{CIX},cmd!~\"eval|evalsha|eval_ro|evalsha_ro|fcall|fcall_ro\"}}[{RI}])) / "
            f"(sum(rate(redis_commands_total{{{CIX},cmd!~\"eval|evalsha|eval_ro|evalsha_ro|fcall|fcall_ro\"}}[{RI}])) > 0)",
            "一般指令 平均", "C")],
       gp(12, y, 12, 8), unit="s", no_value=NOLUA, legend_calcs=("mean", "max", "lastNotNull")),
]
y += 8
panels += [
    ts("腳本呼叫次數 vs 全部指令（每秒）",
       desc("每秒有幾次腳本呼叫，和 Redis 每秒處理的全部指令數（含腳本裡的 redis.call）。",
            "腳本佔全部的一小部分。",
            "腳本次數不多、但左上 Lua 佔用很高 = 每個腳本都很重；全部指令數掉下去 = 其他人的指令被擠掉了。"),
       [tgt(f"sum by (instance) (rate(redis_commands_total{{{CIS}}}[{RI}]))", "{{instance}} 腳本", "A"),
        tgt(f"sum by (instance) (rate(redis_commands_processed_total{{{CI}}}[{RI}]))", "{{instance}} 全部指令", "B")],
       gp(0, y, 12, 8), unit="ops", no_value=NOLUA),
    ts("BUSY 與腳本錯誤（每分鐘次數）",
       desc("**BUSY** 其他指令因腳本跑太久被拒　**NOSCRIPT** EVALSHA 找不到腳本（節點重啟 / failover 後要重新 SCRIPT LOAD）　"
            "**UNKILLABLE** 腳本已經寫入資料、不能用 SCRIPT KILL 停（只能 SHUTDOWN NOSAVE）。另列腳本本身執行失敗的次數。",
            "全部 0。",
            "BUSY > 0 是腳本卡住 Redis 最直接的證據：那段時間所有其他指令都被拒絕。"),
       [tgt(f"sum by (err) ({new_counts(f'redis_errors_total{{{CI},err=~\"BUSY|NOSCRIPT|UNKILLABLE\"}}', '1m')})", "{{err}}", "A"),
        tgt(f"sum by (cmd) ({new_counts(f'redis_commands_failed_calls_total{{{CIS}}}', '1m')}) > 0", "{{cmd}} 執行失敗", "B")],
       gp(12, y, 12, 8), unit="short", legend_calcs=("max", "lastNotNull"), no_value="0"),
]
y += 8

# ④ 記憶體與 Key
panels.append(row("④ 記憶體與 Key：會不會滿、資料會不會不見", y)); y += 1
panels += [
    ts("記憶體使用率",
       desc("used_memory / maxmemory。黃線 70%、紅線 85%。", "< 70% 且平穩。",
            "一直往上爬不回落 = key 沒設 TTL 或有東西沒清。maxmemory=0（沒設上限）時這張沒資料，請看 ⑦ 主機記憶體。"),
       [tgt(f"redis_memory_used_bytes{{{CI}}} / (redis_memory_max_bytes{{{CI}}} > 0)", "{{instance}}")],
       gp(0, y, 8, 8), unit="percentunit", th=PCT, th_style="line", min_=0, max_=1,
       no_value="沒有設定 maxmemory（無上限）→ 請看 ⑦ 主機記憶體"),
    ts("記憶體使用量",
       desc("每個節點 Redis 用了多少記憶體。", "跟資料量成正比、平穩。",
            "突然暴增 = 寫入大量資料或有大 key；replica 比 master 大很多可能是複寫緩衝積壓。"),
       [tgt(f"redis_memory_used_bytes{{{CI}}}", "{{instance}}")], gp(8, y, 8, 8), unit="bytes"),
    ts("記憶體碎片率",
       desc("作業系統實際給 Redis 的記憶體 ÷ Redis 自己算的用量。黃線 1.5。", "1.0 ～ 1.5。",
            "> 1.5 浪費較多記憶體（大量刪改後常見）；資料量很小時數字會偏高，可忽略。"),
       [tgt(f"redis_mem_fragmentation_ratio{{{CI}}}", "{{instance}}")], gp(16, y, 8, 8), unit="none",
       th=steps(("green", None), ("yellow", 1.5)), th_style="line", legend_calcs=("mean", "max", "lastNotNull")),
]
y += 8
# 實際佔用 (RSS) vs maxmemory:maxmemory 只比對 used_memory,容器 / 主機看的是 RSS (含碎片、程式開銷)
RSS = f"redis_memory_used_rss_bytes{{{CI}}}"
USED = f"redis_memory_used_bytes{{{CI}}}"
MAXM = f"(redis_memory_max_bytes{{{CI}}} > 0)"
FULL = f"{MAXM} * {RSS} / {USED}"   # 用滿 maxmemory 時,照現在的碎片率預估實際佔用
DASH = [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 6]}}, {"id": "custom.fillOpacity", "value": 0}]
panels += [
    ts("實際佔用（RSS）vs 資料用量 vs maxmemory",
       desc("**實際佔用** = 作業系統真正給 Redis 的記憶體，Docker / K8s 的記憶體上限、OOM kill 看的是這個。"
            "**資料用量**（used_memory）= Redis 自己算的，maxmemory 只跟這個比，不含碎片和程式開銷。"
            "**用滿 maxmemory 時預估實際佔用** = maxmemory × 現在的碎片率。",
            "「用滿時預估」比容器記憶體上限低一截（至少留 20～30%，有開存檔要留更多）。",
            "「用滿時預估」超過容器上限 = 資料還沒到 maxmemory 容器就先被 OOM kill，maxmemory 要調低。"
            "例如上限 32G、maxmemory 30gb、碎片率 1.3 → 用滿時約 39G，資料大概 24G 左右就撞到 32G。"),
       [tgt(RSS, "{{instance}} 實際佔用", "A"),
        tgt(USED, "{{instance}} 資料用量", "B"),
        tgt(f"redis_memory_max_bytes{{{CI}}} > 0", "{{instance}} maxmemory", "C"),
        tgt(FULL, "{{instance}} 用滿 maxmemory 時預估實際佔用", "D")],
       gp(0, y, 12, 9), unit="bytes", fill=0,
       overrides=[{"matcher": {"id": "byRegexp", "options": ".*maxmemory"}, "properties": DASH},
                  {"matcher": {"id": "byRegexp", "options": ".*預估實際佔用"},
                   "properties": DASH + [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]}],
       legend_calcs=("max", "lastNotNull")),
    ts("Lua 腳本快取（腳本數 / 佔用記憶體）",
       desc("Redis 快取了幾支 EVAL 腳本、佔多少記憶體。每個**不同內容**的腳本都會被快取，直到 SCRIPT FLUSH。",
            "腳本數固定（程式用固定腳本 + EVALSHA）。",
            "腳本數一直往上加 = 程式每次把參數寫進腳本內容（動態組 EVAL），記憶體會一直長，應改成固定腳本、參數用 KEYS / ARGV 傳。"),
       [tgt(f"redis_number_of_cached_scripts{{{CI}}}", "{{instance}} 腳本數", "A"),
        tgt(f"(redis_memory_used_vm_eval_bytes{{{CI}}} or redis_memory_used_lua_bytes{{{CI}}}) + "
            f"redis_memory_used_scripts_bytes{{{CI}}}", "{{instance}} 佔用記憶體", "B")],
       gp(12, y, 12, 9), unit="short",
       overrides=[{"matcher": {"id": "byRegexp", "options": ".*佔用記憶體"},
                   "properties": [{"id": "unit", "value": "bytes"}, {"id": "custom.axisPlacement", "value": "right"}]}],
       legend_calcs=("max", "lastNotNull")),
]
y += 9
mem_cols = [
    ("A", RSS, "實際佔用"), ("B", USED, "資料用量"), ("C", f"redis_memory_max_bytes{{{CI}}}", "maxmemory"),
    ("D", f"redis_mem_fragmentation_ratio{{{CI}}}", "碎片率"), ("E", FULL, "用滿時預估實際佔用"),
    ("F", f"max by (instance) (redis_rdb_last_cow_size_bytes{{{CI}}} or redis_aof_last_cow_size_bytes{{{CI}}})", "上次存檔多吃"),
    ("G", f"redis_memory_used_dataset_bytes{{{CI}}}", "其中：資料"),
    ("H", f"redis_mem_clients_normal{{{CI}}}", "其中：client 緩衝"),
    ("I", f"(redis_mem_total_replication_buffers_bytes{{{CI}}} or redis_mem_replication_backlog_bytes{{{CI}}})", "其中：複寫緩衝"),
    ("J", f"(redis_memory_used_vm_eval_bytes{{{CI}}} or redis_memory_used_lua_bytes{{{CI}}}) + redis_memory_used_scripts_bytes{{{CI}}}",
     "其中：Lua"),
]
panels.append({
    "id": nid(), "type": "table", "title": "記憶體組成（現在，每個節點）", "datasource": DS, "gridPos": gp(0, y, 24, 7),
    "description": desc("每個節點的記憶體拆開來看。**上次存檔多吃** = 最近一次背景存檔（RDB / AOF 重寫）期間，因為 fork 後資料被改動而多複製的記憶體（copy-on-write），"
                        "存檔時實際佔用要再加上這一塊。",
                        "「用滿時預估實際佔用 + 上次存檔多吃」比容器記憶體上限低。",
                        "超過容器上限 = 存檔或資料長到某個量時會被 OOM kill；client 緩衝很大 = 有 client 讀大 key 或讀太慢；"
                        "Lua 腳本很大 = 動態組 EVAL 腳本。"),
    "targets": [tgt(f"max by (instance) ({e})", ref=r, instant=True, fmt="table") for r, e, _ in mem_cols],
    "transformations": [
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {
            "excludeByName": {"Time": True},
            "indexByName": {"instance": 0, **{f"Value #{r}": i + 1 for i, (r, _, _) in enumerate(mem_cols)}},
            "renameByName": {"instance": "節點", **{f"Value #{r}": n for r, _, n in mem_cols}}}},
        {"id": "sortBy", "options": {"sort": [{"field": "實際佔用", "desc": True}]}}],
    "fieldConfig": {"defaults": {"unit": "bytes", "decimals": 1, "custom": {"align": "auto", "minWidth": 90}},
                    "overrides": [
                        col("節點", [{"id": "custom.minWidth", "value": 160}]),
                        col("碎片率", [{"id": "unit", "value": "none"}, {"id": "decimals", "value": 2}]),
                        col("maxmemory", [{"id": "mappings", "value": vmap({"0": ("未設上限", "orange")})}]),
                        col("用滿時預估實際佔用", [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                                  {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}])]},
    "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}})
y += 7
panels += [
    ts("每個 master 的 Key 數",
       desc("每個 master 上有幾個 key（不算 replica，避免重複）。", "各 master 差不多。",
            "某台特別多 = hash tag（{...}）讓大量 key 落在同一個 slot。"),
       [tgt(f"sum by (instance) (redis_db_keys{{{CI}}} {MASTER})", "{{instance}}")],
       gp(0, y, 8, 8), unit="short"),
    ts("有設 TTL 的 key 比例",
       desc("所有 key 裡有設過期時間的比例（只算 master）。", "依業務：純快取應接近 100%。",
            "比例很低且記憶體一直漲 → 程式可能忘了設 TTL，資料只增不減。"),
       [tgt(f"sum(redis_db_keys_expiring{{{CI}}} {MASTER}) / sum(redis_db_keys{{{CI}}} {MASTER})", "全部 master")],
       gp(8, y, 8, 8), unit="percentunit", min_=0, max_=1, legend_calcs=("lastNotNull",)),
    ts("過期與被淘汰的 key",
       desc("每秒有幾個 key 因 TTL 到期被刪（過期），或因記憶體滿被迫刪掉（淘汰）。只算 master。",
            "過期：依業務；**淘汰：0**。",
            "淘汰 > 0 = 記憶體滿了，Redis 在刪還沒過期的資料，應用程式會讀不到 → 加記憶體或清資料。"),
       [tgt(f"sum(rate(redis_expired_keys_total{{{CI}}}[{RI}]) {MASTER})", "過期", "A"),
        tgt(f"sum(rate(redis_evicted_keys_total{{{CI}}}[{RI}]) {MASTER})", "淘汰（被迫刪除）", "B")],
       gp(16, y, 8, 8), unit="short",
       overrides=[col("淘汰（被迫刪除）", [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}])],
       legend_calcs=("max", "lastNotNull")),
]
y += 8

# ⑤ CPU 與連線
panels.append(row("⑤ CPU、連線與帳號：瓶頸在哪、是哪個帳號", y)); y += 1
panels += [
    ts("Redis CPU（主執行緒）",
       desc("Redis 用掉多少 CPU。Redis 指令是單執行緒處理，**100% 就是這台的極限**，加 CPU 核心沒用。紅線 80%。",
            "< 80%。",
            "接近 100% → 這台已滿載：把熱點 key 分散到其他節點、減少大 key 操作、或加 master 分擔。"),
       [tgt(f"sum by (instance) (rate(redis_cpu_sys_main_thread_seconds_total{{{CI}}}[{RI}]) + "
            f"rate(redis_cpu_user_main_thread_seconds_total{{{CI}}}[{RI}])) or "
            f"sum by (instance) (rate(redis_cpu_sys_seconds_total{{{CI}}}[{RI}]) + "
            f"rate(redis_cpu_user_seconds_total{{{CI}}}[{RI}]))", "{{instance}}")],
       gp(0, y, 12, 8), unit="percentunit", th=steps(("green", None), ("red", 0.8)), th_style="line",
       min_=0, max_=1),
    ts("連線數",
       desc("每個節點目前的 client 連線數。", "穩定在一個值（連線池大小 × 應用程式數量）。",
            "一直往上漲不下來 = 連線洩漏（沒歸還連線池或沒關閉）。"),
       [tgt(f"redis_connected_clients{{{CI}}}", "{{instance}}")], gp(12, y, 12, 8), unit="short"),
]
y += 8
panels += [
    ts("新建連線（每秒）",
       desc("每秒有幾條新連線進來。", "很低（連線池建好後就重複使用）。",
            "持續很高 = client 沒用連線池、每次請求都重新連線，會拖慢速度並耗 CPU。"),
       [tgt(f"sum by (instance) (rate(redis_connections_received_total{{{CI}}}[{RI}]))", "{{instance}}")],
       gp(0, y, 8, 7), unit="short"),
    ts("連線數使用率",
       desc("connected_clients / maxclients。黃線 70%、紅線 85%。", "< 70%。", "滿了新連線會被拒絕。"),
       [tgt(f"redis_connected_clients{{{CI}}} / redis_config_maxclients{{{CI}}}", "{{instance}}")],
       gp(8, y, 8, 7), unit="percentunit", th=PCT, th_style="line", min_=0, max_=1),
    ts("拒絕連線（每秒）",
       desc("因為超過 maxclients 被拒絕的連線數。", "0。", "> 0 時應用程式會連不上 → 查連線洩漏或調高 maxclients。"),
       [tgt(f"sum by (instance) (rate(redis_rejected_connections_total{{{CI}}}[{RI}]))", "{{instance}}")],
       gp(16, y, 8, 7), unit="short", legend_calcs=("max", "lastNotNull")),
]
y += 7
NOCL = "沒有資料：exporter 要加 --export-client-list 才有（請維運開啟）"
# client list:info 帶 user / host,其他每條連線的數值只帶 id,要 join 回 info 才分得到帳號
CLI = f'redis_connected_client_info{{{CI},flags!~".*[SM].*",name!="redis_exporter"}}'
def by_user(metric, labels="user"):
    return f"{metric}{{{CI}}} * on(instance, id) group_left({labels}) {CLI}"
panels += [
    ts("各帳號連線數",
       desc("依 ACL 帳號分的連線數（不含 replica 複寫連線與 exporter 自己）。帳號通常對應一個專案。",
            "每個帳號穩定在自己的連線池大小。",
            "某個帳號一直往上漲 = 那個專案連線洩漏；突然歸零 = 那個專案斷線或密碼錯。"),
       [tgt(f"count by (user) ({CLI})", "{{user}}")],
       gp(0, y, 12, 8), unit="short", no_value=NOCL),
    ts("各帳號新建連線（每分鐘）",
       desc("每個帳號最近 1 分鐘新開了幾條連線。抓取間隔內開了又關的連線看不到，實際只會更多。",
            "接近 0（連線池建好後重複使用）。",
            "持續有值 = 那個專案沒用連線池、每次請求都重新連線，會拖慢速度、耗 Redis CPU。"),
       [tgt(f"count by (user) ({CLI} unless on(instance, id) ({CLI} offset 1m))", "{{user}}")],
       gp(12, y, 12, 8), unit="short", no_value=NOCL),
]
y += 8
panels += [
    ts("各帳號最大單次回應（大 key 線索）",
       desc("每個帳號的連線當下積壓在 Redis 的回應大小（輸出緩衝），取每個帳號最大的一條。"
            "這是抓取瞬間的取樣，回應很快送完的可能抓不到。",
            "幾 KB 以內。",
            "某個帳號常出現 MB 級 = 那個專案在一次讀大 key（HGETALL、SMEMBERS、LRANGE 0 -1 等）。"),
       [tgt(f"max by (user) ({by_user('redis_connected_client_output_buffer_memory_usage_bytes')})", "{{user}}")],
       gp(0, y, 12, 8), unit="bytes", no_value=NOCL, legend_calcs=("max", "lastNotNull")),
    {"id": nid(), "type": "table", "title": "各帳號 × 來源主機（現在）", "datasource": DS, "gridPos": gp(12, y, 12, 8),
     "description": desc("每個帳號從哪些主機連進來、各幾條，以及連線存活多久、閒置多久。",
                         "一個帳號只從自己專案的主機連進來；連線存活很久（連線池）。",
                         "同一帳號從很多主機連進來 = 可能多個專案共用帳號；**最短存活**只有幾秒 = 一直重連；"
                         "**最久閒置**很長且連線很多 = 連線池開太大。"),
     "targets": [
         tgt(f"count by (user, host) ({CLI})", ref="A", instant=True, fmt="table"),
         tgt(f"min by (user, host) (time() - {by_user('redis_connected_client_created_at_timestamp', 'user, host')})",
             ref="B", instant=True, fmt="table"),
         tgt(f"max by (user, host) (time() - {by_user('redis_connected_client_idle_since_timestamp', 'user, host')})",
             ref="C", instant=True, fmt="table"),
     ],
     "transformations": [
         {"id": "merge", "options": {}},
         {"id": "organize", "options": {
             "excludeByName": {"Time": True},
             "indexByName": {"user": 0, "host": 1, "Value #A": 2, "Value #B": 3, "Value #C": 4},
             "renameByName": {"user": "帳號", "host": "來源主機", "Value #A": "連線數", "Value #B": "最短存活",
                              "Value #C": "最久閒置"}}},
         {"id": "sortBy", "options": {"sort": [{"field": "連線數", "desc": True}]}}],
     "fieldConfig": {"defaults": {"noValue": NOCL}, "overrides": [
         col("最短存活", [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 0}]),
         col("最久閒置", [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 0}])]},
     "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}},
]
y += 8

# ⑤b 看起來沒滿、卻 timeout:短暫爆滿與主機 / 網路瓶頸
NJ = 'job=~"$node_job"'
NIC = 'device!~"lo|docker.*|veth.*|br-.*|cni.*|flannel.*|virbr.*"'
NONODE = "沒有資料：上方「主機 (node exporter)」要選 Redis 主機的 node exporter"
SPIKE_GUIDE = """**看起來都沒滿、卻 command timeout**：圖表多半是 15 秒～1 分鐘的平均，Redis 是單執行緒，爆滿幾百毫秒就足以讓 timeout 設得短的 client 逾時。

- **先看 ③b「其他指令要排多久」**：秒級 = Redis 在排隊（看是誰佔住）；一直只有幾 ms = Redis 沒塞，問題在主機、網路或 client。
- **右邊 15 秒尖峰**碰到 100%，1 分鐘平均卻不高 → Redis 主執行緒短暫滿了（大指令、Lua、某個 client 一次灌大量指令）。
- **下面三張**：某一顆核心滿（網路中斷 softirq、docker-proxy）、網卡滿、TCP 重傳 → 卡在主機或網路，不在 Redis。
- 都沒有 → 在 timeout 的那台跑 `redis-cli -h <redis> --latency-history -i 1`：PING 正常 = 問題在 client（連線池、執行緒、GC）。"""
panels.append(row("⑤b 看起來沒滿、卻 command timeout：短暫爆滿與主機 / 網路瓶頸", y)); y += 1
MAIN_CPU = ("sum by (instance) ({f}(redis_cpu_sys_main_thread_seconds_total{{{ci}}}[{w}]) + "
            "{f}(redis_cpu_user_main_thread_seconds_total{{{ci}}}[{w}])) or "
            "sum by (instance) ({f}(redis_cpu_sys_seconds_total{{{ci}}}[{w}]) + {f}(redis_cpu_user_seconds_total{{{ci}}}[{w}]))")
panels += [
    {"id": nid(), "type": "text", "title": "", "gridPos": gp(0, y, 9, 8), "options": {"mode": "markdown", "content": SPIKE_GUIDE}},
    ts("Redis 主執行緒 CPU：15 秒尖峰 vs 1 分鐘平均",
       desc("實線是每 15 秒（兩次抓取之間）的 CPU，虛線是 1 分鐘平均。Redis 指令是單執行緒，100% 就是極限。",
            "實線也低於 80%。",
            "實線常碰到 100%、虛線只有一半左右 = 有短暫爆滿，這段時間其他 client 的指令在排隊；"
            "對照 ② 每節點 ops、③b Lua、② 最大的單次回應，看是哪種指令造成。15 秒內更短的爆滿還是看不到，要用 redis-cli 每秒看。"),
       [tgt(MAIN_CPU.format(f="irate", ci=CI, w="1m"), "{{instance}} 15 秒", "A"),
        tgt(MAIN_CPU.format(f="rate", ci=CI, w="1m"), "{{instance}} 1 分鐘平均", "B")],
       gp(9, y, 15, 8), unit="percentunit", th=steps(("green", None), ("red", 0.8)), th_style="line", min_=0, max_=1,
       overrides=[{"matcher": {"id": "byRegexp", "options": ".*1 分鐘平均"},
                   "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 6]}},
                                  {"id": "custom.fillOpacity", "value": 0}]}],
       legend_calcs=("max", "lastNotNull")),
]
y += 8
panels += [
    ts("主機：最忙的那一顆 CPU 核心",
       desc("Redis 主機上最忙的一顆核心（15 秒解析度），以及其中處理網路中斷（softirq）的比例。虛線是所有核心的平均。",
            "最忙的核心也低於 80%。",
            "平均很低、但某一顆核心滿了 = 單核瓶頸：網路中斷都擠在同一顆（softirq 高，要調 RPS / irqbalance）、"
            "docker-proxy 轉送（Docker 用 -p 開 port，可改 host network）、或 Redis 主執行緒跟它們搶同一顆。"),
       [tgt(f"max by (instance) (1 - irate(node_cpu_seconds_total{{{NJ},mode=\"idle\"}}[1m]))", "{{instance}} 最忙的核心", "A"),
        tgt(f"max by (instance) (irate(node_cpu_seconds_total{{{NJ},mode=\"softirq\"}}[1m]))", "{{instance}} 網路中斷（softirq）最高", "B"),
        tgt(f"1 - avg by (instance) (irate(node_cpu_seconds_total{{{NJ},mode=\"idle\"}}[1m]))", "{{instance}} 全部核心平均", "C")],
       gp(0, y, 8, 8), unit="percentunit", th=steps(("green", None), ("red", 0.8)), th_style="line", min_=0, max_=1,
       overrides=[{"matcher": {"id": "byRegexp", "options": ".*全部核心平均"},
                   "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 6]}},
                                  {"id": "custom.fillOpacity", "value": 0}]}],
       no_value=NONODE, legend_calcs=("max", "lastNotNull")),
    ts("主機：網卡使用率（÷ 網卡速度）",
       desc("每張網卡收 / 送的量 ÷ 網卡速度（15 秒解析度）。虛擬機或部分虛擬網卡不回報速度，就不會有線。",
            "低於 70%。",
            "接近 100% = 網卡滿了，所有 client 的回應都在排隊，Redis 本身卻不忙；通常是大 key / 大回應（看 ② 最大的單次回應）。"),
       [tgt(f"max by (instance) (irate(node_network_receive_bytes_total{{{NJ},{NIC}}}[1m]) / "
            f"(node_network_speed_bytes{{{NJ},{NIC}}} > 0))", "{{instance}} 收", "A"),
        tgt(f"max by (instance) (irate(node_network_transmit_bytes_total{{{NJ},{NIC}}}[1m]) / "
            f"(node_network_speed_bytes{{{NJ},{NIC}}} > 0))", "{{instance}} 送", "B")],
       gp(8, y, 8, 8), unit="percentunit", th=steps(("green", None), ("red", 0.8)), th_style="line", min_=0, max_=1,
       no_value="沒有資料：網卡沒有回報速度（常見於虛擬機），改看 ⑦ 主機網路的流量", legend_calcs=("max", "lastNotNull")),
    ts("主機：TCP 重傳、連線佇列溢出、conntrack",
       desc("**TCP 重傳率** = 重送的封包 ÷ 送出的封包　**連線佇列溢出** = 新連線來不及接、被丟掉（每秒）　"
            "**conntrack 使用率** = 連線追蹤表用了幾成（Docker / iptables NAT 會用到）。",
            "重傳率 < 1%；溢出 0；conntrack < 70%。",
            "重傳率高 = 網路掉封包，client 要等重送（常見 200ms 起跳）；溢出 > 0 = 連線進不來（tcp-backlog / somaxconn 太小）；"
            "conntrack 接近 100% = 新連線被丟。"),
       [tgt(f"sum by (instance) (irate(node_netstat_Tcp_RetransSegs{{{NJ}}}[1m])) / "
            f"(sum by (instance) (irate(node_netstat_Tcp_OutSegs{{{NJ}}}[1m])) > 0)", "{{instance}} TCP 重傳率", "A"),
        tgt(f"node_nf_conntrack_entries{{{NJ}}} / (node_nf_conntrack_entries_limit{{{NJ}}} > 0)", "{{instance}} conntrack 使用率", "B"),
        tgt(f"sum by (instance) (irate(node_netstat_TcpExt_ListenOverflows{{{NJ}}}[1m]) + "
            f"irate(node_netstat_TcpExt_ListenDrops{{{NJ}}}[1m]))", "{{instance}} 連線佇列溢出 / 秒", "C")],
       gp(16, y, 8, 8), unit="percentunit", min_=0,
       overrides=[{"matcher": {"id": "byRegexp", "options": ".*連線佇列溢出.*"},
                   "properties": [{"id": "unit", "value": "short"}, {"id": "custom.axisPlacement", "value": "right"}]}],
       no_value=NONODE, legend_calcs=("max", "lastNotNull")),
]
panels[-1]["fieldConfig"]["defaults"]["custom"]["axisSoftMax"] = 0.05  # 都是 0 時刻度不要放大到 10000%
y += 8

# ⑥ 複寫、failover、背景存檔 (預設收合)
y6 = y
p6 = [
    timeline("角色變化（failover 時間軸）",
             desc("每個節點在這段時間是 master 還是 replica。", "整段不變。",
                  "顏色切換的時間點 = 發生 failover；對照應用程式錯誤的時間。"),
             f"max by (instance) (redis_instance_info{{{CI},role=\"master\"}}) or on(instance) "
             f"(0 * max by (instance) (redis_instance_info{{{CI},role!=\"master\"}}))",
             gp(0, y6 + 1, 12, 7), vmap({"1": ("master", "blue"), "0": ("replica", "#8c8c8c")})),
    timeline("背景存檔中（RDB / AOF 重寫）",
             desc("節點是否正在做背景存檔。存檔會 fork 行程，fork 那一下主執行緒會停住。", "偶爾出現、很短。",
                  "延遲突刺剛好對上這裡 = 存檔造成；資料量大時 fork 更久，見右下「fork 耗時」。"),
             f"max by (instance) (redis_rdb_bgsave_in_progress{{{CI}}} + redis_aof_rewrite_in_progress{{{CI}}})",
             gp(12, y6 + 1, 12, 7), vmap({"0": ("閒置", "transparent"), "1": ("存檔中", "orange")}), show_value="never"),
    ts("複寫延遲（replica 落後多少資料）",
       desc("master 已寫入、但 replica 還沒收到的資料量。", "接近 0。",
            "持續上升 = replica 跟不上寫入速度；此時 failover 會遺失這段資料。"),
       [tgt(f"max by (instance, slave_ip) (redis_master_repl_offset{{{CI}}} - on(instance) group_right "
            f"redis_connected_slave_offset_bytes{{{CI}}})", "{{instance}} → {{slave_ip}}")],
       gp(0, y6 + 8, 12, 7), unit="bytes"),
    ts("fork 耗時（最近一次）",
       desc("最近一次背景存檔 fork 花多久，這段時間 Redis 完全停住。", "幾 ms ～ 幾十 ms。",
            "> 100ms 會造成明顯延遲突刺；記憶體越大越久。"),
       [tgt(f"redis_latest_fork_seconds{{{CI}}}", "{{instance}}")], gp(12, y6 + 8, 12, 7), unit="s",
       legend_calcs=("max", "lastNotNull")),
]
panels.append(row("⑥ 複寫、Failover 與背景存檔（點開）", y6, collapsed=True, panels=p6)); y += 1

# ⑦ 主機 (node exporter)
y7 = y
N = 'job=~"$node_job"'
p7 = [
    ts("主機 CPU 使用率",
       desc("Redis 所在主機的整體 CPU（所有核心平均）。紅線 80%。", "< 80%。",
            "主機 CPU 很高但 ⑤ Redis CPU 不高 = 被同主機的其他程式搶資源。"),
       [tgt(f"1 - avg by (instance) (rate(node_cpu_seconds_total{{{N},mode=\"idle\"}}[{RI}]))", "{{instance}}")],
       gp(0, y7 + 1, 12, 7), unit="percentunit", th=steps(("green", None), ("red", 0.8)), th_style="line",
       min_=0, max_=1),
    ts("主機記憶體使用率",
       desc("主機記憶體使用率。黃線 70%、紅線 85%。", "< 70%。",
            "Redis 背景存檔時最多可能需要接近兩倍記憶體；太滿會被系統 OOM kill 或用到 swap 變很慢。"),
       [tgt(f"1 - node_memory_MemAvailable_bytes{{{N}}} / node_memory_MemTotal_bytes{{{N}}}", "{{instance}}")],
       gp(12, y7 + 1, 12, 7), unit="percentunit", th=PCT, th_style="line", min_=0, max_=1),
    ts("主機網路（正值收、負值送）",
       desc("主機網卡每秒收送量（不含 lo、docker、veth 等虛擬網卡）。", "遠低於網卡上限。",
            "接近上限（例如 1Gbps ≈ 125MB/s）就會變慢。"),
       [tgt(f"sum by (instance) (rate(node_network_receive_bytes_total{{{N},device!~\"lo|docker.*|veth.*|br-.*|cni.*|flannel.*\"}}[{RI}]))",
            "{{instance}} 收", "A"),
        tgt(f"-sum by (instance) (rate(node_network_transmit_bytes_total{{{N},device!~\"lo|docker.*|veth.*|br-.*|cni.*|flannel.*\"}}[{RI}]))",
            "{{instance}} 送", "B")],
       gp(0, y7 + 8, 12, 7), unit="Bps", fill=0),
    ts("主機磁碟 IOPS（正值讀、負值寫）",
       desc("每秒磁碟讀寫次數。Redis 平常幾乎不碰磁碟，存檔（RDB / AOF）時才會。", "平時很低，存檔時短暫升高。",
            "AOF 開啟且寫入 IOPS 長時間很高 → 磁碟可能拖慢寫入（appendfsync always / everysec）。"),
       [tgt(f"sum by (instance) (rate(node_disk_reads_completed_total{{{N},device=~\"(sd|vd|xvd|nvme|dm-).*\"}}[{RI}]))",
            "{{instance}} 讀", "A"),
        tgt(f"-sum by (instance) (rate(node_disk_writes_completed_total{{{N},device=~\"(sd|vd|xvd|nvme|dm-).*\"}}[{RI}]))",
            "{{instance}} 寫", "B")],
       gp(12, y7 + 8, 12, 7), unit="iops", fill=0),
]
panels.append(row("⑦ 主機資源（node exporter，點開）", y7, collapsed=True, panels=p7)); y += 1

def var_query(name, label, query, regex="", multi=False, include_all=False, refresh=1, hide=0):
    return {"name": name, "label": label, "type": "query", "datasource": DS, "hide": hide,
            "query": {"query": query, "refId": "PrometheusVariableQueryEditor-VariableQuery", "qryType": 1},
            "definition": query, "regex": regex, "refresh": refresh, "multi": multi, "includeAll": include_all,
            "sort": 1, "current": {}, "options": []}

dash = {
    "title": "Redis 問題排查（開發人員）",
    "uid": "redis-dev-troubleshoot",
    "description": "給開發人員：應用程式遇到 Redis 相關問題時，依症狀找方向。每張圖 ⓘ 有說明。",
    "tags": ["redis", "troubleshooting"],
    "editable": True, "graphTooltip": 1, "fiscalYearStartMonth": 0, "liveNow": False,
    "refresh": "30s", "time": {"from": "now-1h", "to": "now"},
    "timepicker": {"refresh_intervals": ["10s", "30s", "1m", "5m", "15m"]},
    "timezone": "browser", "schemaVersion": 41, "version": 1, "links": [],
    "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                              "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                              "name": "Annotations & Alerts", "type": "dashboard"}]},
    "templating": {"list": [
        {"name": "datasource", "label": "環境", "type": "datasource", "query": "prometheus",
         "regex": sys.argv[2] if len(sys.argv) > 2 else "Prometheus-(SIT|UAT|PROD-NH|PROD-BQ|PROD-BQ1)",
         "refresh": 1, "hide": 0, "current": {}, "options": []},
        var_query("cluster", "Cluster", "label_values(redis_up,job)", r"^(?!.*_node$).*redis.*", refresh=2),
        var_query("instance", "節點", 'label_values(redis_up{job="$cluster"},instance)', "",
                  multi=True, include_all=True, refresh=2),
        var_query("node_job", "主機 (node exporter)", "label_values(node_uname_info,job)",
                  r"^(?!.*test).*redis_node$", refresh=2),
    ]},
    "panels": panels,
}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as f:
    json.dump(dash, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("panels", sum(1 for p in panels if p["type"] != "row") + len(p6) + len(p7), "max id", _id[0])
