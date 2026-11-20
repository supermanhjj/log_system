# Fluent Bit 采集器镜像（业务主机侧唯一进程）
# 关键约束：官方 fluent/fluent-bit:5.2.0-arm64 是 distroless 镜像——无 /bin/sh、无 curl、无 apk，
# 仅有 libc（glibc/aarch64）。但本架构要求：
#   ① exec 输入插件以 `sh -c` 调起随包管控脚本 control.sh（心跳上报 agent-service）；
#   ② validate-chain.sh 通过 `docker exec <fb> curl` 在容器内网做连通性校验。
# 因此用多阶段构建：以 debian:trixie-slim 为 helper（glibc 2.38+/OpenSSL 3.4+，与 fluent-bit
# 5.2.0 底层一致），收集 curl/sh/date/hostname 及其动态库依赖，按原绝对路径打包后 COPY 进
# fluent-bit 基础镜像（COPY 不需要 shell，先注入 /bin/sh 后 RUN/chmod 即可生效）。
# 注意：helper 库版本必须 >= 基础镜像自有库版本，否则 /lib/aarch64-linux-gnu 优先加载的低版本
# 库会致 fluent-bit 二进制报 GLIBC/OPENSSL 符号找不到；bookworm(2.36/3.0) 已踩坑，故用 trixie。
# 5.2.0-arm64 为 Apple Silicon 原生构建，免 QEMU 模拟；exec 输入插件在 4.x/5.x 仍可用。
# 生产若部署到 x86_64 业务主机，将本文件基础镜像改回 fluent/fluent-bit:5.2.0 即可。
# 见 docs/tech-stack-mapping.md 3.2(8) / 9.1

# ---- helper：准备 shell + curl 工具链（glibc/aarch64，版本与 fluent-bit 5.2.0 底层一致） ----
FROM debian:trixie-slim AS helper
# 国内网络下将 Debian 官方源替换为阿里云镜像，避免 deb.debian.org 超时/被拦
RUN sed -i 's#deb.debian.org#mirrors.aliyun.com#g' /etc/apt/sources.list 2>/dev/null || true
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates coreutils hostname \
 && rm -rf /var/lib/apt/lists/*
# 收集二进制及其动态库依赖，按原绝对路径摆到 /staging（COPY 进目标时落回 /）
RUN set -e; \
    mkdir -p /staging; \
    BINS="/usr/bin/curl /bin/sh /bin/date /bin/hostname"; \
    for b in $BINS; do \
      mkdir -p "/staging$(dirname "$b")"; \
      cp -aL "$b" "/staging$b"; \
      for lib in $(ldd "$b" 2>/dev/null | awk '/=>/{print $3}' | sort -u); do \
        [ -e "$lib" ] || continue; \
        mkdir -p "/staging$(dirname "$lib")"; \
        cp -a "$lib" "/staging$lib"; \
      done; \
      # 复制动态链接器（ldd 输出中无 '=>' 的解释器行，如 /lib/ld-linux-aarch64.so.1）；
      # distroless 基础镜像仅含 /usr/lib/.../ld-linux...，dash 的解释器路径 /lib/ld-linux... 缺失会致 exec 失败
      INTERP=$(ldd "$b" 2>/dev/null | awk '!/=>/ && /ld-linux/{print $1; exit}'); \
      if [ -n "$INTERP" ] && [ -e "$INTERP" ]; then \
        mkdir -p "/staging$(dirname "$INTERP")"; \
        cp -aL "$INTERP" "/staging$INTERP"; \
      fi; \
    done; \
    cp -a /etc/ssl "/staging/etc/ssl" 2>/dev/null || true
# 管控脚本在 helper 阶段即设为可执行（distroless 目标镜像无 chmod，无法在 RUN 中设权）
COPY scripts/ /staging/fluent-bit/scripts/
RUN chmod +x /staging/fluent-bit/scripts/*.sh

# ---- runtime：distroless fluent-bit + 注入的 shell/curl ----
FROM fluent/fluent-bit:5.2.0-arm64
# 注入 /bin/sh /usr/bin/curl /bin/date /bin/hostname 及其依赖（基础镜像原图无这些）
COPY --from=helper /staging/ /
# 配置与管控脚本（脚本可执行权限已在 helper 阶段设好，目标镜像无 chmod）
COPY fluent-bit.conf /fluent-bit/etc/fluent-bit.conf
COPY parsers.conf /fluent-bit/config/parsers.conf
COPY --from=helper /staging/fluent-bit/scripts/ /fluent-bit/scripts/
# 基础镜像默认以 root 运行 fluent-bit；distroless 无 fluentbit 用户，沿用 root 即可（开发环境）
