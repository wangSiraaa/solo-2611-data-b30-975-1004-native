"""库内统一异常类型，全部继承 ValueError，便于调用方按需捕获。"""


class TemplateCompileError(ValueError):
    """模板词法扫描失败（未结束引号/注释、分号、问号、裸冒号等）。"""


class BindingError(ValueError):
    """绑定字典缺键/多键，或值类型不在支持范围内。"""


class PlanStoreError(ValueError):
    """计划发布冲突、计划文件缺失或计划内容不符合公开格式。"""


class BatchError(ValueError):
    """批次预检失败、批次不可变约束被违反等执行前错误。"""


class LedgerError(ValueError):
    """分店册签发/读取合同被违反：净数为负、行形状不符、册不存在等。"""


class ClearanceError(ValueError):
    """总店清货轮次合同被违反：余额不足、册版本漂移、状态文件损坏等。"""
