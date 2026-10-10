# RTO/RPO evidence — Lab 23

Đo bằng `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`. Thời gian ISO ở đây là UTC. Tất cả mốc tính từ trường `ts` của log; số giây làm tròn 0,1 s như công cụ đo. Hai lần thử dùng Region A thật sự bị tạm dừng tiến trình trên Windows (`NtSuspendProcess`, tương đương SIGSTOP trong bare mode), rồi được tiếp tục sau mỗi drill.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 2026-10-10T06:50:19.160Z | sự kiện kill | `chaos/chaos-events.jsonl:1` |
| Request lỗi đầu tiên bắt đầu sau outage | +2.1s | `ok:false`, seq 16 | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau lỗi | không có | 13 request sau kill đều lỗi; request cuối vẫn lỗi | `reports/drill-1-nodr.jsonl:29` |
| RTO | NO_RECOVERY | `rto_measured_s:null` | `reports/measure-drill-1.json:1` |

Request seq 15 ở `reports/drill-1-nodr.jsonl:16` bắt đầu trước kill nhưng kết thúc bằng lỗi; phép đo chuẩn chỉ lấy request có `ts >= t_outage`.

## 2. Drill 2 — có DR

| Mốc | Giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 0.0s | kill A | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +2.4s | request đầu tiên sau kill có `ok:false` | `reports/drill-2-withdr.jsonl:24` |
| Health checker phát hiện A | +15.4s | `UNHEALTHY`, 3 lỗi liên tiếp, interval 5s | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +16.0s | có model version, RPO và số doc mất | `reports/failover-events.jsonl:2` |
| B ready | +23.2s | `/readyz` trả 200 | `reports/failover-events.jsonl:4` |
| DNS cutover | +23.2s | ghi active region B | `reports/failover-events.jsonl:5` |
| **Request đầu tiên thành công từ B / RTO** | **+25.1s** | `ok:true`, `served_by:b` | `reports/drill-2-withdr.jsonl:33` |

| Chỉ số | Đo được | Mục tiêu | Kết quả / Evidence |
|---|---:|---:|---|
| RTO inference API | 25.1s | 300s | PASS — `reports/measure-drill-2.json:1` |
| RPO vector DB lúc restore | 4.0s / 2 documents | 300s | PASS — `reports/failover-events.jsonl:2` |
| Golden signals sau cutover | p95 563.0 ms, error rate 0/10 | p95 < 1000 ms, error rate 0 | PASS — `reports/runbook-run.jsonl:6` |

`reports/measure-drill-2.json:1` xác nhận `valid:true`, `warnings:[]`, `recovered_by_region:b`. Phiên bản embedding phục hồi là `embed-model=vi-e5-base@v3` (`reports/failover-events.jsonl:2`).

## 3. Thành phần RTO

| Thành phần | Thời gian | Nguồn đo | Cách rút ngắn |
|---|---:|---|---|
| Health-check detection | 15.4s | t_detect − t_outage; `interval_s:5 × threshold:3 = 15s` là detection floor ở `reports/health-events.jsonl:2` | Giảm interval có cân nhắc false alert và tải probe. |
| Verify + snapshot restore | 0.6s | t_restore − t_detect, `reports/health-events.jsonl:2`, `reports/failover-events.jsonl:2` | Duy trì snapshot B gần hơn, nhưng phải kiểm tra version. |
| GPU pool warm-up | 7.2s | t_ready − t_restore; `waited_s:7.19` ở `reports/failover-events.jsonl:4` | Giữ pool dự phòng ấm hơn. |
| DNS/LB TTL và request sampling | 1.9s | t_recovered − t_cutover; `reports/failover-events.jsonl:5`, `reports/drill-2-withdr.jsonl:33` | Giảm TTL nếu chi phí query chấp nhận được. |
| **Tổng RTO** | **25.1s** | tổng bốn khoảng trên | Đối chiếu `reports/measure-drill-2.json:1`. |

Các khoảng làm tròn một chữ số thập phân. RPO 4.0s là chênh giữa timestamp tài liệu mới nhất ở primary và bản restore, không phải tuổi của snapshot; 2 tài liệu bị mất được đếm trực tiếp từ SQLite ở `reports/failover-events.jsonl:2`.
