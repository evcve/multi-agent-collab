# tools/

工程工具集：从 CAD/FEM 提取几何 → 干涉检测。**通用实现，不含任何项目数据。**

## 工具清单

| 工具 | 输入 | 输出 |
|---|---|---|
| `dxf_rect_extract.py` | ASCII DXF（正交条带几何） | JSON 条带列表（方向/名称/范围/中心） |
| `fem_rbe2_holes.py` | Nastran .fem/.bdf | JSON 安装孔坐标（RBE2 dependent 节点） |
| `interference_check.py` | holes.csv + bands.csv | 冲突清单（孔落入条带） |

## 背景（为什么这样设计）

- **DXF 固定列陷阱**：Nastran/UG 导出可能用固定列格式，相邻数字粘连
  （`221.952944.6044` 是两个数），必须按列切分不能按空格
- **RBE2 续行**：字段 7 为 `+` 时 dependent 节点列表跨行，漏解析会丢孔
- **质量点=质心**：CONM2 挂载节点即单机质心；RBE2 dependent 节点即安装孔
  中心在板面的投影——这是 FEM 里"孔位"的免费数据源，不用另建孔

## 示例

```bash
# 1. 从 DXF 提取条带（热管/槽道）
python dxf_rect_extract.py ../examples/sample_bands.dxf

# 2. 从 FEM 提取安装孔（指定关注的节点）
python fem_rbe2_holes.py model.fem --nodes 45,44,43

# 3. 干涉检测（孔 vs 条带，8mm 安全边距）
python interference_check.py --holes ../examples/sample_holes.csv \
                             --bands ../examples/sample_bands.csv --margin 8
```

## 依赖

纯 Python 标准库（无 numpy/pandas），Python 3.8+。
