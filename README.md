# 馆藏数字阅览的共现诊断与主动补扫

本地数据与术语材料供业务人员整理分析资料使用，所有记录均为合成样例。

## 目录

`fixtures/` 保存合成输入，`docs/` 说明字段与业务规程，`tests/` 检查文件结构。

## 环境和测试

Python 3.11 标准库，无需第三方依赖。运行 `python3 -m unittest discover -s tests` 检查输入结构，运行 `python3 -m compileall -q .` 检查 Python 文件语法。

## 分析流程

`reading_diagnosis/` 实现事件级有效性判定、组合三分类（supported / spurious / inconclusive）、
留一会话样本外检验、补扫机会增量与影响台账，判定口径见 `docs/方法说明.md`，
协议边界以 `docs/业务规程.md` 为准。

运行（`-I` 隔离模式，避免从数据目录加载到植入模块）：

```
python3 -I analyze.py --fixtures fixtures --out out --as-of 2026-11-23
```

产物：`out/diagnosis.json`（机读全量结果）与 `out/diagnosis_report.md`（业务报告）。
可选输入 `closures.csv`、`substitutes.csv`、`constraints.csv` 缺报时按未知处理并在报告中声明降级。
