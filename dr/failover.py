"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(rec) + "\n")
    print(json.dumps(rec))
    return rec


def state_of(region: str) -> dict:
    return httpx.get(f"{URL[region]}/v1/state", timeout=2).json()


def failover(target: str, backend: str, wait: float) -> dict:
    if target not in URL or wait < 0:
        raise ValueError("invalid target or wait")
    active = pathlib.Path("edge/active_region")
    if active.exists() and active.read_text().strip() == target:
        return {"ok": False, "error": "target already active"}
    try:
        before = state_of(target)
        emit(step="1_verify_target", target=target, state=before)
        meta = snapshot.get(target, backend)
        rpo = snapshot.rpo(pathlib.Path(f"state/region-{'b' if target == 'a' else 'a'}/vectors.sqlite"),
                           pathlib.Path(f"state/region-{target}/vectors.sqlite"))
        emit(step="2_restore_snapshot", target=target, **meta,
             rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"])
        pool = pathlib.Path(f"state/region-{target}/pool_state")
        pool.write_text("full")
        emit(step="3_scale_pool", target=target, pool_state="full")
        deadline = time.monotonic() + wait
        began = time.monotonic()
        reason = "not ready"
        while True:
            try:
                response = httpx.get(f"{URL[target]}/readyz", timeout=2)
                if response.status_code == 200:
                    ready = response.json()
                    emit(step="4_wait_ready", target=target,
                         waited_s=round(time.monotonic() - began, 2), ready=ready)
                    break
                reason = str(response.json().get("reasons", response.status_code))
            except (httpx.HTTPError, ValueError) as exc:
                reason = type(exc).__name__
            if time.monotonic() >= deadline:
                emit(step="4_wait_ready", target=target, ok=False, reason=reason,
                     waited_s=round(time.monotonic() - began, 2))
                return {"ok": False, "error": f"target not ready: {reason}"}
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        active.write_text(target)
        emit(step="5_dns_cutover", target=target, ok=True)
        return {"ok": True, "target": target, "state": ready,
                "restore": meta, "rpo": rpo, "cutover": True}
    except (OSError, httpx.HTTPError, ValueError, SystemExit) as exc:
        emit(step="abort", target=target, reason=str(exc))
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
