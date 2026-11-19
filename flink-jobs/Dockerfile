# Flink 1.18 运行镜像 + Kafka connector（官方镜像不含 connector，需构建注入）
# 两个 connector jar 由 flink-jobs/lib/ 提供（见 README 构建步骤）。
FROM flink:1.18-scala_2.12

# 把 Kafka connector + kafka-clients 放进 Flink classpath
COPY lib/flink-connector-kafka-*.jar /opt/flink/lib/
COPY lib/kafka-clients-*.jar /opt/flink/lib/

# 校正权限（Flink 以 flink 用户运行）
USER root
RUN chown flink:flink /opt/flink/lib/*.jar
USER flink
