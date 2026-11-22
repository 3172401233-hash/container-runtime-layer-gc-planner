# container-runtime-layer-gc-planner

规划容器层引用计数、共享摘要和空间回收。

容器镜像层可能被多个镜像、运行中容器和构建缓存共同引用。如果按"未被
tag 引用就删除"的朴素策略回收，会把仍在运行的容器层清掉。本库只做
**规划**：根据输入的镜像清单、tag、digest、运行容器、构建租约、共享层
和临时挂载，计算出可删除、保留和待观察的层，并给出释放空间、引用链和
阻塞原因。

## 重要：不删除真实文件

本库是纯规划器，**绝不触碰文件系统或真实容器运行时**。它只返回一份
`GCPlan` 决策清单，由调用方（如 containerd / buildkit 的 GC 执行器）
自行决定是否、何时执行删除。损坏摘要的层只会被标记隔离（quarantine），
同样不会被删除或复用。

## 回收规则

- 被 tag、运行中容器、未过期租约或临时挂载引用的层必须保留（retain）。
- 被引用层的所有祖先层沿父链向上保留，父层不会在子层存活时被回收。
- 未被引用的层只有在其整条父链都可删除时才标记为可删除（delete）；
  父层仍被保留时，不能只删子层记录，该层进入待观察（observe）。
- 摘要损坏的层一律隔离（quarantine），既不复用也不自动删除。
- 已停止的容器不固定任何层。

## 用法

```python
from layergc import ImageManifest, LayerGCPlanner, LayerRecord, Tag

layers = [
    LayerRecord("sha256:base", size=500),
    LayerRecord("sha256:top", size=20, parent="sha256:base"),
]
manifests = [ImageManifest("m1", ("sha256:base", "sha256:top"))]
tags = [Tag("app:latest", "m1")]

plan = LayerGCPlanner(layers, manifests, tags).plan()
print(plan.freed_bytes)        # 可回收字节数
print(plan.deletable)          # 可删除层及原因
print(plan.retained)           # 保留层及引用链
print(plan.observing)          # 待观察层及阻塞原因
print(plan.quarantined)        # 隔离层（损坏摘要）
print(plan.reference_chains)   # 每层的完整引用链
print(plan.blocking_reasons)   # 每层不可删除的原因
```

## 测试

```sh
python -m unittest
```

兼容 Python 3.8.2+。测试覆盖多 tag、共享层、运行容器、构建租约、临时
挂载和损坏摘要等场景。
