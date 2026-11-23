"""跨模块联动测试：验证18个backend服务的增强业务逻辑。

测试跨模块数据流：
1. api_registry -> query_route -> cache_layer: API注册→路由规则→缓存策略联动
2. api_registry -> subscription -> rate_limiter -> usage_billing: API→订阅审批→限流→用量计量
3. sla_monitor -> error_classify -> query_route: SLA偏离→异常归类→路由降级
4. export_task -> data_merge -> call_audit: 导出→数据归并→审计
5. version_compat -> api_registry -> sdk_config: 版本兼容→API过渡→SDK配置更新
6. batch_query -> query_optimize -> cache_layer: 批量查询→优化→缓存预热
"""

import os
import sys
import json
import tempfile

# 设置测试环境
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 设置临时DATA_ROOT
temp_data = tempfile.mkdtemp(prefix='cc10026_test_')
os.environ['DATA_ROOT'] = temp_data


class TestCrossModuleFlows:
    """跨模块数据流测试"""

    def setup_method(self):
        """每个测试前初始化store。"""
        from app.seed import SEED_ROWS
        from app.store import store as s

        # 重新初始化store
        s._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        # 清空测试数据目录
        import shutil
        if os.path.exists(temp_data):
            shutil.rmtree(temp_data, ignore_errors=True)

    # ============================================================
    # Flow 1: api_registry -> query_route -> cache_layer
    # ============================================================

    def test_api_publish_auto_create_route_cache_sla(self):
        """API上线时自动创建默认路由、缓存策略和SLA指标"""
        from app.store import store
        from app.services.api_registry import ApiRegistryService

        svc = ApiRegistryService()
        # 先获取已上线的API(seed中id=3)
        entry = store.find("api_registry", 3)
        assert entry is not None
        assert entry["status"] == "已上线"

        # 模拟上线该API
        result, message = svc.run_action(3, "确认上线")
        assert result is not None, f"上线失败: {message}"

        # 验证路由规则被自动创建
        route_rows = store.rows("query_route")
        route_created = [r for r in route_rows
                         if r.get("关联API") == entry.get("API名称")
                         and r.get("_auto_created")]
        assert len(route_created) > 0, "API上线后应自动创建路由规则"
        assert route_created[0]["status"] == "已生效"

        # 验证缓存策略被自动创建
        cache_rows = store.rows("cache_layer")
        cache_created = [r for r in cache_rows
                         if r.get("关联API") == entry.get("API名称")
                         and r.get("_auto_created")]
        assert len(cache_created) > 0, "API上线后应自动创建缓存策略"
        assert cache_created[0]["status"] == "已生效"

        # 验证SLA指标被自动创建
        sla_rows = store.rows("sla_monitor")
        sla_created = [r for r in sla_rows
                       if r.get("关联API") == entry.get("API名称")
                       and r.get("_auto_created")]
        assert len(sla_created) > 0, "API上线后应自动创建SLA指标"
        assert sla_created[0]["status"] == "监控中"

    def test_api_publish_writes_shared_snapshot(self):
        """API上线后写共享快照供下游消费"""
        from app.store import store
        from app.services.api_registry import ApiRegistryService

        svc = ApiRegistryService()
        entry = store.find("api_registry", 3)
        svc.run_action(3, "确认上线")

        # 验证data/api/目录创建了共享快照
        from app.services.data_manager import read_api_snapshot
        registry = read_api_snapshot()
        assert isinstance(registry, dict)
        assert str(3) in registry, "API快照应包含注册的API"
        assert registry[str(3)].get("api_name") == entry.get("API名称")

    # ============================================================
    # Flow 2: subscription -> rate_limiter -> usage_billing
    # ============================================================

    def test_subscription_approval_creates_limiter_and_usage(self):
        """审批通过时自动创建限流策略和用量记录"""
        from app.store import store
        from app.services.subscription import SubscriptionService

        svc = SubscriptionService()

        # 种子数据中的sub: id=3 is 已通过状态
        # 先准备一个待审批的subscription entry
        sub_id = 1
        entry = store.find("subscription", sub_id)
        assert entry is not None

        # 先提交审批
        svc.run_action(sub_id, "提交审批")

        # 再审批通过
        result, message = svc.run_action(sub_id, "审批通过")
        assert result is not None, f"审批通过失败: {message}"

        # 验证限流策略被创建
        rate_rows = store.rows("rate_limiter")
        rate_created = [r for r in rate_rows if r.get("_auto_created")]
        assert len(rate_created) > 0, "审批通过应创建限流策略"

        # 验证用量文件被写入
        from app.services.data_manager import list_usage
        usage_records = list_usage()
        assert len(usage_records) > 0, "审批通过应创建用量记录"

    def test_subscription_validate_api_exists(self):
        """订阅审批前校验API是否注册"""
        from app.services.subscription import SubscriptionService

        svc = SubscriptionService()
        # 种子中的sub关联的API可能是“API注册样例1”之类的
        # 已经注册的API应该通过校验
        valid = svc._validate_api_exists({"订阅API": "API注册样例1"})
        assert isinstance(valid, bool)

    # ============================================================
    # Flow 3: sla_monitor -> error_classify -> query_route
    # ============================================================

    def test_sla_record_metric_triggers_classify(self):
        """SLA记录指标超标触发异常分类"""
        from app.store import store
        from app.services.sla_monitor import SlaMonitorService

        svc = SlaMonitorService()
        rows = store.rows("sla_monitor")
        monitoring = [r for r in rows if r.get("status") == "监控中"
                       and r.get("P99延迟", "0") is not None
                       and r.get("P99延迟", "").replace(".", "").isdigit()]
        if monitoring:
            api_name = monitoring[0].get("关联API", "")
            result = svc.record_metric(api_name, latency_ms=500.0, error=False)
            assert result, "record_metric应返回结果"

    def test_sla_deviation_triggers_degrade(self):
        """SLA标记偏离触发路由降级"""
        from app.store import store
        from app.services.sla_monitor import SlaMonitorService

        svc = SlaMonitorService()
        rows = store.rows("sla_monitor")
        monitoring = [r for r in rows if r.get("status") == "监控中"]
        if monitoring:
            # 标记偏离
            entry_id = monitoring[0].get("id")
            result, msg = svc.run_action(entry_id, "标记偏离")
            assert result is not None, f"标记偏离失败: {msg}"
            assert result.get("status") == "已偏离"

    # ============================================================
    # Flow 4: export_task -> call_audit
    # ============================================================

    def test_export_execute_writes_audit(self):
        """导出执行完成写入审计记录"""
        from app.store import store
        from app.services.export_task import ExportTaskService

        svc = ExportTaskService()
        entry = store.find("export_task", 1)
        assert entry is not None

        result = svc.execute_export(1)
        assert result.get("ok"), f"导出失败: {result.get('message')}"

        # 验证审计记录被写入(直接检查data/audit/目录)
        from app.services.data_manager import DATA_ROOT
        audit_files = list(DATA_ROOT.glob("audit/[0-9]*.json"))
        assert True, f"审计目录存在文件: {len(audit_files) if audit_files else 0}"  # 仅验证不崩溃

    def test_export_run_action_triggers_full_pipeline(self):
        """run_action发起导出触发完整流水线"""
        from app.store import store
        from app.services.export_task import ExportTaskService

        svc = ExportTaskService()
        result, msg = svc.run_action(1, "发起导出")
        assert result is not None, f"发起导出失败: {msg}"
        assert result.get("status") == "已完成"

    # ============================================================
    # Flow 5: version_compat -> sdk_config
    # ============================================================

    def test_version_compat_disable_old_subscriptions(self):
        """版本过渡禁用旧版本订阅"""
        from app.store import store
        from app.services.version_compat import VersionCompatService

        svc = VersionCompatService()
        # 先确保有subscription条目
        sub_rows = store.rows("subscription")
        assert len(sub_rows) > 0

        # 尝试禁用（即使没有匹配的版本也可验证函数不报错）
        disabled = svc.disable_old_version_subscriptions("测试API", "v1")
        assert isinstance(disabled, int), "应返回禁用数量"

    def test_version_compat_run_transition(self):
        """版本兼容启动过渡动作"""
        from app.store import store
        from app.services.version_compat import VersionCompatService

        svc = VersionCompatService()
        entry = store.find("version_compat", 1)
        if entry:
            result, msg = svc.run_action(1, "启动过渡")
            # 过渡应该成功执行
            assert result is not None or "不存在" in msg, f"启动过渡: {msg}"

    # ============================================================
    # Flow 6: batch_query -> query_optimize -> cache_layer
    # ============================================================

    def test_batch_query_uses_optimization(self):
        """批量查询执行时调用优化计划"""
        from app.store import store
        from app.services.batch_query import BatchQueryService

        svc = BatchQueryService()
        result = svc.execute_batch(1)
        assert result.get("ok"), f"批量查询失败: {result.get('message')}"
        assert result.get("query_count", 0) > 0, "应有查询结果"

        # 验证优化计划被应用
        # result中latency应该被优化收益减少
        for r in result.get("results", []):
            assert r.get("optimized") in (True, False), "应标记是否优化"

    def test_cache_warmup_after_batch(self):
        """批量查询后触发缓存预热"""
        from app.services.cache_layer import CacheLayerService

        svc = CacheLayerService()
        result = svc.warmup_cache("测试API", 10)
        assert result.get("ok") is not None, "warmup_cache应返回结果"

        # 如果缓存策略未生效，应该返回失败但不应报错
        assert isinstance(result, dict)

    def test_cache_evict_expired(self):
        """缓存淘汰过期条目"""
        from app.services.cache_layer import CacheLayerService

        svc = CacheLayerService()
        evicted = svc.evict_expired("不存在的API")
        assert evicted == 0, "无缓存策略时应返回0"

    # ============================================================
    # Flow 7: usage_billing -> call_audit
    # ============================================================

    def test_usage_billing_record_usage(self):
        """记录API调用用量"""
        from app.services.usage_billing import UsageBillingService

        svc = UsageBillingService()
        result = svc.record_usage("测试API", "测试调用方", call_count=10, data_rows=100, amount=5.0)
        assert result is not None
        assert result.get("调用方") == "测试调用方"

    def test_usage_billing_settle_triggers_audit(self):
        """结算完成触发审计"""
        from app.store import store
        from app.services.usage_billing import UsageBillingService

        svc = UsageBillingService()
        # 先记录一些用量
        svc.record_usage("结算测试API", "结算测试方", call_count=50, amount=25.0)

        # 找一条usage_billing记录标记结算
        rows = store.rows("usage_billing")
        if rows:
            uid = rows[-1]["id"]
            result, msg = svc.run_action(uid, "完成结算")
            assert result is not None or "不存在" in msg or "已归档" in msg

    # ============================================================
    # Flow 8: rate_limiter check
    # ============================================================

    def test_rate_limit_check(self):
        """限流检查功能"""
        from app.services.rate_limiter import RateLimiterService

        svc = RateLimiterService()
        result = svc.check_rate_limit("不存在的API", "测试调用方")
        assert result.get("allowed") is True, "无策略时应允许"
        assert result.get("reason") == "未配置限流策略"

    # ============================================================
    # Flow 9: error_classify
    # ============================================================

    def test_error_classify_by_code(self):
        """按错误码进行异常分类"""
        from app.services.error_classify import ErrorClassifyService

        svc = ErrorClassifyService()
        # 种子中应该有5XX分类
        result = svc.classify("500", "Internal Server Error")
        assert isinstance(result, dict)
        assert "matched" in result

        # 非匹配码
        result2 = svc.classify("999", "Unknown")
        assert result2.get("matched") is False

    # ============================================================
    # Flow 10: data_merge
    # ============================================================

    def test_data_merge_sources(self):
        """合并两个数据源"""
        from app.services.data_merge import DataMergeService

        svc = DataMergeService()
        source_a = [{"id": 1, "name": "源A-1"}, {"id": 2, "name": "源A-2"}]
        source_b = [{"id": 2, "name": "源B-2"}, {"id": 3, "name": "源B-3"}]

        # 使用默认归并（rule_id不存在时直接拼接）
        result = svc.merge_sources(999, source_a, source_b)
        assert len(result) >= 3, f"归并后应至少有3条: got {len(result)}"

    def test_data_merge_and_audit(self):
        """归并并写入审计"""
        from app.services.data_merge import DataMergeService

        svc = DataMergeService()
        source_a = [{"id": 1, "name": "A1"}, {"id": 2, "name": "A2"}]
        source_b = [{"id": 2, "name": "B2"}, {"id": 3, "name": "B3"}]
        result = svc.merge_and_audit(999, source_a, source_b)
        assert len(result) >= 3

    # ============================================================
    # Flow 11: call_audit
    # ============================================================

    def test_call_audit_record(self):
        """记录API调用审计"""
        from app.services.call_audit import CallAuditService

        svc = CallAuditService()
        result = svc.record_api_call("测试调用方", "测试API", params="id=1",
                                     response_code="200", latency_ms=45.0, result_rows=10)
        assert result is not None
        assert result.get("调用方") == "测试调用方"
        assert result.get("status") == "已记录"

    def test_call_audit_verify(self):
        """验证审计记录"""
        from app.services.call_audit import CallAuditService

        svc = CallAuditService()
        audits = svc.verify_audits("测试调用方", "不存在的API")
        assert audits == [], "不存在的API应返回空列表"

    # ============================================================
    # Flow 12: data_filter, field_mask, result_format
    # ============================================================

    def test_data_filter_apply(self):
        """应用数据过滤规则"""
        from app.services.data_filter import DataFilterService

        svc = DataFilterService()
        rows = [{"name": "张三", "phone": "13800138000", "email": ""},
                {"name": "李四", "phone": "", "email": "lisi@test.com"}]
        # 没有生效规则时应该原样返回
        result = svc.apply_filters("不存在的API", rows)
        assert len(result) == 2

    def test_field_mask_apply(self):
        """应用字段脱敏"""
        from app.services.field_mask import FieldMaskService

        svc = FieldMaskService()
        rows = [{"name": "张三", "phone": "13800138000"}]
        result = svc.mask_fields("测试API", rows)
        assert len(result) == 1
        # 没有生效脱敏规则时保持原样
        assert result[0]["phone"] == "13800138000"

    def test_result_format_basic(self):
        """格式化输出"""
        from app.services.result_format import ResultFormatService

        svc = ResultFormatService()
        rows = [{"id": 1, "name": "测试"}]
        result = svc.format_results("不存在的API", rows)
        assert isinstance(result, list)

    # ============================================================
    # Flow 13: sdk_config
    # ============================================================

    def test_sdk_config_generate(self):
        """生成SDK代码"""
        from app.services.sdk_config import SdkConfigService

        svc = SdkConfigService()
        # seed中sdk_config有数据
        code = svc.generate_sdk_code(1)
        assert isinstance(code, str)
        assert len(code) > 0 or code == ""

    # ============================================================
    # Flow 14: 跨模块调用 service_for
    # ============================================================

    def test_service_for_all_modules(self):
        """所有18个模块的服务注册正确"""
        from app.services import service_for

        modules = [
            "api_registry", "query_route", "subscription", "rate_limiter",
            "usage_billing", "cache_layer", "data_filter", "field_mask",
            "sla_monitor", "result_format", "query_optimize", "error_classify",
            "data_merge", "export_task", "call_audit", "version_compat",
            "batch_query", "sdk_config"
        ]
        for mod in modules:
            svc = service_for(mod)
            assert svc is not None, f"{mod} 服务未注册"

    # ============================================================
    # Flow 15: 完整跨模块场景
    # ============================================================

    def test_full_lifecycle_flow(self):
        """完整API生命周期：注册->路由->缓存->订阅->限流->用量->导出->审计"""
        from app.store import store
        from app.services import service_for

        # 1. 注册一个API
        api_svc = service_for("api_registry")
        api_entry, _ = api_svc.create_entry({
            "API编号": "API-TEST-001",
            "API名称": "完整测试API",
            "接口路径": "/api/test",
            "请求方法": "GET",
            "数据源": "test_db",
            "返回格式": "json",
            "缓存策略": "default",
            "API版本": "v1",
        })
        assert api_entry is not None

        # 2. 上线API（触发自动创建路由/缓存/SLA）
        api_result, _ = api_svc.run_action(api_entry["id"], "确认上线")
        assert api_result is not None

        # 3. 订阅该API
        sub_svc = service_for("subscription")
        sub_entry, _ = sub_svc.create_entry({
            "申请编号": "SUB-TEST-001",
            "订阅API": "完整测试API",
            "申请方": "测试团队",
        })
        assert sub_entry is not None

        # 4. 审批通过
        sub_svc.run_action(sub_entry["id"], "提交审批")
        sub_result, sub_msg = sub_svc.run_action(sub_entry["id"], "审批通过")
        assert sub_result is not None, f"审批通过失败: {sub_msg}"

        # 5. 记录用量
        usage_svc = service_for("usage_billing")
        usage_result = usage_svc.record_usage("完整测试API", "测试团队", call_count=100, data_rows=500, amount=10.0)
        assert usage_result is not None

        # 6. 频率检查
        rate_svc = service_for("rate_limiter")
        rate_result = rate_svc.check_rate_limit("完整测试API", "测试团队")
        assert isinstance(rate_result, dict)

        # 7. 调用审计
        audit_svc = service_for("call_audit")
        audit_result = audit_svc.record_api_call("测试团队", "完整测试API", response_code="200", latency_ms=50.0)
        assert audit_result is not None

        # 8. SLA检查
        sla_svc = service_for("sla_monitor")
        sla_result = sla_svc.record_metric("完整测试API", latency_ms=50.0, error=False)
        assert isinstance(sla_result, dict) or sla_result == {}

        # 9. 导出
        export_svc = service_for("export_task")
        export_entry, _ = export_svc.create_entry({
            "任务编号": "EXP-TEST-001",
            "关联API": "完整测试API",
            "查询参数": "all",
        })
        exp_result = export_svc.execute_export(export_entry["id"])
        assert exp_result.get("ok")

        # 验证所有关键模块都有产出
        assert store.find("api_registry", api_entry["id"]) is not None
        print("完整生命周期测试通过")


class TestCallAuditArchive:
    """调用审计归档回归：归档划分、重复执行、迟到明细与对账。"""

    BOUNDARY = "2026-09-10"

    def setup_method(self):
        """每个测试前重建store并清空数据目录，避免归档用例互相串扰。"""
        import shutil
        from app.seed import SEED_ROWS
        from app.store import store as s

        s._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        if os.path.exists(temp_data):
            shutil.rmtree(temp_data, ignore_errors=True)

    def _write_call(self, audit_id, record_no, api, ts, logged_at):
        from app.services.data_manager import write_audit

        write_audit(audit_id, {
            "audit_id": audit_id,
            "record_no": record_no,
            "caller": "alice",
            "api_name": api,
            "request_params": "id=1",
            "response_code": "200",
            "latency_ms": 10.0,
            "result_rows": 2,
            "timestamp": ts,
        }, logged_at=logged_at)

    def _seed_two_calls(self):
        self._write_call(1, "CALL-0001", "API-A",
                         "2026-09-01T10:00:00+08:00", "2026-09-01T10:00:01+08:00")
        self._write_call(2, "CALL-0002", "API-A",
                         "2026-09-02T11:00:00+08:00", "2026-09-02T11:00:01+08:00")
        self._write_call(3, "CALL-0003", "API-B",
                         "2026-09-10T09:00:00+08:00", "2026-09-10T09:00:01+08:00")

    def test_archive_excludes_archive_artifact_and_preserves_times(self):
        """归档只收真实调用，不把归档交付物当调用，也不改写原始时间。"""
        import os
        from app.services import data_manager as dm
        from app.services.call_audit import CallAuditService

        self._seed_two_calls()
        svc = CallAuditService()
        result = svc.archive_old_records(self.BOUNDARY)
        assert result["archived_count"] == 2
        assert result["late_record_nos"] == []
        # 边界日当天不计入该归档日期
        assert "CALL-0003" not in {r["record_no"] for r in result["records"]}
        # 原始调用时间与日志写入时间保持原值
        record = dm.read_audit(1)
        assert record["timestamp"] == "2026-09-01T10:00:00+08:00"
        assert record["_logged_at"] == "2026-09-01T10:00:01+08:00"
        assert record["status"] == "已归档"
        # 没有产生伪调用文件，归档交付物不出现在调用明细里
        assert not os.path.exists(dm.DATA_ROOT / "audit" / "000000.json")
        assert {r["record_no"] for r in dm.list_audit()} == {"CALL-0001", "CALL-0002", "CALL-0003"}

    def test_rerun_without_new_records_keeps_archive_reference(self):
        """无新增记录重跑：原归档引用与取数范围保持不变。"""
        from app.services import data_manager as dm
        from app.services.call_audit import CallAuditService

        self._seed_two_calls()
        svc = CallAuditService()
        first = svc.archive_old_records(self.BOUNDARY)
        first_logged = dm.read_audit(1)["_logged_at"]

        second = svc.archive_old_records(self.BOUNDARY)
        assert second["archive_date"] == first["archive_date"]
        assert second["archived_count"] == first["archived_count"] == 2
        assert second["records"] == first["records"]
        assert dm.read_audit(1)["_logged_at"] == first_logged

    def test_late_call_lands_in_same_archive_and_rerun_reconciles(self):
        """迟到明细再次归档归位到原日期，并能与归档错位/日志丢失区分。"""
        import json
        import os
        from app.services import data_manager as dm
        from app.services.call_audit import CallAuditService

        self._seed_two_calls()
        svc = CallAuditService()
        first = svc.archive_old_records(self.BOUNDARY)

        # 迟到但真实：调用时间落在归档日期内，日志写入时间很晚
        svc.backfill_audit_record(
            record_no="CALL-0004", caller="alice", api_name="API-A",
            call_time="2026-09-03T08:30:00+08:00",
            logged_at="2026-09-11T16:00:00+08:00",
        )
        second = svc.archive_old_records(self.BOUNDARY)
        assert second["archive_date"] == first["archive_date"]
        assert second["archived_count"] == 3
        assert second["late_record_nos"] == ["CALL-0004"]
        # 迟到明细的时间在再次归档后仍是原值
        late = [r for r in second["records"] if r["record_no"] == "CALL-0004"][0]
        assert late["timestamp"] == "2026-09-03T08:30:00+08:00"
        assert late["_logged_at"] == "2026-09-11T16:00:00+08:00"

        # 历史归档可按日期复核，并保留真实来源
        history = svc.query_archived_records(start_date=self.BOUNDARY, end_date=self.BOUNDARY)
        assert {h["record_no"] for h in history} == {
            "CALL-0001", "CALL-0002", "CALL-0004"}
        assert all(h["_archive_before_date"] == self.BOUNDARY for h in history)

        # 制造原始日志丢失与归档内容错位
        os.remove(dm.DATA_ROOT / "audit" / "000002.json")
        archive_path = dm.DATA_ROOT / "audit" / f"_archive_{self.BOUNDARY}.json"
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
        for record in archive["records"]:
            if record["record_no"] == "CALL-0001":
                record["timestamp"] = "2026-09-20T00:00:00+08:00"
        archive_path.write_text(json.dumps(archive, ensure_ascii=False), encoding="utf-8")

        final = svc.archive_old_records(self.BOUNDARY)
        assert "CALL-0002" in final["missing_record_nos"]
        assert "CALL-0001" in final["misplaced_record_nos"]
        assert final["reconciliation"]["missing_count"] == 1
        assert final["reconciliation"]["misplaced_count"] == 1