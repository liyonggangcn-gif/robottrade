# 因子挖掘工具包

本目录包含因子挖掘、测试和优化的工具集。

---

## 📁 文件说明

### `factor_tester.py` - 因子测试器
**功能**：
- ✅ 单因子IC测试（Information Coefficient）
- ✅ 分组回测（十分组）
- ✅ 多空收益计算
- ✅ Sharpe比率、信息比率（IR）
- ✅ IC时间序列可视化

**使用示例**：
```python
from tools.factor_mining.factor_tester import FactorTester

# 初始化测试器
tester = FactorTester(start_date='2024-01-01', end_date='2026-01-01')

# 加载数据
df = tester.load_data()

# 构建因子（例如：PE倒数）
factor = 1 / df['pe_ttm'].replace(0, np.nan)
returns = df.groupby('ts_code')['return_1d'].shift(-1)

# 测试因子
results = tester.test_factor(factor, returns, factor_name='PE_INV')

# 绘制IC分析图
ic_series = tester.calculate_ic(factor, returns)
tester.plot_ic_series(ic_series, factor_name='PE_INV')
```

**运行Demo**：
```bash
python tools/factor_mining/factor_tester.py
```

**输出示例**：
```
============================================================
因子测试: PE_INV
============================================================

【IC分析】
  IC均值: 0.0523
  IC标准差: 0.1142
  IR（信息比率）: 0.4580
  IC胜率: 61.23%

【分组测试】(十分组)
  Top组平均收益: 0.1234%
  Bottom组平均收益: -0.0567%
  多空收益: 0.1801%
  多空Sharpe: 1.2456
  单调性: 0.8765

【综合评价】
  ✅ 因子有效性：优秀
```

---

## 🔧 扩展功能（待开发）

### `factor_generator.py` - 因子生成器
**计划功能**：
- 特征交叉自动生成
- 遗传算法Alpha挖掘
- 深度学习特征提取
- 因子正交化

### `factor_library.py` - 因子库
**计划功能**：
- WorldQuant 101 Alphas实现
- Barra因子库
- 自定义因子管理

### `factor_optimizer.py` - 因子优化器
**计划功能**：
- 因子组合优化
- 因子权重分配
- 多因子模型构建

---

## 📊 因子评价标准

### IC（Information Coefficient）
- **优秀**: |IC| > 0.05, 胜率 > 55%
- **良好**: |IC| > 0.03, 胜率 > 50%
- **一般**: |IC| > 0.01
- **较弱**: |IC| < 0.01

### IR（Information Ratio）
- **优秀**: IR > 0.5
- **良好**: IR > 0.3
- **一般**: IR > 0.1

### 多空Sharpe
- **优秀**: Sharpe > 2.0
- **良好**: Sharpe > 1.0
- **一般**: Sharpe > 0.5

### 单调性
- **优秀**: |Monotonicity| > 0.8
- **良好**: |Monotonicity| > 0.6
- **一般**: |Monotonicity| > 0.4

---

## 📚 参考资源

### 因子挖掘理论
- 《因子投资：方法与实践》- 石川
- WorldQuant 101 Formulaic Alphas
- Fama-French因子模型

### 在线工具
- 聚宽因子库：https://www.joinquant.com/help/api/help#Factor
- 优矿因子平台：https://uqer.datayes.com/
- 米筐因子API：https://www.ricequant.com/doc/api/

---

## 💡 使用建议

1. **先测试经典因子**：PE、PB、ROE、动量等
2. **构建因子组合**：单一因子效果有限，组合使用更稳定
3. **定期更新测试**：市场环境变化，因子有效性会衰减
4. **注意数据质量**：脏数据会严重影响因子测试结果
5. **样本外验证**：避免过拟合，使用样本外数据验证

---

## 🚀 快速开始

### 1. 测试一个简单因子
```python
# 动量因子（20日收益率）
factor = df.groupby('ts_code')['close'].pct_change(20)
returns = df.groupby('ts_code')['return_1d'].shift(-1)

tester = FactorTester()
results = tester.test_factor(factor, returns, factor_name='MOM_20')
```

### 2. 测试基本面因子
```python
# ROE因子
factor = df['roe']
returns = df.groupby('ts_code')['return_1d'].shift(-1)

results = tester.test_factor(factor, returns, factor_name='ROE')
```

### 3. 测试复合因子
```python
# PEG因子（PE / 盈利增长率）
factor = df['pe_ttm'] / df['netprofit_yoy'].replace(0, np.nan)
returns = df.groupby('ts_code')['return_1d'].shift(-1)

results = tester.test_factor(factor, returns, factor_name='PEG')
```

---

**欢迎贡献新的因子和测试工具！**
