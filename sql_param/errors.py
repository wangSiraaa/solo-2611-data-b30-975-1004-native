"""库内统一异常类型，全部继承 ValueError，便于调用方按需捕获。"""


class TemplateCompileError(ValueError):
    """模板词法扫描失败（未结束引号/注释、分号、问号、裸冒号等）。"""


class BindingError(ValueError):
    """绑定字典缺键/多键，或值类型不在支持范围内。"""


class PlanStoreError(ValueError):
    """计划发布冲突、计划文件缺失或计划内容不符合公开格式。"""


class BatchError(ValueError):
    """批次预检失败、批次不可变约束被违反等执行前错误。"""


class SlipError(ValueError):
    """对账条出具或校验失败（回执未承诺、下标/列位越界、值类型非法等）。"""


class ReconcileError(ValueError):
    """合账输入不合合同（模板越限、批次超限、快照缺整数主键、搜索超预算等）。"""


class CreditError(ValueError):
    """授信失败（申请不合合同、旧承诺无法兑现、账簿冲突、额度不足等）。"""
