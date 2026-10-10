# Runbook: Region A mất khả năng phục vụ

Chạy các lệnh từ thư mục gốc repo. On-call giữ quyền xác nhận failover; incident commander (IC) giữ quyền rollback. Region B phải còn sống và có snapshot trước khi chuyển traffic. Dùng `/readyz` để xác nhận khả năng phục vụ.

| # | Bước / owner | Lệnh có thể chạy | Tín hiệu hoàn tất |
|---|---|---|---|
| 1 | Xác nhận outage / on-call | `python -c "from dr.health_checker import probe; print([probe('a',1) for _ in range(3)]); print('b:', probe('b',1))"` | A lỗi 3 lần liên tiếp; B vẫn phản hồi. Đối chiếu `reports/health-events.jsonl` có `UNHEALTHY` của A. |
| 2 | Mở incident, bấm đồng hồ / on-call | `python -c "import json; print([json.loads(x) for x in open('chaos/chaos-events.jsonl') if 'kill' in x][-1])"` | Ghi thời điểm outage từ `ts` vào incident; lúc chạy bước 3, `reports/runbook-run.jsonl` ghi mốc thông báo. |
| 3 | Restore snapshot và scale pool / on-call | `python dr/runbook.py --primary a --target b --backend fs` | Xác nhận `y` một lần; các bước `1_verify_target`, `2_restore_snapshot`, `3_scale_pool`, `4_wait_ready`, `5_dns_cutover` xuất hiện đúng thứ tự trong `reports/failover-events.jsonl`. Chỉ dùng `--auto` khi diễn tập/CI. |
| 4 | Verify state replica / on-call | `python -c "import httpx; print(httpx.get('http://127.0.0.1:8002/v1/state').json())"` | `count > 0`, `weights: true`, pool `full`; `2_restore_snapshot` ghi `embed_model_version`, `rpo_seconds`, `docs_lost`. |
| 5 | Verify DNS/LB cutover / on-call | `python -c "import httpx; print(httpx.get('http://127.0.0.1:8080/edge/state').json())"` | Sau TTL tối đa 5 giây, `active_region: b`; `5_dns_cutover` có `ok: true`. |
| 6 | Verify golden signals / on-call | `python -c "import json; print([json.loads(x) for x in open('reports/runbook-run.jsonl') if 'verify_golden_signals' in x][-1])"` | 10 request thật tới B, `error_rate = 0`, p95 < 1000 ms; kiểm tra thêm edge bằng `python -c "import httpx; print(httpx.get('http://127.0.0.1:8080/v1/infer').json())"`. |
| 7 | Đo RTO, đóng incident, postmortem / IC | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid: true`, `warnings: []`, `rto_verdict: PASS`; lưu RTO/RPO và các dòng bằng chứng trong `reports/rto-evidence.md`, `reports/postmortem.md`. |

**Abort:** Nếu B không trả 200 ở `/readyz` trước 60 giây, `failover.py` trả `ok: false` và giữ DNS ở A. IC quyết định khôi phục A hoặc xử lý B; không ép cutover.

**Rollback về A:** Chỉ IC được phê duyệt khi A đã ổn định, `/readyz` trả 200 liên tục và state A đã được đối chiếu với B. Nếu B lỗi liên tục sau cutover, IC yêu cầu on-call đóng băng write, chụp snapshot mới bằng `python state/snapshot.py put --region b --backend fs`, xác nhận manifest và model version rồi mới chạy `python dr/runbook.py --primary b --target a --backend fs` theo cùng kiểm soát. Nếu không thể chụp snapshot B, IC phải chọn cách khôi phục dữ liệu khác và ghi rõ RPO; không tự động chuyển qua lại. Ghi thời gian và lý do vào incident.
