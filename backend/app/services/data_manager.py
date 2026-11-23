"""跨模块共享数据管理器：读写 data/ 目录下的业务数据产物。

职责：
- 每个服务产出 domain_snapshot 后序列化到 data/{module}/snapshot.json
- 下游服务从 data/ 目录读取上游的标准化数据产物
- 审计记录写入 data/audit/
- 用量记录写入 data/usage/
- 导出任务结果写入 data/exports/
- API 注册快照写入 data/api/
- 订阅审批记录写入 data/subscriptions/
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 数据目录相对于仓库根
DATA_ROOT = Path(os.environ.get("DATA_ROOT", str(
    Path(__file__).resolve().parents[3] / "data"
)))

# 东八区
TZ = timezone(timedelta(hours=8))


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


# ========== API 注册快照 ==========


def write_api_snapshot(api_id: int, api_data: dict[str, Any]) -> None:
    """将单个 API 注册数据写入共享快照，供路由、缓存、限流等模块消费。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    key = str(api_id)
    api_data["_synced_at"] = _now_iso()
    snapshot[key] = api_data
    _write_json(DATA_ROOT / "api" / "registry.json", snapshot)


def read_api_snapshot(api_id: int | None = None) -> dict[str, Any] | dict[str, dict[str, Any]]:
    """读取 API 注册快照。返回单个或全部。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    if api_id is not None:
        return snapshot.get(str(api_id), {})
    return snapshot


def remove_api_snapshot(api_id: int) -> None:
    """标记 API 为下线。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    key = str(api_id)
    if key in snapshot:
        snapshot[key]["status"] = "已下线"
        snapshot[key]["_deleted_at"] = _now_iso()
        _write_json(DATA_ROOT / "api" / "registry.json", snapshot)


# ========== 订阅审批 ==========


def write_subscription(sub_id: int, sub_data: dict[str, Any]) -> None:
    """写入订阅申请记录到共享目录。"""
    record = {
        "subscription_id": sub_id,
        **sub_data,
        "_created_at": sub_data.get("_created_at", _now_iso()),
        "_updated_at": _now_iso(),
    }
    _write_json(DATA_ROOT / "subscriptions" / f"{sub_id:06d}.json", record)


def read_subscription(sub_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "subscriptions" / f"{sub_id:06d}.json", {})


def list_subscriptions() -> list[dict[str, Any]]:
    """列出所有订阅记录。"""
    _ensure_dir(DATA_ROOT / "subscriptions")
    records = []
    for f in sorted(DATA_ROOT.glob("subscriptions/*.json")):
        record = _read_json(f)
        if record:
            records.append(record)
    return records


def approve_subscription(sub_id: int, approved: bool, approver: str = "system") -> dict[str, Any]:
    """审批订阅，记录审批流程。"""
    record = read_subscription(sub_id)
    if not record:
        return {}
    record["approval_status"] = "已通过" if approved else "已驳回"
    record["approver"] = approver
    record["_approved_at"] = _now_iso()
    write_subscription(sub_id, record)
    return record


# ========== 用量计费 ==========


def write_usage(usage_id: int, usage_data: dict[str, Any]) -> None:
    """写入用量记录到共享目录。"""
    usage_data["_created_at"] = usage_data.get("_created_at", _now_iso())
    usage_data["_updated_at"] = _now_iso()
    _write_json(DATA_ROOT / "usage" / f"{usage_id:06d}.json", usage_data)


def read_usage(usage_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "usage" / f"{usage_id:06d}.json", {})


def list_usage(billing_cycle: str | None = None) -> list[dict[str, Any]]:
    """列出用量记录，支持按计费周期过滤。"""
    _ensure_dir(DATA_ROOT / "usage")
    records = []
    for f in sorted(DATA_ROOT.glob("usage/*.json")):
        record = _read_json(f)
        if record:
            if billing_cycle is None or record.get("billing_cycle") == billing_cycle:
                records.append(record)
    return records


def settle_billing(billing_cycle: str, use_auto_settlement: bool = True) -> dict[str, Any]:
    """结算指定计费周期，生成结算汇总。"""
    records = list_usage(billing_cycle)
    total_amount = sum(float(r.get("amount", 0)) for r in records)
    total_calls = sum(int(r.get("call_count", 0)) for r in records)
    settlement = {
        "billing_cycle": billing_cycle,
        "total_amount": total_amount,
        "total_calls": total_calls,
        "record_count": len(records),
        "settled_at": _now_iso(),
        "auto_settlement": use_auto_settlement,
    }
    _write_json(DATA_ROOT / "usage" / f"_settlement_{billing_cycle}.json", settlement)

    for r in records:
        r["settlement_status"] = "已结算"
        r["_settled_at"] = _now_iso()
        write_usage(r.get("subscription_id", 0), r)
    return settlement


# ========== 调用审计 ==========

# 真实调用明细文件：data/audit/000001.json
_AUDIT_RECORD_NAME = re.compile(r"^\d+$")
# 归档交付物文件：data/audit/_archive_2026-09-01.json
_ARCHIVE_NAME = re.compile(r"^_archive_(.+)$")


def _audit_record_files() -> list[Path]:
    """真实调用明细文件，排除归档交付物等非调用记录。"""
    _ensure_dir(DATA_ROOT / "audit")
    return [
        f for f in sorted(DATA_ROOT.glob("audit/*.json"))
        if _AUDIT_RECORD_NAME.match(f.stem)
    ]


def _audit_archive_files() -> list[Path]:
    """审计归档交付物文件。"""
    _ensure_dir(DATA_ROOT / "audit")
    return [
        f for f in sorted(DATA_ROOT.glob("audit/_archive_*.json"))
        if _ARCHIVE_NAME.match(f.stem)
    ]


def _audit_call_time(record: dict[str, Any]) -> str:
    """调用记录的真实调用时间；缺失时退回原日志写入时间。"""
    return str(record.get("timestamp") or record.get("_logged_at") or "")


def write_audit(audit_id: int, audit_data: dict[str, Any],
                logged_at: str | None = None) -> None:
    """写入审计记录。

    保留原始日志写入时间：已带 _logged_at 的记录不会被重写时间；
    只有新产生的记录才打上当前时间，补录历史调用可显式传入 logged_at。
    """
    if logged_at:
        audit_data["_logged_at"] = logged_at
    elif not audit_data.get("_logged_at"):
        audit_data["_logged_at"] = _now_iso()
    _write_json(DATA_ROOT / "audit" / f"{int(audit_id):06d}.json", audit_data)


def read_audit(audit_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "audit" / f"{int(audit_id):06d}.json", {})


def next_audit_id() -> int:
    """下一个可用的真实调用明细编号（不含归档交付物）。"""
    return max(
        (int(f.stem) for f in _audit_record_files()), default=0) + 1


def list_audit(api_name: str | None = None, caller: str | None = None,
               start_time: str | None = None, end_time: str | None = None) -> list[dict[str, Any]]:
    """按条件查询审计记录（仅真实调用明细，不含归档交付物）。"""
    records = []
    for f in _audit_record_files():
        record = _read_json(f)
        if not record:
            continue
        # 归档交付物曾被误写成 000000.json，这类无真实 audit_id 的伪调用不纳入
        if int(record.get("audit_id", 0) or 0) <= 0:
            continue
        if api_name and record.get("api_name") != api_name:
            continue
        if caller and record.get("caller") != caller:
            continue
        if start_time and record.get("_logged_at", "") < start_time:
            continue
        if end_time and record.get("_logged_at", "") > end_time:
            continue
        records.append(record)
    return records


def list_audit_archives() -> list[dict[str, Any]]:
    """列出所有审计归档交付物。"""
    archives = []
    for f in _audit_archive_files():
        archive = _read_json(f)
        if archive:
            archives.append(archive)
    return archives


def read_audit_archive(before_date: str) -> dict[str, Any]:
    """按归档日期读取审计归档交付物。"""
    return _read_json(DATA_ROOT / "audit" / f"_archive_{before_date}.json", {})


def query_audit_archive(start_date: str | None = None,
                        end_date: str | None = None) -> list[dict[str, Any]]:
    """按日期范围调取历史归档中的真实调用明细。

    从归档交付物里还原 archival 记录，并回挂原始来源信息，
    使调用审计能关联到原请求及后续新增明细。
    """
    merged: dict[str, dict[str, Any]] = {}
    for archive in list_audit_archives():
        archive_date = str(archive.get("before_date", ""))
        if start_date and archive_date < start_date:
            continue
        if end_date and archive_date > end_date:
            continue
        for record in archive.get("records", []):
            source = dict(record)
            source["_archive_before_date"] = archive_date
            source["_archive_file"] = f"_archive_{archive_date}.json"
            record_no = str(source.get("record_no") or source.get("audit_id") or "")
            # 同一真实调用多次归档时以最新归档内容为准，但不重复计数
            merged[record_no] = source
    return list(merged.values())


def archive_audit_records(before_date: str) -> dict[str, Any]:
    """归档指定日期之前的审计记录。

    只从原始调用明细（data/audit/NNNNNN.json）取数，归档交付物不参与归档；
    归档结果按归档日期合并写入，重复执行不会把历史包当成调用、不会覆写
    原归档，也不会改写调用时间或原日志写入时间。

    迟到明细：再次归档时新出现的真实调用追加进同一归档，并记为迟到记录。
    对账信息：归档同时给出迟到、原始日志丢失、归档内容错位三类清单，
    便于区分哪些是迟到调用、哪些是归档错位或原始日志缺失。
    """
    archive_path = DATA_ROOT / "audit" / f"_archive_{before_date}.json"
    existing = _read_json(archive_path, {}) or {}
    is_initial = not existing

    previous: dict[str, dict[str, Any]] = {}
    for record in existing.get("records", []):
        key = str(record.get("record_no") or record.get("audit_id") or "")
        carried = dict(record)
        carried.setdefault("_archived_from", "audit_archive")
        previous[key] = carried

    # 原始调用明细（唯一真实来源），按日期边界拆分为本轮归档范围内外
    all_details: dict[str, dict[str, Any]] = {}
    for record in list_audit():
        key = str(record.get("record_no") or record.get("audit_id") or "")
        all_details[key] = record
    candidates = {
        key: record for key, record in all_details.items()
        if _audit_call_time(record)[:10]
        and _audit_call_time(record)[:10] < before_date
    }

    # 对账：把上一版归档与当前原始明细逐条比对，区分三类差异
    missing_record_nos: list[str] = []
    misplaced_record_nos: list[str] = []
    for key, archived_record in previous.items():
        source = all_details.get(key)
        if source is None:
            # 归档里有、原始调用明细已不存在：原始日志丢失
            missing_record_nos.append(key)
            continue
        source_time = _audit_call_time(source)[:10]
        archived_time = _audit_call_time(archived_record)[:10]
        if source_time and source_time >= before_date:
            # 归档内容落在错误的归档日期，属归档内容错位
            misplaced_record_nos.append(key)
        elif archived_time and archived_time != source_time:
            # 归档保存的调用时间与原始明细不一致，属归档内容错位
            misplaced_record_nos.append(key)

    late_record_nos: list[str] = list(existing.get("late_record_nos", []))
    for key, record in candidates.items():
        snapshot = dict(record)
        snapshot["status"] = "已归档"
        snapshot["_source_file"] = f"{int(record.get('audit_id', 0)):06d}.json"
        snapshot["_archived_from"] = "call_detail"
        if key in previous:
            snapshot["_archived_from"] = previous[key].get("_archived_from", "call_detail")
        elif not is_initial and key not in late_record_nos:
            # 首轮之后才出现、且确属归档日期内的真实调用：记为迟到明细
            late_record_nos.append(key)
        previous[key] = snapshot

    # 原始明细已丢失的历史归档记录原样保留，保证归档数据可复核
    archived = list(previous.values())

    archive_data = {
        "archive_date": existing.get("archive_date") or _now_iso(),
        "before_date": before_date,
        "archived_count": len(archived),
        "late_record_nos": late_record_nos,
        "missing_record_nos": missing_record_nos,
        "misplaced_record_nos": misplaced_record_nos,
        "reconciliation": {
            "late_count": len(late_record_nos),
            "missing_count": len(missing_record_nos),
            "misplaced_count": len(misplaced_record_nos),
        },
        "records": archived,
    }
    # 无新增真实调用时保持原归档引用不变（含 archive_date），保证可复核一致
    previous_payload = {k: v for k, v in existing.items() if k != "updated_at"}
    if archive_data != previous_payload:
        archive_data["updated_at"] = _now_iso()
        _write_json(archive_path, archive_data)
    else:
        archive_data = existing

    # 标记原始调用明细为已归档；沿用原 _logged_at，不重写原始时间
    for record in candidates.values():
        rid = record.get("audit_id", 0)
        record["status"] = "已归档"
        write_audit(rid, record, logged_at=record.get("_logged_at"))

    return archive_data


# ========== 导出任务 ==========


def write_export(export_id: int, export_data: dict[str, Any]) -> None:
    """写入导出任务结果。"""
    export_data["_created_at"] = export_data.get("_created_at", _now_iso())
    export_data["_updated_at"] = _now_iso()
    _write_json(DATA_ROOT / "exports" / f"{export_id:06d}.json", export_data)
    # 同时生成一份导出 CSV
    _write_export_csv(export_id, export_data)


def _write_export_csv(export_id: int, export_data: dict[str, Any]) -> None:
    """根据导出数据生成 CSV 文件头。"""
    columns = export_data.get("columns", [])
    rows = export_data.get("rows", [])
    csv_path = DATA_ROOT / "exports" / f"{export_id:06d}.csv"
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    csv_path.write_text("\n".join(lines), encoding="utf-8")


def read_export(export_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "exports" / f"{export_id:06d}.json", {})


def list_exports() -> list[dict[str, Any]]:
    _ensure_dir(DATA_ROOT / "exports")
    records = []
    for f in sorted(DATA_ROOT.glob("exports/[0-9]*.json")):
        record = _read_json(f)
        if record:
            records.append(record)
    return records


# ========== 模块数据快照 ==========


def write_module_snapshot(module: str, snapshot: dict[str, Any]) -> None:
    """各服务将自己的 domain_snapshot 写入共享目录，供跨模块消费。"""
    snapshot["_snapshot_at"] = _now_iso()
    _write_json(DATA_ROOT / module / "snapshot.json", snapshot)


def read_module_snapshot(module: str) -> dict[str, Any]:
    return _read_json(DATA_ROOT / module / "snapshot.json", {})


def list_module_snapshots() -> list[dict[str, Any]]:
    """读取所有模块的快照汇总。"""
    snapshots = []
    for module_dir in DATA_ROOT.iterdir():
        if module_dir.is_dir() and not module_dir.name.startswith("_"):
            snap = _read_json(module_dir / "snapshot.json")
            if snap:
                snapshots.append(snap)
    return snapshots