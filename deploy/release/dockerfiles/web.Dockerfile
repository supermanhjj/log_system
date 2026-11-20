# 本地/云上通用的前端镜像：dist 与 K8s 网关配置打入镜像，避免依赖宿主机挂载。
FROM nginx:1.27-alpine
COPY frontend/dist /usr/share/nginx/html
COPY deploy/k8s/platform/base/nginx-k8s.conf /etc/nginx/conf.d/default.conf
