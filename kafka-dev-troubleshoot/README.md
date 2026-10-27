# Kafka 問題排查（開發人員）Dashboard

給開發人員：應用程式遇到 Kafka 相關問題（lag 一直漲、寫入失敗、權限錯誤、登入失敗、變慢）時，依症狀找方向。分成兩個 dashboard，右上角互相連結（會帶著目前選的環境 / Cluster / Topic / Group）：

| Dashboard | 用途 | 時間 |
|---|---|---|
| **Kafka 即時現況** | 現在有沒有問題、追得上嗎、多久追上 | 隱藏時間選擇器，永遠是最新狀態（每 30 秒更新） |
| **Kafka 問題排查** | 回看過去、評估壓測 | 全部跟著上方時間範圍走 |

為什麼要拆成兩個：表格和單一數字只看時間範圍「結束那一刻」。同一個 dashboard 裡沒有辦法讓面板固定看「現在」（Grafana 面板的 Relative time 覆寫只在相對時間範圍有效，在圖上拖曳選取後就失效），所以即時的放在另一個 dashboard。

每個面板的 ⓘ 有「是什麼／正常／不正常時」說明。

## 檔案

| 檔案 | 說明 |
|---|---|
| `kafka-dev-troubleshoot-dashboard.json` | Kafka 問題排查（跟著時間範圍） |
| `kafka-dev-troubleshoot-dashboard-live.json` | Kafka 即時現況 |
| `gen_dashboard.py` | 產生上面兩個 JSON 的腳本。要改面板就改這裡，再執行 `python gen_dashboard.py kafka-dev-troubleshoot-dashboard.json` 重新產生（`-live.json` 會一起產生） |

第二個參數可以限制「環境」下拉選單只列出符合 regex 的 Prometheus 資料來源（預設不限制）：

```
python gen_dashboard.py kafka-dev-troubleshoot-dashboard.json "Prometheus-(SIT|UAT|PROD-NH|PROD-BQ|PROD-BQ1)"
```

kafka-exporter 的 scrape 間隔不是 15 秒時，用 `--scrape` 指定（例如 `--scrape=30s`、`--scrape=1m`）。Lag 圖的取樣解析度、頭尾平均的長度都依它決定（頭尾平均 = 4 次 scrape，最少 1 分鐘）。Grafana 資料來源設定裡的 **Scrape interval** 也要填一樣的值，`$__rate_interval` 才算得對。

## 匯入

1. Grafana → Dashboards → New → Import，**兩個 JSON 都要匯入**（右上角的連結靠 `kafka-dev-troubleshoot` 這個 tag 找到對方）。
2. 上方變數依序選：
   - **環境**：Prometheus 資料來源。
   - **Cluster**：Kafka 叢集（取自 kafka-exporter 的 `cluster` label）。
   - **Topic** / **Consumer Group**：預設全選。
   - **比較區間**：速度、寫入 / 消費筆數、Lag 變化、追上需要都用這一段算（5m / 15m / 1h，預設 5m）。舊版叫「速度取樣」，舊連結帶的值會被忽略、改用預設。

需求（括號內是實測版本）：

- jmx_exporter Java agent（1.6.0）
- danielqsj/kafka-exporter（v1.9.0）
- node_exporter（v1.9.1）
- Prometheus（v3.2.0）、Grafana 12

三個 exporter 的 scrape job 都要帶**同一個 `cluster` label**，dashboard 用它篩選。

## 區塊

**Kafka 即時現況**

- Kafka 本身有沒有壞
- Consumer 追得上嗎：追得上嗎（Lag、成員、追上需要、還落後）、近 5m 寫入 / 消費、會不會沒讀就被刪、有 lag 的 Partition
- Topic：規模、流量
- 連線與副本：連線的 client 程式、副本沒跟上的 Partition、Leader 不在首選、磁碟幾小時後滿

**Kafka 問題排查**

1. 使用說明：依症狀找方向（預設收合）
2. ① Kafka 本身有沒有壞（期間最糟）
3. ② Consumer 有沒有追上、Topic 是否滿載：4 張期間表格 + 走勢圖
4. ③ 延遲：寫入 / 讀取慢在哪一段
5. ④ 錯誤與權限：應用程式收到什麼錯誤
6. ⑤ 各帳號（專案）與連線（預設收合）
7. ⑥ Broker 資源（預設收合）
8. ⑦ 副本與 Partition 健康（維運用，預設收合）

## 壓測回顧

在 **Kafka 問題排查** 把時間範圍選壓測開始前到結束後（或在任一張走勢圖上拖曳選取），然後看：

- ① **期間最糟**：每格是期間內最高（Broker 數、Active Controller 取最低）。每次 scrape 都看，短暫尖峰也抓得到；小走勢圖看得出是什麼時候。
- ② 期間表格：
  - **期間落後多少**：最高 Lag、最高還落後、**落後的時間**（期間 lag 超過 30 秒寫入量的總時間，實際量到的；壓測時 ≈ 從開始落後到追上花了多久）。
  - **期間寫入 / 消費**：範圍頭尾相減，期間寫入 − 期間消費 = Lag 變化。範圍開始時 group 還沒 commit 過會顯示「範圍開頭還沒有」，把範圍開頭往後調。
  - **期間處理速度**：最高消費/秒 ≈ consumer 目前配置能處理的上限；明顯低於最高寫入/秒就是瓶頸。
  - **期間 consumer 與保留**：最少成員、成員變動次數、最高佔保留量。
  - 時間範圍很長（例如 7 天）時會算比較久。
- ② 走勢圖：Lag、速度、**預估追上需要**、還落後、成員數、Lag 佔保留量是什麼時候開始變化的。圖例的 Mean / Max 是整個範圍的值。

## 幾個容易誤會的數字

- **寫入、消費、Lag 變化**（即時的「近 5m」、問題排查的「期間」）：都是區間頭尾相減的筆數，所以一定對得上：**寫入 − 消費 = Lag 變化**（四捨五入可能差 1）。
  - 資料每次 scrape 才一筆，consumer 又是每隔幾秒才 commit，lag 本來就是鋸齒狀。頭尾各取一小段平均（4 次 scrape，最少 1 分鐘）再相減，鋸齒才不會被當成增減。
  - kafka-exporter 算 lag 時，log end 是另外跟 broker 要的，跟 topic 的 offset 指標不是同一個瞬間。所以寫入用「lag + commit 位置」還原 lag 用的那個 log end，三個數字才會剛好對上。
  - 只算這個 group 頭尾都 commit 過的 partition，所以寫入可能比 Topic 流量表少。
  - **Lag 變化**在 ±10 筆內視為持平。
  - **消費**是負數 = offset 被往回重設（重讀）。
- **沒 commit 的 Partition**：group 在這個 topic 還沒 commit 過的 partition 數。kafka-exporter 對這些 partition 回報 lag = -1，所以 Lag 算 0、寫入 / 消費 / Lag 變化也不算它們；> 0 時這些 partition 的積壓看不到，確認是不是沒有 consumer 分到，或程式只在處理到資料時才 commit。整個 topic 都沒 commit 過時，exporter 不會列出這個 group × topic。
- **追上需要**（即時）：Lag ÷ 比較區間內 lag 減少的速度。依序判斷：
  1. 沒有 lag → 已跟上
  2. 有 lag 但沒有 consumer → 沒人在讀
  3. lag 不到 30 秒的寫入量 → 已跟上
  4. lag 在減少 → 算出時間；超過 1 天顯示「> 1 天」
  5. lag 在增加 → 越落越多
  6. lag 增減在 ±10 筆內 → 持平（沒在追上）
  7. 比較區間開頭還沒有這個 group 的資料 → 資料不足
- **預估追上需要**（問題排查的走勢圖）：同上的第 4 種，每個時間點都算一次；lag 沒在減少、或已跟上時不畫，Y 軸是對數刻度。lag 剛開始下降時比較區間裡還混著上升的那段，數字會偏大；實際花多久看「落後的時間」。資料沒讀就被刪時 lag 也會下降，那時的數字沒有意義。
- **還落後**：Lag ÷ 平均寫入速度換算的估算值。Kafka 指標沒有訊息時間，寫入忽快忽慢時只是概略值；要精準請在應用程式記錄訊息時間。
- **表格某格顯示灰字**（—、資料不足、範圍開頭還沒有…）：那一列這一欄算不出來，說明文字就是原因。
- **速度圖**：每個點是那個時間點往前「比較區間」的平均，會比 Lag 圖慢半拍。
- **Lag 圖**：每個點取前一小段（至少 4 次 scrape）的最高值，時間範圍拉長也不會漏掉高點。
- **Lag 佔保留量** = 還沒讀的筆數 ÷ Kafka 這個 partition 現在還留著的筆數（取最糟的 partition）。
  - 只用 offset 算（kafka-exporter），不是 segment，也跟 retention 是依時間還是大小無關，不需要 jmx。
  - 例：還留著 1,000 筆、其中 950 筆沒讀 = 95%，再刪一點就會刪到沒讀的。
  - 各情境顯示：

    | 情況 | 顯示 |
    |---|---|
    | 有成員、≤ 100% | 百分比（70% 黃、85% 紅） |
    | 有成員、超過 100% | 已掉資料（紅） |
    | 成員 0、有 lag | 沒人在讀（灰）。沒人讀的 group 遲早都會到 100%，百分比沒意義所以不顯示；確認程式該不該在跑，不用了就 `kafka-consumer-groups --delete --group <group>` |
    | 成員 0、超過 100% | 已掉資料（沒人讀）（橘）：沒人讀的期間資料已被刪 |
    | lag 0 | 0% |

  - **100% 不一定很急**：Kafka 是整個 segment 一起刪，還在寫的那個 segment 不會刪。partition 只留幾筆時（例：留 6 筆、6 筆都沒讀 = 100%）通常要等好幾天才會刪，搭配 Lag 筆數看。
  - **已掉資料**（超過 100%）：沒讀的資料已經被刪了，救不回來。consumer 會收到 `OFFSET_OUT_OF_RANGE`，之後從哪讀看 `auto.offset.reset`（earliest = 跳到最舊還留著的、latest = 跳到最新、none = 報錯）。
  - Kafka 的資料是整個 topic 共用一份，被刪的那段所有 group（包括之後新訂閱的）都讀不到；百分比是每個 group 各自算，已經追上的 group 不受影響。
  - topic 的 retention 設多少，dashboard 看不到，要用 `kafka-configs --describe --entity-type topics --entity-name <topic>` 查。
- **讀取延遲約 500 ms**：是正常的。沒有新資料時，broker 會等滿 `fetch.max.wait.ms`（預設 500 ms）才回應。

## jmx exporter 設定

jmx exporter 的規則有兩種寫法，dashboard 都支援：

- **官方範例 `kafka-kraft-3_0_0.yml`**：帳號、listener 會被寫進 metric 名稱，帳號名稱的 `-` 會顯示成 `_`。
- **自訂規則**：帳號、listener 拆成 label，例如 `kafka_server_client_quota_byte_rate{user=...}`。

面板顯示「jmx exporter 設定沒收 X」時，在 `includeObjectNames` 加上 X：

| 面板 | 要收的 MBean |
|---|---|
| ④ ACL 授權檢查、權限被拒的 authorizer 線 | `kafka.server:type=plugins,config=authorizer.class.name,*` |
| ① ④ 登入失敗、⑤ 連線數、即時現況的連線的 client 程式 | `kafka.server:type=socket-server-metrics,listener=*,networkProcessor=*`、`kafka.server:type=socket-server-metrics,clientSoftwareName=*,clientSoftwareVersion=*,listener=*,networkProcessor=*` |
| ⑤ 各帳號佔用 broker 處理時間 | `kafka.server:type=Request,user=*` |
| ② Rebalance 次數 | `kafka.server:type=group-coordinator-metrics`、`kafka.coordinator.group:type=GroupMetadataManager,*` |
| ③ 延遲拆解的後三段 | `kafka.network:type=RequestMetrics,name={ThrottleTimeMs,ResponseQueueTimeMs,ResponseSendTimeMs},request={Produce,FetchConsumer}` |
| ② Topic 磁碟大小 | `kafka.log:type=Log,name=Size,*`（每個 partition 一條，partition 多時請評估） |

自訂規則常見的坑：

- **錯誤回應**：Fetch 的錯誤是 `request=Fetch`（不分 FetchConsumer / FetchFollower）；`GROUP_AUTHORIZATION_FAILED` 在 `FindCoordinator`。建議直接收 `kafka.network:type=RequestMetrics,name=ErrorsPerSec,*`。
- **KRaft 通用規則**：寫成 `kafka.server<type=(.+)><>...` 會連 `type=Produce, user=...` 這類 MBean 一起配到，後面的 quota 規則就永遠輪不到。改成 `type=([a-z-]+)` 只配單一屬性的 MBean。
- **group-coordinator-metrics**：這是 Kafka metrics，不是 yammer metrics，MBean 沒有 `name=` 屬性，寫 `name=...` 會一個都抓不到。

## 各帳號流量（⑤）

Kafka 要有設定 quota，broker 才會記錄每個帳號的流量。設一個很大的預設值就不會真的限速，也不用重啟 broker：

```
kafka-configs --alter --entity-type users --entity-default \
  --add-config producer_byte_rate=1073741824,consumer_byte_rate=1073741824,request_percentage=10000
```

## 這裡看不到的

- **哪個帳號被拒**：Kafka 只提供拒絕總數，要看 broker 的 authorizer log。
- **每則訊息的端到端延遲**：要應用程式自己埋點。
- **Topic 設定**（retention 等）：用 `kafka-configs --describe` 查。
