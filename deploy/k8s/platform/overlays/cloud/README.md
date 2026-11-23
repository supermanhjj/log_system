# 云上 K8s overlay 使用说明

三层 overlay 的分工：

| overlay | 用途 | 数据层形态 |
|---|---|---|
| `local` | Docker Desktop 本地验证 | 单 Pod、local-path、小 PVC、Kafka RF1、OS 1/0 |
| `cloud` | 云上测试环境 | 单 Pod、云盘 StorageClass、按日量扩 PVC、OS 3 分片 0 副本、Kafka RF1 |
| `cloud-ha` | 3 节点生产形态 | 继承 cloud，OS 3 分片 2 副本、Kafka RF3/min ISR2 |

## 部署前

1. 改 `cloud/kustomization.yaml`：
   - `images` 里的 `registry.example.com/ruilog` 换成实际 Harbor/ACR 地址；
   - `cloud-ssd` 换成集群实际 StorageClass；如果集群有默认 StorageClass，可删掉所有 `storageClassName` 的 add patch；
   - PVC 大小按真实日量调整（当前按 0.05–0.1 TB/天估算）。
2. 构建并推送镜像（先构建 Flink base，再构建 job）：

```bash
REGISTRY=<registry>/ruilog TAG=0.9.0-test \
  sh deploy/release/build-images.sh
```

3. 确认 `frontend/dist` 和 `flink-jobs/java/target/log-pipeline.jar` 已存在；构建脚本会预检。

## 部署

```bash
sh deploy/k8s/apply.sh cloud
kubectl -n ruilog-system get pods -w
```

> 不要直接 `kubectl apply -k`：base 的 configMapGenerator 引用了 `deploy/shared`，
> 超出 base 根目录会被 Kustomize 默认加载限制拒绝；`apply.sh` 已用
> `--load-restrictor LoadRestrictionsNone` 处理。

首次启动顺序：`postgres` → `opensearch` / `kafka` / `clickhouse` / `redis` →
`kafka-init` / `opensearch-init` → 业务服务 → `flink-jobmanager/taskmanager` →
`flink-job-supervisor`（常驻，等 JM 就绪后自动提交并持续守护作业）。

## 采集器

云上测试用 `values-cloud.yaml`，替换 4 个占位值后再装：

```bash
helm upgrade --install ruilog-collector ./deploy/k8s/collector \
  -n ruilog-system --create-namespace \
  -f deploy/k8s/collector/values-cloud.yaml
```

`inputs: []` 表示先只上报心跳；接入真实业务后按文件内注释追加 input。

## 注意

- `cloud-ha` 只改副本策略，不会把 Kafka/OpenSearch StatefulSet 从 1 Pod 变 3 Pod；
  多副本 StatefulSet 或托管中间件需要单独准备。
- 云上测试默认仍关闭 OpenSearch 安全插件；要开启时按 `cloud/kustomization.yaml`
  末尾注释同时改 `OPENSEARCH_SCHEME`、`OPENSEARCH_URL`，并在 `ruilog-secret`
  里加 `OPENSEARCH_USER` / `OPENSEARCH_PASSWORD`。
- 已有 Kafka topic 不会被 `kafka-init` 改副本数；RF 调整只对首次创建生效。
