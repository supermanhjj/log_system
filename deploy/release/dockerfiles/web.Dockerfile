# 本地/云上通用的前端镜像：dist 打入镜像。
# 网关配置不再烤进镜像：K8s 侧由 ConfigMap + initContainer 在启动时渲染
# （DNS 地址 / 命名空间都是运行期注入，换集群不用重建镜像）。
FROM nginx:1.27-alpine
COPY frontend/dist /usr/share/nginx/html
