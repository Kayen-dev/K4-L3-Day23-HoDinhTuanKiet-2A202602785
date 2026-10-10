"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(rec) + "\n")
    print(json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    return auto or input(f"{msg} [y/N] ").strip().lower() == "y"


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different regions")
    started = time.time()
    observations = []
    for _ in range(3):
        observations.append({region: hc.probe(region, 1.0)[0] for region in (primary, target)})
        time.sleep(0.2)
    primary_down = all(not item[primary] for item in observations)
    step(1, "xac_nhan_outage", primary=primary, target=target,
         observations=observations, confirmed=primary_down)
    if not primary_down:
        return {"ok": False, "error": "primary outage not confirmed"}
    if not confirm(auto, f"Confirm failover {primary} -> {target}?"):
        return {"ok": False, "error": "operator declined"}
    chaos = pathlib.Path("chaos/chaos-events.jsonl")
    kills = [json.loads(line) for line in chaos.read_text().splitlines()
             if line.strip() and json.loads(line).get("action") == "kill"] if chaos.exists() else []
    outage_ts = kills[-1]["ts"] if kills else None
    step(2, "thong_bao_incident", outage_ts=outage_ts, notified_at=time.time(),
         primary=primary, target=target)
    if outage_ts is None:
        return {"ok": False, "error": "missing chaos outage event"}
    # The health checker is the circuit breaker: cutover cannot precede its alert.
    health_log = pathlib.Path("reports/health-events.jsonl")
    detect_deadline = time.monotonic() + 45
    while time.monotonic() < detect_deadline:
        events = [json.loads(line) for line in health_log.read_text().splitlines() if line.strip()] if health_log.exists() else []
        if any(e.get("event") == "state_change" and e.get("region") == primary
               and e.get("to") == "UNHEALTHY" and e["ts"] >= outage_ts for e in events):
            break
        time.sleep(0.5)
    else:
        return {"ok": False, "error": "health checker did not detect outage"}
    result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", ok=result.get("ok", False), result=result)
    if not result.get("ok"):
        return result
    restored = result["state"]
    state_ok = bool(restored["vectors"]["count"] and
                    pathlib.Path(f"state/region-{target}/weights/model.bin").exists())
    step(4, "verify_state_replica", vectors=restored["vectors"]["count"],
         weights=state_ok, embed_model_version=result["restore"]["embed_model_version"])
    step(5, "dns_cutover", target=target, ok=result["cutover"])
    latencies = []
    errors = 0
    for _ in range(10):
        t0 = time.monotonic()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", timeout=3)
            if response.status_code != 200 or response.json().get("region") != target:
                errors += 1
        except (httpx.HTTPError, ValueError):
            errors += 1
        latencies.append((time.monotonic() - t0) * 1000)
    p95 = sorted(latencies)[8]
    step(6, "verify_golden_signals", requests=10, p95_latency_ms=round(p95, 1),
         error_rate=errors / 10)
    elapsed = round(time.time() - started, 2)
    step(7, "post_incident", elapsed_s=elapsed,
         measure_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return {"ok": state_ok and errors == 0, "elapsed_s": elapsed,
            "p95_latency_ms": round(p95, 1), "error_rate": errors / 10,
            "failover": result}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
