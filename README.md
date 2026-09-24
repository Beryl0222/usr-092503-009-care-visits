# 农村养老探访风险协调器

本项目维护县、乡、村三级协同所需的探访结果和风险等级契约。当前模块只处理稳定标识及离线凭证格式，便于不同站点交换一致的数据。

## 本地运行

执行测试：

```bash
python3 -m unittest discover -s tests -v
```

执行构建检查：

```bash
python3 -m compileall -q src
```
