# OCP 壓測影響評估 Dashboard

在 OpenShift 上壓測某個 Deployment 時，用來判斷它會不會拖慢**同一台 Node** 上的其他服務。

Pod 已經有 CPU / 記憶體 limit，但下面這些是**整台 Node 共用、limit 管不到**的資源，也是這個 Dashboard 的重點：

- 本機磁碟 IO（寫 log、暫存檔、容器可寫層）
- 本機磁碟空間
- 網卡頻寬與封包處理（核心處理封包的 CPU 也不算在 Pod 的 CPU limit 裡）
- conntrack 連線追蹤表

OCP 沒有特別設定時，不會限制 Pod 的網路頻寬與磁碟讀寫速度，一個 Pod 可以用到整台 Node 的上限。

## 兩個 Dashboard 的分工

| Dashboard | 用途 |
|---|---|
| **OCP 叢集風險總覽** | 全叢集每台 Node、每個 Workload 的排行，先找出嫌疑對象 |
| **OCP 壓測影響評估** | 針對單一 Deployment，查證它有沒有拖慢所在 Node 上的其他服務 |

使用流程：在總覽看哪台 Node 變吃緊、哪個 Workload 排名前面 → 點表格中的 `workload` 名稱 → 自動帶著 namespace、workload 與時間範圍開啟細節 Dashboard。細節 Dashboard 右上角也有連結可以回到總覽。

## 檔案

| 檔案 | 說明 |
|---|---|
| `ocp-cluster-risk-overview-dashboard.json` | 叢集風險總覽 |
| `ocp-loadtest-impact-dashboard.json` | 單一 Workload 細節 |
| `gen_overview_dashboard.py`、`gen_dashboard.py` | 產生上面兩個 JSON 的腳本。要改面板就改腳本，再執行 `python <腳本>` 重新產生 |

## 叢集風險總覽

一個資料來源對應一個 OCP 叢集，切換「資料來源（叢集）」就是切換叢集。

| 區塊 | 內容 |
|---|---|
| 1.【Node】各 Node 狀態 | 每台 Node 的 CPU、可用記憶體、conntrack、磁碟延遲、檔案系統、網路丟包、iowait、網路封包處理 CPU、壓力狀態，加上各項 Top 10 趨勢 |
| 2.【Workload】風險排行 | 每個 Deployment / StatefulSet / DaemonSet 的 Pod 數、CPU 用量與 ÷ Request、throttling、記憶體 ÷ Limit、重啟、網路流量與丟包、磁碟寫入、可寫層用量 |
| 3.【Workload】limit 相關 Top 10 | 主要影響他自己的指標隨時間的變化 |
| 4.【Workload】共用資源 Top 10 | limit 管不到、會影響同 Node 鄰居的指標隨時間的變化 |

注意事項：

- **表格是快照**：表格取的是時間範圍**結束前 5 分鐘**。要看壓測當下的排名，把時間範圍的結束點拉到壓測期間；Top 10 趨勢圖則會顯示整段時間。
- **排名高不等於造成影響**：只代表是大戶，要進細節 Dashboard 對照 Node 壓力的時間點。
- **預設排除系統 namespace**：`openshift.*|kube-.*`，可在上方「排除的 Namespace」修改或清空。
- **點擊跳轉只支援 Deployment**：細節 Dashboard 以 Pod 名稱規則對應 Deployment，StatefulSet、DaemonSet 點進去會沒有資料。
- **使用 hostNetwork 的 Pod**：網路指標會是整台 Node 的流量，排名可能偏高。

## 匯入

1. Grafana → Dashboards → New → Import，分別上傳兩個 JSON。兩個都要匯入，互相跳轉的連結才會有效。
2. 上方變數依序選：
   - **資料來源**：只會列出名稱符合 `Prometheus.*OCP`（開頭 `Prometheus`、結尾 `OCP`）的 Prometheus 資料來源。要改規則，修改 `gen_dashboard.py` 裡 datasource 變數的 `regex`。
   - **Namespace**、**Deployment**：要壓測的服務。
   - **Node**：會自動帶出該 Deployment 的 Pod 所在的 Node，預設全選。
   - **網卡上限 Mbps、磁碟吞吐上限 MB/s、磁碟 IOPS 上限**：給第 3 區【上限】面板用，預設 0。網卡填 0 會改用 Node 回報的速度；磁碟填 0 則不計算。
3. 時間範圍要**包含壓測前**的一段時間，才看得出壓測時有沒有往上飆。

## 先搞清楚「在看誰」

每個面板標題都有前綴，代表這個面板看的對象。**這是看懂這個 Dashboard 最重要的一點。**

| 前綴 | 看的是 | 用來回答 |
|---|---|---|
| 【他】 | 他自己的 Pod 合計 | 他用了多少、有沒有被自己的 limit 卡住 |
| 【占比】 | 他 ÷ 所在 Node **目前的總用量** | 他在這台 Node 上算不算大戶 |
| 【上限】 | 整台 Node 和他各自 ÷ **硬體上限** | 資源離滿還有多遠、用量主要是不是他 |
| 【整台 Node】 | 整台 Node 所有 Pod 加總，**不是他的用量** | 這台機器有沒有變吃緊 |
| 【鄰居】 | 同 Node 上所有 Pod 的排名（含他自己） | 誰是大戶、鄰居有沒有跟著變化 |
| 【共用】 | 全叢集共用元件（Router、DNS） | 入口和 DNS 有沒有被打高 |

【整台 Node】的指標**沒辦法告訴你是誰造成的**，它只回答「這台機器有沒有變吃緊」。要判斷是不是他造成的，要把三個面向的時間對起來：

1. 【他】的用量在壓測時上升；
2. 【整台 Node】的壓力指標在**同一時間**往上飆；
3. 【鄰居】排名裡他是前幾名，或其他 Pod 的狀況跟著變差。

三者都成立，才算他影響了整體。只有占比高，不能下結論。

## 為什麼看的是「他所在的 Node」

磁碟、網卡、conntrack 都是**每台 Node 各自一份**，所以他只會直接影響和他跑在同一台 Node 上的 Pod。這也是 `Node` 變數會自動帶出他所在 Node 的原因。

有幾個例外，影響範圍會超出他所在的 Node：

- **Pod 會移動**：HPA 擴容或 Pod 重建時，可能被排到新的 Node。第 1 區「各 Node 上的 Pod 數」可以看到部署位置隨時間的變化，新出現的 Node 也要一起看。
- **PVC 用網路儲存**（Ceph、NFS、SAN）：IO 會打到共用的儲存後端，影響所有用同一個儲存的服務，不限同一台 Node。這部分要看儲存系統自己的監控。
- **入口與 DNS**：Router 與 CoreDNS 是全叢集共用，所以放在第 6 區。
- **外部服務**（DB、Kafka、Redis）：連線數與負載會隨他的副本數放大，要在各系統自己的監控對照同一個時間窗。

## 各區塊說明

### 1. 總覽：他自己有沒有被限制卡住、部署在哪

快速看他的副本數、CPU 用量 ÷ request、記憶體 ÷ limit、CPU throttling、重啟次數，以及他占所在 Node 的 CPU 比例。下方有 Pod 分布在哪些 Node 的圖和對應表。

### 2. 【他】自己的用量

他的 CPU、記憶體、網路流量、網路丟包、磁碟讀寫、可寫層用量、重啟與 OOMKilled。CPU 面板會疊上「昨天同時段」，用來確認這次壓測多出多少負載。

### 3. 【占比】與【上限】

**【占比】**：CPU、網路、磁碟寫入、記憶體各占所在 Node **目前總用量**的比例，只計算跑在所選 Node 上的 Pod。占比只說明他是不是大戶：Node 總共只跑 10 MB/s 時，他占 80% 也只有 8 MB/s，網卡其實很閒。

**【上限】**：跟硬體上限比，每台 Node 畫兩條線。

- **整台**：整台 Node 用掉上限的幾成，接近紅色虛線代表這項資源快滿。
- **他**：他自己用掉上限的幾成，越貼近整台的線，代表用量主要來自他。

| 面板 | 上限從哪裡來 |
|---|---|
| 網路送出 TX、接收 RX | Node 回報的網卡速度（`node_network_speed_bytes`）；上方「網卡上限 Mbps」填非 0 的值會覆蓋 |
| 磁碟吞吐 | 上方「磁碟吞吐上限 MB/s」，**沒有指標會回報，必須手動填** |
| 磁碟 IOPS | 上方「磁碟 IOPS 上限」，**沒有指標會回報，必須手動填** |
| 磁碟空間 | 檔案系統容量，自動計算 |

**上限要填多少**：先看第 3 區最下方的「【上限】參考」表格，列出每台 Node 回報的網卡速度，以及過去 7 天實際跑到的網路、磁碟峰值。峰值只代表「至少撐得住這麼多」：到 Grafana Explore 看峰值那段時間的磁碟延遲與網路丟包，都正常就代表上限更高；延遲已經明顯上升，這附近就是實際可用上限。也可以直接在 Node 上查規格：

```bash
oc debug node/<node名稱> -- chroot /host cat /sys/class/net/<網卡名稱>/speed
```

```bash
oc debug node/<node名稱> -- chroot /host lsblk -d -o NAME,ROTA,SIZE,MODEL
```

第一個回傳網卡速度（Mbps）；第二個列出本機磁碟，`ROTA=0` 是 SSD。

上限數值的注意事項：

- **虛擬機的網卡速度不一定準**：VMware 的 vmxnet3 通常固定回報 10 Gbps，有些虛擬網卡則不回報。雲端或虛擬化平台常另外限制每台 VM 的頻寬，比網卡標示的低。知道實際可用頻寬時，請手動填入。
- **磁碟上限向誰問**：地端找儲存或虛擬化團隊；雲端查磁碟類型的規格（例如 AWS gp3 預設 125 MB/s、3000 IOPS，有額外購買則較高）。
- **磁碟上限是 Node 上所有本機磁碟合計**，Node 有多顆本機磁碟時請填合計值。
- **網卡用 bond 時**，bond 和底下的實體網卡都會被算進流量，使用率可能偏高。

### 4. 【整台 Node】共用資源壓力

這一區是**整台 Node 的狀態**，不是他的用量。看壓測前和壓測中的曲線有沒有明顯往上飆。

### 5. 【鄰居】所選 Node 上用量前 10 名

第 4 區往上飆的時候，看是誰同時上升。

### 6. 【共用】入口與 DNS

Router 的回應狀態碼、後端排隊，以及 CoreDNS 的查詢量與延遲。

## 指標說明

### PSI（Pressure Stall Information）

Linux 核心記錄的「有工作因為搶不到資源而在等」的時間比例，分成 CPU、IO、記憶體三種。它比 CPU 使用率更直接回答「大家有沒有變慢」：CPU 使用率 90% 但沒人在等，其實沒問題；PSI 上升才代表有工作真的被卡住。

- **怎麼看**：壓測前後比較，往上飆就是 Node 開始吃緊。粗略參考：CPU 或 IO 持續超過 10% 值得注意。
- **沒有資料**：Node 核心沒開 PSI。RHCOS 預設可能關閉，需要加 `psi=1` 核心參數才會有。這時改看「PSI 替代：iowait 與每核負載」面板。

### iowait 與每核負載（PSI 的替代）

- **iowait**：CPU 閒著在等磁碟的時間比例。往上飆代表磁碟變慢。
- **每核負載**：`load1 ÷ CPU 核數`，代表平均每顆核有幾個工作在排隊。長時間超過 100% 代表工作在排隊。

### 磁碟延遲（await）

每次讀寫平均要等多久。**判斷磁碟有沒有拖慢大家，這個最準。** 粗略參考：SSD 通常數 ms 以內，超過 10–20 ms 就很慢。

### 磁碟忙碌程度（io time / %util）

磁碟有在處理 IO 的時間比例。接近 100% 可能飽和，但 SSD 和雲端磁碟可以同時處理多筆 IO，100% 不一定代表慢，要搭配磁碟延遲一起看。

### IOPS

每秒完成的讀寫次數。IOPS 上升而延遲沒變，代表磁碟還撐得住；IOPS 上不去但延遲飆升，代表已經到磁碟上限。

### 檔案系統使用率與 DiskPressure

Node 本機磁碟空間用了幾成，容器映像、容器可寫層、log 都放在這裡。剩餘空間太少時，kubelet 會把 Node 標成 DiskPressure 並開始驅逐 Pod，同 Node 的其他服務也會被趕走。超過 85% 就要注意。

### 網路封包處理 CPU（softirq）

Node 核心處理網路封包（以及部分磁碟完成通知）所花的 CPU。這部分 CPU **不算在 Pod 的 CPU limit 裡**，所以就算他的 CPU 被限制住，大流量還是會吃掉整台 Node 的 CPU。壓測時跟著網路流量一起往上飆，就是大流量在消耗 Node 的 CPU。持續超過 10% 值得注意。

### 網路丟包 / 錯誤

- **丟包（drop）**：網卡或核心的緩衝區塞滿、處理不及，封包被丟掉。
- **錯誤（error）**：封包損壞或硬體問題。

兩者正常都應接近 0。壓測期間開始出現並往上升，代表網卡塞住，會造成重傳與延遲變高，同 Node 所有 Pod 都受影響。

### conntrack 連線追蹤表

Node 核心記錄每條網路連線的表格，同 Node 所有 Pod 共用一張，有容量上限。大量短連線（例如每次都重新連 DB、Redis、Kafka）最容易吃滿。超過 70% 要注意；滿了之後新連線會被直接丟掉，同 Node 所有 Pod 都會連不出去。

### CPU throttling

Pod 想用 CPU 但被 limit 擋下的時間比例。超過 25% 通常已經讓他自己變慢。**被節流只影響他自己，不會拖累鄰居**，但可能引發 timeout 和重試，間接放大對外部服務的流量。

### CPU 用量 ÷ Request

超過 100% 代表他正在借用 Node 的閒置 CPU。Node 吃緊時，超用 request 的 Pod 最容易和鄰居互相搶。

### 記憶體 working set 與 OOMKilled

working set 是 Linux 判斷要不要 OOM 的記憶體用量。碰到 memory limit 會被 OOMKilled，只影響他自己的 Pod。

## 限制與沒資料時的處理

| 狀況 | 原因與處理 |
|---|---|
| 資料來源下拉選單是空的 | 沒有名稱符合 `Prometheus.*OCP` 的資料來源（大小寫有區分），修改 datasource 變數的 `regex` |
| PSI 面板沒資料 | Node 核心沒開 PSI，改看替代面板。要開啟需由平台團隊用 MachineConfig 加上 `psi=1` 核心參數，**套用時該 Node pool 會逐台重開機** |
| 【他】【占比】【鄰居】的磁碟寫入沒資料 | 查詢會依序嘗試三種 Pod 層級的磁碟指標（見下方）。三種都沒有，代表叢集沒有收集 Pod 層級的磁碟 IO，只能看第 4 區整台 Node 的磁碟面板，用時間點對照 |
| 所有【整台 Node】面板都沒資料 | 這些查詢假設 node-exporter 的 `instance` 標籤是 Node 名稱（OCP 預設如此）。如果你的環境是 `IP:9100`，要修改查詢 |
| 網路相關面板沒資料 | 網卡名稱不是 `ens / eth / eno / enp / bond` 開頭，修改 `gen_dashboard.py` 的 `NIC` |
| 磁碟相關面板沒資料 | 磁碟名稱不是 `sd* / nvme* / vd* / xvd*`，修改 `gen_dashboard.py` 的 `DEV` |
| 找不到 Deployment 的 Pod | 用 Pod 名稱規則 `<deployment>-<hash>-<id>` 對應，StatefulSet、DaemonSet 不適用 |
| 網路占比超過 100% | 同一台 Node 內 Pod 互傳不經過實體網卡，占比只當量級參考 |
| 【上限】網路面板沒資料 | Node 沒有回報網卡速度，在上方「網卡上限 Mbps」填入實際頻寬 |
| 【上限】磁碟吞吐、IOPS 面板顯示「未設定」 | 在上方填入磁碟規格。只有「整台」沒有「他」，代表叢集沒有收集 Pod 層級的磁碟 IO |
| 磁碟數字對不太上 | 容器層（cAdvisor）與 Node 層的統計方式不同，寫到 PVC 的量也不算在 Node 本機磁碟，只當量級參考 |
| Router 面板沒資料 | 服務不經過 Router，或沒有權限查詢 Router 指標 |

### 確認叢集有哪些 Pod 層級的磁碟指標

在 Grafana Explore 分別執行下面三個查詢，有回傳數字就代表該指標有收集。磁碟寫入面板會依這個順序使用第一個有資料的：

```promql
count(container_fs_writes_bytes_total{container!="", container!="POD"})
```

```promql
count(container_fs_writes_bytes_total{container="", pod!=""})
```

```promql
count(container_blkio_device_usage_total{operation="Write", container="", pod!=""})
```
