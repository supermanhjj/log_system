# RuiLog 采集器（K8s DaemonSet）

采集器只负责三件事：**按 CRI 拆行、按语言分帧、把原文送到接入网关**。
字段抽取、级别归一、分类、脱敏都在服务端解析管线完成，改规则不碰集群。

## 部署

```bash
# 1) 在平台「数据接入 → 解析规则 → 采集器与审计」创建采集器，拿到明文 key。
#    数据库只存 sha256；旧版 INGEST_COLLECTOR_KEYS 环境变量仍可用但不限制租户。

# 2) 装到 K8s
helm install ruilog-collector ./deploy/k8s/collector \
  -n ruilog-system --create-namespace \
  --set collector.clusterId=sgcc-test \
  --set collector.ingestUrl=http://ruilog-ingest.ruilog-system.svc:8000/ingest/collector \
  --set collector.collectorKey='<第 1 步的 key>'

# 3) 新增业务系统：values 里加三行，然后升级
helm upgrade ruilog-collector ./deploy/k8s/collector \
  -n ruilog-system --reuse-values \
  --set 'inputs[3].name=order-new' \
  --set 'inputs[3].namespace=prod-order-new' \
  --set 'inputs[3].multiline=java'
```

用已有 Secret 时把 `collector.collectorKey` 换成
`collector.existingSecret=<secret-name>`，Secret 的 key 默认 `collector-key`。

## 采哪些日志

`values.yaml` 的 `inputs` 一项对应一个 namespace：

```yaml
- name: i6000-order
  namespace: prod-i6000-order
  multiline: java     # java | go | python | none
```

`name` 决定位点文件 `ruilog-<name>.db`，改名等于从头重采，不要随意改。
`multiline` 只做分帧，异常栈必须配，否则一个 SQL 异常拆成 20 条 UNKNOWN 裸行。

## 六个必踩的坑

| 坑 | 后果 | 本 chart 的处理 |
|---|---|---|
| 不配 `Parser cri` | 整行 `2026-... stdout F xxx` 进 message，服务端 grok 全废 | INPUT 固定 `Parser cri` |
| 多行放在 INPUT 的 `multiline.parser` | 规则匹配到 CRI 前缀而非日志内容，异常栈永远分不开 | 用 FILTER `multiline`，先拆 log 再分帧 |
| DB 写在容器文件系统 | Pod 重建位点丢失，全量重读重复入库 | hostPath `/var/lib/ruilog`，`DirectoryOrCreate` |
| 只挂 `/var/log/containers` | 它是软链，容器内看到断链 | 挂宿主机整个 `/var/log`，只读 |
| 不排除自身和 kube-system | 采自己的日志，自我放大；etcd/kubelet 全进来 | `excludeNamespaces` 默认排除三个 |
| 集群已有日志 DaemonSet | 同一份日志采两遍，流量存储翻倍 | 部署前 `kubectl get ds -A` 查一遍 |

另：`memory.limits` 至少是 `memBufLimit` 的两倍，否则大日志量节点会 OOMKilled。

## 权限

本 chart **不需要** ServiceAccount 访问 API Server、不需要 RBAC、不需要 privileged：
namespace 从文件路径解析，不用 `kubernetes` filter。只需要：

1. `ruilog-system` namespace
2. DaemonSet（仅该 namespace 内）
3. hostPath 只读 `/var/log`、读写 `/var/lib/ruilog`
4. 从国网云 Harbor 拉镜像（需提前推镜像并过安全扫描）
5. 出方向到 ingest Service 的 8000 端口

## 与本地 Docker 环境的关系

`deploy/docker/fluentbit/conf.d/*.conf` 那套是主机文件采集，本地开发链路继续用它。
K8s 场景用本 chart，两套配置互不覆盖；服务端解析规则完全相同——
这正是「解析放服务端」换来的收益：换采集方式不用重写解析。
