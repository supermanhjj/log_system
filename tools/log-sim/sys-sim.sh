#!/bin/sh
# =============================================================================
# 系统日志模拟：busybox syslogd 真实落盘 + logger 产生真实 RFC3164 事件
#
# 为什么要用真 syslogd 而不是脚本拼字符串：
#   syslog 的难点在格式细节（无年份时间戳、facility.severity 标记、tag 前缀、
#   内核消息形态）。用真守护进程产出的行，才验证得了解析器。
#
# 输出：/var/log/ruilog/syslog（与 nginx/redis 共用平台日志卷 ruilog_logs）
#   Nov 19 08:55:48 <hostname> authpriv.warn sshd: Failed password for invalid user ...
# =============================================================================
set -e

LOG_DIR=/var/log/ruilog
mkdir -p "$LOG_DIR"

# -n 前台，-O 直接写文件（不做 remote 转发）
syslogd -n -O "$LOG_DIR/syslog" &
sleep 1

i=0
while true; do
  i=$((i + 1))
  # 常规系统事件（daemon）
  logger -p daemon.info -t systemd "Started Session $((1000 + i)) of user ops."
  logger -p cron.info -t CROND "(${i}) CMD (/usr/local/bin/health-check.sh)"

  # 认证类（authpriv）：最典型的运维排查目标
  if [ $((i % 3)) -eq 0 ]; then
    logger -p authpriv.warning -t sshd \
      "Failed password for invalid user admin from 10.20.30.$((i % 250 + 1)) port $((40000 + i)) ssh2"
  else
    logger -p authpriv.info -t sshd \
      "Accepted publickey for deploy from 10.20.30.$((i % 250 + 1)) port $((40000 + i)) ssh2"
  fi

  # 内核/资源类（kern）：OOM、磁盘、连接数——告警规则的主要来源
  case $((i % 5)) in
    0) logger -p kern.crit -t kernel \
         "Out of memory: Killed process $((2000 + i)) (java) total-vm:2048000kB, anon-rss:1900000kB" ;;
    1) logger -p kern.warn -t kernel \
         "TCP: request_sock_TCP: Possible SYN flooding on port 8080. Sending cookies." ;;
    2) logger -p daemon.err -t dockerd \
         "level=error msg=\"failed to start container: no space left on device\"" ;;
    *) logger -p daemon.info -t systemd "Reached target Multi-User System." ;;
  esac

  # 少量应用侧 stderr 混入（真实主机上很常见）
  if [ $((i % 7)) -eq 0 ]; then
    logger -p user.err -t app-runner "connection refused: dial tcp 127.0.0.1:6379"
  fi

  sleep 2
done
