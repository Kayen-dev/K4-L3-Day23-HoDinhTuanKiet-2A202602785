# Postmortem — diễn tập failover Region A → B

Thời gian bên dưới là UTC ngày 2026-10-10. Đây là outage giả lập bằng tạm dừng tiến trình A; mục tiêu là đánh giá thiết kế và quy trình, không quy lỗi cá nhân.

## 1. Timeline

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 06:51:11.658Z | Outage bắt đầu, A bị tạm dừng | `chaos/chaos-events.jsonl:3` |
| 06:51:14.032Z | Request đầu tiên bắt đầu sau outage trả lỗi | `reports/drill-2-withdr.jsonl:24` |
| 06:51:20.562Z | Runbook ghi nhận incident; chế độ `--auto` dùng riêng cho drill | `reports/runbook-run.jsonl:2` |
| 06:51:27.048Z | Health checker báo A `UNHEALTHY` sau 3 probe hỏng liên tiếp | `reports/health-events.jsonl:2` |
| 06:51:27.693Z | Restore snapshot B, đo RPO 4.0s và 2 documents | `reports/failover-events.jsonl:2` |
| 06:51:34.879Z | B ready sau warm-up | `reports/failover-events.jsonl:4` |
| 06:51:34.880Z | DNS/LB cutover sang B | `reports/failover-events.jsonl:5` |
| 06:51:36.725Z | Request đầu tiên được B phục vụ thành công | `reports/drill-2-withdr.jsonl:33` |

## 2. RTO/RPO và gap analysis

- **RTO:** mục tiêu 300s, đo 25.1s, còn dư 274.9s (`reports/measure-drill-2.json:1`). Baseline không DR là `NO_RECOVERY`, 13 request sau kill thất bại (`reports/measure-drill-1.json:1`).
- **RPO:** mục tiêu 300s, đo 4.0s và 2 documents không có trong bản restore; còn dư 296.0s (`reports/failover-events.jsonl:2`). Đây là mất mát dữ liệu thực tế tại thời điểm restore, không suy từ chu kỳ snapshot.
- **Gap lớn nhất trong RTO:** phát hiện lỗi 15.4s, chiếm 61.4% RTO, do interval 5s và threshold 3 (`reports/health-events.jsonl:2`). Snapshot/verify 0.6s; warm-up 7.2s; TTL và chờ request thành công 1.9s. Tổng 25.1s (`reports/rto-evidence.md:32`).

## 3. Root cause — 5 whys

1. Vì sao user nhận lỗi? Edge tiếp tục định tuyến tới A sau khi A ngừng trả lời.
2. Vì sao edge chưa đổi ngay? Edge chỉ đọc `active_region`; nó không tự đánh giá readiness.
3. Vì sao không cutover ngay khi một probe lỗi? Threshold 3 ngăn flapping do lỗi thoáng qua.
4. Vì sao khôi phục còn tốn thời gian? B ban đầu không có weights, vector DB, pool `full`; phải restore và warm-up trước cutover (`reports/failover-events.jsonl:1`, `reports/failover-events.jsonl:4`).
5. Vì sao mất 2 documents? Snapshot chạy theo chu kỳ 30s nên các write sau snapshot mới nhất chưa được sao chép (`reports/replication.jsonl:2`, `reports/failover-events.jsonl:2`).

Nếu đây là outage thật, rủi ro vận hành chính là thiếu snapshot hợp lệ hoặc model version không tương thích; bước `2_restore_snapshot` sẽ abort trước cutover. Runbook cần on-call kiểm tra manifest và người chỉ huy incident quyết định hướng khôi phục.

## 4. Action items

| # | Action item | Owner | Deadline | Tác động kỳ vọng |
|---|---|---|---|---|
| 1 | Thử interval 3s, threshold 3 qua thêm các game day có lỗi mạng thoáng qua; giữ điều kiện chống flapping. | SRE on-call | 2026-10-17 | Detection floor giảm từ 15s xuống 9s, tiết kiệm tối đa khoảng 6s RTO nếu false alert chấp nhận được. |
| 2 | Đánh giá snapshot mỗi 10s và kiểm thử restore kèm model version, đồng thời đo chi phí I/O. | Data platform | 2026-10-24 | Giới hạn lag kỳ vọng giảm từ khoảng 30s xuống khoảng 10s; số documents mất phụ thuộc tốc độ ingest. |

## 5. Câu hỏi phản tư

1. `interval × threshold = 5 × 3 = 15s`, là detection floor; detection thực đo 15.4s, chiếm 61.4% RTO 25.1s. Với threshold 3 và phần còn lại 9.7s giữ nguyên, interval lý thuyết tối đa khoảng `(300−9.7)/3 = 96.8s`; phải dự phòng thêm timeout, jitter và thao tác người vận hành.
2. Nếu interval 1s, floor lý thuyết từ 15s còn 3s, có thể tiết kiệm tối đa khoảng 12s. Tuy nhiên probe timeout 2s và poll tuần tự có thể làm chu kỳ thực dài hơn 1s; số probe tăng 5 lần và các lỗi mạng thoáng qua dễ tạo báo động giả hơn. Cần đo lại trước khi đổi cấu hình.
3. Nếu primary mất dữ liệu vĩnh viễn, `docs_lost=2` nghĩa là hai tài liệu đã được nhận tại A nhưng không có trong bản restore của B. Khách hàng có thể phải gửi lại hai write đó hoặc cần replay từ log nguồn; đây là tổn thất dữ liệu cụ thể, không chỉ là 4.0s gián đoạn.
