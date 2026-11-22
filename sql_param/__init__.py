"""独立 SQL 命名参数模板编译与 SQLite 批量执行库。

公开入口：
- compile_template:        单条模板的字符级扫描编译器
- make_plan:               由编译产物构造 sql-param-plan/v1 计划字典
- decode_value / bind_parameters: 绑定值解码与按出现顺序绑定
- PlanStore:               计划发布、重放校验与持久化加载
- BatchExecutor:           固定修订、预检绑定、单事务批量执行与回执
- export_slip / validate_slip:   离线柜台对账条（sql-param-slip/v1）出具与校验
- reconcile_round / verify_order: 回单室合账（交错顺序搜索、终局枚举、独立复核）
- CreditDesk / capacities_from_report: 授信席（最大价值签单、配给、缺口见证）
"""
from .compiler import (
    CompiledTemplate,
    Parameter,
    TemplateCompileError,
    compile_template,
    make_plan,
    normal_view,
)
from .bindings import BindingError, bind_parameters, decode_value, encode_json_value
from .errors import (
    BatchError,
    CreditError,
    PlanStoreError,
    ReconcileError,
    SlipError,
)
from .plan_store import PLAN_FORMAT, PlanStore, validate_plan
from .executor import BatchExecutor
from .slips import SLIP_FORMAT, export_slip, validate_slip
from .reconcile import (
    MAX_BATCHES_PER_ROUND,
    RECONCILE_FORMAT,
    reconcile_round,
    verify_order,
)
from .credit import (
    LEDGER_FORMAT,
    MAX_APPLICATIONS_PER_ROUND,
    MAX_BINS,
    CreditDesk,
    capacities_from_report,
)

__all__ = [
    "CompiledTemplate",
    "Parameter",
    "TemplateCompileError",
    "compile_template",
    "make_plan",
    "normal_view",
    "BindingError",
    "bind_parameters",
    "decode_value",
    "encode_json_value",
    "BatchError",
    "CreditError",
    "PlanStoreError",
    "ReconcileError",
    "SlipError",
    "PLAN_FORMAT",
    "PlanStore",
    "validate_plan",
    "BatchExecutor",
    "SLIP_FORMAT",
    "export_slip",
    "validate_slip",
    "MAX_BATCHES_PER_ROUND",
    "RECONCILE_FORMAT",
    "reconcile_round",
    "verify_order",
    "LEDGER_FORMAT",
    "MAX_APPLICATIONS_PER_ROUND",
    "MAX_BINS",
    "CreditDesk",
    "capacities_from_report",
]
