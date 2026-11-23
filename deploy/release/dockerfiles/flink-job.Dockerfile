# Flink 作业镜像：在已有的 Flink 运行镜像里加入作业 jar。
# 云上只需由 CI 用 buildx 构建并推送，不再依赖宿主机 bind mount。
# 基础镜像默认指向本地已导入的 log_system-flink:1.18；
# 云上构建时由 build-images.sh 传入 --build-arg FLINK_BASE=<registry>/flink-base:<tag>。
ARG FLINK_BASE=log_system-flink:1.18
FROM ${FLINK_BASE}
USER root
RUN mkdir -p /opt/flink/jobs && chown flink:flink /opt/flink/jobs
COPY flink-jobs/java/target/log-pipeline.jar /opt/flink/jobs/log-pipeline.jar
USER flink
