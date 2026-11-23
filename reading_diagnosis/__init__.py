"""馆藏数字阅览的共现诊断与主动补扫。

模块划分：
- models:   领域模型（事件、扫描记录、政策、评估结果）
- io_table: CSV 载入与字段解析（空值保留为 None，区别于零与缺报）
- protocol: 按《阅览与补扫协议》做事件级有效性判定
- pairs:    组合构建、去重、三分类与样本外检验
- planner:  补扫候选、机会增量、约束与影响台账
- pipeline: 串接全流程并产出报告
"""

from .pipeline import run_analysis, write_reports

__all__ = ["run_analysis", "write_reports"]
