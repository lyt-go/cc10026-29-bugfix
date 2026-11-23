"""调用审计领域服务：管理API调用的审计记录、归档和查询追溯。

跨模块数据流：
- 消费 usage_billing 快照 → 用量异常触发审计核查
- 消费 subscription 共享记录 → 审批通过的订阅记录审计
- 消费 export_task 快照 → 导出操作记录审计
- 审计记录定期归档 → 写入 data/audit/ 共享目录
- 被 error_classify 消费 → 异常调用触发错误归类
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class CallAuditService(BaseModuleService):
    MODULE = "call_audit"
    ENTITY = "调用记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "调用方", "目标API", "请求参数", "响应码", "响应耗时", "返回行数", "时间戳"]
    REQUIRED_FIELDS = ["记录编号", "调用方", "目标API"]
    STATUS_ORDER = ["已记录", "已校验", "已归档", "已过期"]
    ACTION_RULES = {"确认校验": "已校验", "提交归档": "已归档", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    def record_api_call(self, caller: str, api_name: str, params: str = "",
                        response_code: str = "200", latency_ms: float = 0.0,
                        result_rows: int = 0) -> dict[str, Any]:
        """记录一次API调用到审计日志。"""
        from app.services.data_manager import next_audit_id, write_audit
        from app.store import store as s

        audit_rows = s.rows(self.MODULE)
        new_id = max(
            max((int(r.get("id", 0)) for r in audit_rows), default=0),
            next_audit_id() - 1,
        ) + 1
        record_no = f"CALL-{new_id:04d}"
        call_time = self._now_iso()

        audit_data = {
            "audit_id": new_id,
            "record_no": record_no,
            "caller": caller,
            "api_name": api_name,
            "request_params": params,
            "response_code": response_code,
            "latency_ms": latency_ms,
            "result_rows": result_rows,
            "timestamp": call_time,
        }
        write_audit(new_id, audit_data)

        # 同时记录到内存
        entry = {
            "id": new_id,
            "记录编号": record_no,
            "调用方": caller,
            "目标API": api_name,
            "请求参数": params,
            "响应码": response_code,
            "响应耗时": str(latency_ms),
            "返回行数": str(result_rows),
            "时间戳": call_time,
            "status": "已记录",
            "pending": True,
            "abnormal": response_code != "200",
        }
        audit_rows.append(entry)
        return entry

    def _now_iso(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).isoformat()

    def verify_audits(self, caller: str, api_name: str) -> list[dict[str, Any]]:
        """校验调用方对指定API的审计记录。"""
        from app.services.data_manager import list_audit, read_api_snapshot

        # 检查API是否已注册
        registry = read_api_snapshot()
        api_exists = any(
            isinstance(v, dict) and v.get("api_name") == api_name
            for v in (registry.values() if isinstance(registry, dict) else []))

        if not api_exists:
            return []

        audits = list_audit(api_name=api_name, caller=caller)
        # 校验订阅状态
        from app.services.data_manager import list_subscriptions
        subs = list_subscriptions()
        valid_sub = any(
            s.get("api_name") == api_name and s.get("applicant") == caller and s.get("approval_status") == "已通过"
            for s in subs
        )

        for audit in audits:
            audit["subscription_valid"] = valid_sub
            audit["verified"] = valid_sub
        return audits

    def archive_old_records(self, before_date: str) -> dict[str, Any]:
        """归档指定日期之前的审计记录。"""
        from app.services.data_manager import archive_audit_records
        return archive_audit_records(before_date)

    def backfill_audit_record(self, record_no: str, caller: str, api_name: str,
                              params: str = "", response_code: str = "200",
                              latency_ms: float = 0.0, result_rows: int = 0,
                              call_time: str = "", logged_at: str = "") -> dict[str, Any]:
        """补录一条迟到的真实调用明细。

        调用时间与原日志写入时间取自补录数据本身，不被补录动作改写；
        补录后落在原归档日期内，再次归档即可归位到同一归档。
        """
        from app.services.data_manager import list_audit, next_audit_id, write_audit
        from app.store import store as s

        audit_rows = s.rows(self.MODULE)
        new_id = max(
            max((int(r.get("id", 0)) for r in audit_rows), default=0),
            next_audit_id() - 1,
        ) + 1
        # 记录编号沿用补录方口径；缺省按编号规则生成，避免覆盖既有调用
        resolved_no = record_no or f"CALL-{new_id:04d}"
        existing_nos = {str(r.get("record_no") or r.get("记录编号") or "")
                        for r in list_audit()}
        if resolved_no in existing_nos:
            raise ValueError(f"记录编号 {resolved_no} 已存在，不能重复补录")

        audit_data = {
            "audit_id": new_id,
            "record_no": resolved_no,
            "caller": caller,
            "api_name": api_name,
            "request_params": params,
            "response_code": response_code,
            "latency_ms": latency_ms,
            "result_rows": result_rows,
            "timestamp": call_time or logged_at or self._now_iso(),
        }
        write_audit(new_id, audit_data, logged_at=logged_at or call_time or None)

        entry = {
            "id": new_id,
            "记录编号": resolved_no,
            "调用方": caller,
            "目标API": api_name,
            "请求参数": params,
            "响应码": response_code,
            "响应耗时": str(latency_ms),
            "返回行数": str(result_rows),
            "时间戳": audit_data["timestamp"],
            "status": "已记录",
            "pending": True,
            "abnormal": response_code != "200",
        }
        s.rows(self.MODULE).append(entry)
        return entry

    def query_archived_records(self, start_date: str | None = None,
                               end_date: str | None = None) -> list[dict[str, Any]]:
        """按日期从审计归档处复核历史真实调用明细。"""
        from app.services.data_manager import query_audit_archive
        return query_audit_archive(start_date=start_date, end_date=end_date)

    def get_archive(self, before_date: str) -> dict[str, Any]:
        """按归档日期读取审计归档交付物。"""
        from app.services.data_manager import read_audit_archive
        return read_audit_archive(before_date)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "提交归档":
            self.archive_old_records(self._today_str())

        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def _today_str(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        audit_list = []
        for row in rows:
            audit_list.append({
                "id": row.get("id"),
                "record_no": row.get("记录编号"),
                "caller": row.get("调用方"),
                "api": row.get("目标API"),
                "params": row.get("请求参数"),
                "response_code": row.get("响应码"),
                "latency": row.get("响应耗时"),
                "result_rows": row.get("返回行数"),
                "timestamp": row.get("时间戳"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(audit_list),
            "audits": audit_list,
            "archived_count": sum(1 for r in audit_list if r.get("status") == "已归档"),
            "abnormal_count": sum(1 for r in rows if r.get("abnormal")),
        }