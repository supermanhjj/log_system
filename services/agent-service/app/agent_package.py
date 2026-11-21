"""接入包渲染：把「下载接入配置」从单文件模板升级为可直接落地的 tar.gz。

两种形态，对应仓库里真实跑通的两条链路：

- ``host``      业务主机 / 物理机。tail 多路日志，env 注入地址与凭证，
                适合安装在集群外的机器（平台地址走网关或 NodePort）。
- ``container`` K8s 容器（DaemonSet 形态）。tail ``/var/log/containers/*.log``，
                CRI 拆包 + 多行分帧，地址走 service DNS。

三条硬约束（上一版单文件 conf 不可用的根因，改动时别退回去）：

1. **自包含**：旧模板引用了平台镜像里才有的 ``app_log`` parser，目标机没有
   该文件时 Fluent Bit 直接起不来。这里把 parsers.conf 随包下发并在配置里指向它。
2. **地址可注入**：不再写死 ``agent-service`` / ``ingest-service``，外部主机
   填网关地址即可；容器形态才用 service DNS。
3. **凭证不留中文占位符**：由 agent-service 签发后写进 env；未签发时用 ASCII
   的 ``REPLACE_ME_WITH_COLLECTOR_KEY`` 显式占位，避免中文 header 触发网关 500。

占位手法用 ``@@KEY@@`` 而不是 ``str.format`` / ``Template``：配置正文里既有
JSON 花括号又有 Fluent Bit 的 ``${ENV}``，两种插值语法都会打架。
"""
from __future__ import annotations

import io
import json
import tarfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------
_PARSERS_HOST = """\
# =============================================================================
# RuiLog 主机采集 · parser 集（随接入包下发，不依赖平台镜像）
# 只做字段抽取，不在这里做租户归属；归属由 ingest-service 判定。
# =============================================================================
[PARSER]
    Name        ruilog_json
    Format      json
    Time_Key    time
    Time_Format %Y-%m-%dT%H:%M:%S.%L

[PARSER]
    Name        ruilog_syslog
    Format      regex
    Time_Key    event_time
    Time_Format %b %d %H:%M:%S
    Time_Offset +0800
    Regex       ^(?<raw>(?<event_time>[A-Z][a-z]{2}\\s+\\d{1,2}\\s+\\d{2}:\\d{2}:\\d{2})\\s+(?<host>\\S+)\\s+(?<facility>[a-z0-9]+)\\.(?<severity_name>[a-z]+)\\s+(?<source>[^:]+):\\s?(?<message>[\\s\\S]*))$

[PARSER]
    Name        ruilog_nginx_access
    Format      regex
    Time_Key    event_time
    Time_Format %d/%b/%Y:%H:%M:%S %z
    Regex       ^(?<raw>(?<remote_addr>\\S+)\\s+-\\s+(?<remote_user>\\S+)\\s+\\[(?<event_time>[^\\]]+)\\]\\s+"(?<request>[^"]*)"\\s+(?<status>\\d{3})\\s+(?<body_bytes>\\S+)\\s+"(?<referer>[^"]*)"\\s+"(?<user_agent>[^"]*)")$

[PARSER]
    Name        ruilog_nginx_error
    Format      regex
    Time_Key    event_time
    Time_Format %Y/%m/%d %H:%M:%S
    Time_Offset +0800
    Regex       ^(?<raw>(?<event_time>\\d{4}/\\d{2}/\\d{2}\\s+\\d{2}:\\d{2}:\\d{2})\\s+\\[(?<level>\\w+)\\]\\s+(?<pid>\\d+)#(?<tid>\\d+):\\s+\\*?(?<conn_id>\\d+)?\\s*(?<message>[\\s\\S]*))$
"""

_PARSERS_CONTAINER = """\
# =============================================================================
# RuiLog 容器采集 · parser 集
# CRI 格式：time stream logtag log。不配这个 parser，整行会进 message，
# 服务端 grok 全部失效；多行分帧也必须在拆出 log 字段之后做。
# =============================================================================
[PARSER]
    Name        cri
    Format      regex
    Regex       ^(?<time>[^ ]+) (?<stream>stdout|stderr) (?<logtag>[^ ]*) (?<log>.*)$
    Time_Key    time
    Time_Format %Y-%m-%dT%H:%M:%S.%L%z

# ---- 多行分帧：堆栈/异常续行并入首行，否则一条异常会被拆成几十条日志 ----
# 判据：首行以「时间戳 + 级别」开头；后续不以时间戳开头的行都属于上一条。
[MULTILINE_PARSER]
    Name          ruilog-java
    Type          regex
    Flush_Timeout 1000
    Rule          "start_state" "/(?<time>[0-9]{4}-[0-9]{2}-[0-9]{2}[^ ]*)[ ]+(?<level>[A-Z]+)[ ]+(?<message>.*)/" "cont"
    Rule          "cont" "/(?![0-9]{4}-[0-9]{2}-[0-9]{2})(?<message>.*)/" "cont"

[MULTILINE_PARSER]
    Name          ruilog-go
    Type          regex
    Flush_Timeout 1000
    Rule          "start_state" "/(?<time>[0-9]{4}-[0-9]{2}-[0-9]{2}[^ ]*)[ ]+(?<message>.*)/" "cont"
    Rule          "cont" "/(?![0-9]{4}-[0-9]{2}-[0-9]{2})(?<message>.*)/" "cont"

[MULTILINE_PARSER]
    Name          ruilog-python
    Type          regex
    Flush_Timeout 1000
    Rule          "start_state" "/(?<time>[0-9]{4}-[0-9]{2}-[0-9]{2}[^ ]*)[ ]+(?<level>[A-Z]+)[ ]+(?<message>.*)/" "cont"
    Rule          "cont" "/(?![0-9]{4}-[0-9]{2}-[0-9]{2})(?<message>.*)/" "cont"
"""

_HOST_CONF = """\
# =============================================================================
# RuiLog 主机采集 Agent（Fluent Bit）— 由平台按参数生成
# 目标主机 : @@HOSTNAME@@
# 目标租户 : @@TENANT@@
# 生成时间 : @@GENERATED_AT@@
#
# 安装：见同目录 README.md；配置项全部通过 agent.env 注入，不要改本文件里的
# ${VAR}，改 env 后 systemctl restart ruilog-agent 即可生效。
# =============================================================================
[SERVICE]
    Flush                     1
    Daemon                    off
    Log_Level                 info
    HTTP_Server               On
    HTTP_Listen               0.0.0.0
    HTTP_Port                 2020
    Parsers_File              /etc/ruilog/parsers.conf
    storage.path              /var/lib/ruilog/buffer/
    storage.sync              normal
    storage.max_chunks_up     128

# ---- 管控心跳：每 30s 上报，服务端以接收时刻判定在线 ----
# hostname / tenant 在生成时写死，避免依赖 env 插值；改主机名请重新下载接入包。
[INPUT]
    Name         dummy
    Tag          agent.heartbeat
    Dummy        {"hostname":"@@HOSTNAME@@","tenant":"@@TENANT@@","version":"@@VERSION@@","os":"linux","status":"online"}
    Interval_Sec 30

[OUTPUT]
    Name         http
    Match        agent.heartbeat
    Host         ${AGENT_HOST}
    Port         ${AGENT_PORT}
    URI          /heartbeat
    Format       json
    Header       Content-Type application/json
    Retry_Limit  False

# ---- 业务日志：应用直接输出的一行一条 JSON ----
[INPUT]
    Name              tail
    Tag               ruilog.business.json
    Path              "@@APP_LOG_JSON@@"
    Parser            ruilog_json
    Path_Key          src_file
    Read_from_head    true
    DB                /var/lib/ruilog/business-json.db
    Buffer_Chunk_Size 256K
    Buffer_Max_Size   8M
    Mem_Buf_Limit     32M
    Rotate_Wait       5
    Skip_Long_Lines   On

# ---- 业务日志：普通文本 ----
[INPUT]
    Name              tail
    Tag               ruilog.business.text
    Path              "@@APP_LOG_TEXT@@"
    Exclude_Path      "@@APP_LOG_JSON@@"
    Path_Key          src_file
    Read_from_head    true
    DB                /var/lib/ruilog/business-text.db
    Buffer_Chunk_Size 256K
    Buffer_Max_Size   8M
    Mem_Buf_Limit     32M
    Rotate_Wait       5
    Skip_Long_Lines   On

# ---- 系统日志：Debian/Ubuntu 用 syslog，RHEL/CentOS 改 SYSLOG_PATH=messages ----
[INPUT]
    Name              tail
    Tag               ruilog.system.syslog
    Path              "@@SYSLOG_PATH@@"
    Parser            ruilog_syslog
    Path_Key          src_file
    Read_from_head    true
    DB                /var/lib/ruilog/system-syslog.db
    Buffer_Chunk_Size 128K
    Buffer_Max_Size   4M
    Mem_Buf_Limit     20M
    Rotate_Wait       5

# ---- nginx access / error（路径不存在时 tail 会等待，不影响其他输入）----
[INPUT]
    Name              tail
    Tag               ruilog.middleware.nginx_access
    Path              "@@NGINX_ACCESS_PATH@@"
    Parser            ruilog_nginx_access
    Path_Key          src_file
    Read_from_head    true
    DB                /var/lib/ruilog/nginx-access.db
    Buffer_Chunk_Size 256K
    Buffer_Max_Size   8M
    Mem_Buf_Limit     16M
    Rotate_Wait       5

[INPUT]
    Name              tail
    Tag               ruilog.middleware.nginx_error
    Path              "@@NGINX_ERROR_PATH@@"
    Parser            ruilog_nginx_error
    Path_Key          src_file
    Read_from_head    true
    DB                /var/lib/ruilog/nginx-error.db
    Buffer_Chunk_Size 128K
    Buffer_Max_Size   4M
    Mem_Buf_Limit     16M
    Rotate_Wait       5

# ---- 来源元数据：采集端只打"可信部署元数据"，不注入 tenant_id ----
# 租户由 ingest-service 推导（hosts 台账 > 文件名），注入值不参与最终归属。
[FILTER]
    Name    record_modifier
    Match   ruilog.business.*
    Record  log_class   business
    Record  source_type app
    Record  app         ${APP_NAME}
    Record  host        ${HOST_NAME}
    Record  cluster_id  ${CLUSTER_ID}

[FILTER]
    Name    record_modifier
    Match   ruilog.system.*
    Record  log_class   system
    Record  source_type syslog
    Record  app         linux
    Record  cluster_id  ${CLUSTER_ID}

[FILTER]
    Name    record_modifier
    Match   ruilog.middleware.nginx_access
    Record  log_class   middleware
    Record  source_type nginx-access
    Record  app         nginx
    Record  host        ${HOST_NAME}
    Record  cluster_id  ${CLUSTER_ID}

[FILTER]
    Name    record_modifier
    Match   ruilog.middleware.nginx_error
    Record  log_class   middleware
    Record  source_type nginx-error
    Record  app         nginx
    Record  host        ${HOST_NAME}
    Record  cluster_id  ${CLUSTER_ID}

# ---- 统一上报接入网关 ----
# Retry_Limit False：网关不可用时持续重试而不是丢数据（旧模板缺这一行，
# 凭证填错时重试几次就丢）。
[OUTPUT]
    Name              http
    Match             ruilog.*
    Host              ${INGEST_HOST}
    Port              ${INGEST_PORT}
    URI               @@INGEST_URI@@
    Format            json
    Header            Content-Type application/json
    Header            X-Collector-Key ${COLLECTOR_KEY}
    Retry_Limit       False
"""

_CONTAINER_CONF = """\
# =============================================================================
# RuiLog 容器采集 Agent（DaemonSet 形态）— 由平台按参数生成
# 节点 / 主机 : @@HOSTNAME@@
# 目标租户   : @@TENANT@@
# 生成时间   : @@GENERATED_AT@@
#
# 用法：把本文件与 parsers.conf 挂进 Fluent Bit 容器（/fluent-bit/etc/），
# 并注入 COLLECTOR_KEY / NODE_NAME / INGEST_HOST / INGEST_PORT / CLUSTER_ID。
# =============================================================================
[SERVICE]
    Flush                     1
    Daemon                    off
    Log_Level                 info
    Parsers_File              /fluent-bit/etc/parsers.conf
    HTTP_Server               On
    HTTP_Listen               0.0.0.0
    HTTP_Port                 2020
    storage.path              /var/lib/ruilog/buffer/
    storage.sync              normal
    storage.max_chunks_up     128

[INPUT]
    Name              tail
    Tag               kube.*
    Path              "@@CONTAINER_LOG_GLOB@@"
    Parser            cri
    Path_Key          src_file
    DB                /var/lib/ruilog/containers.db
    Mem_Buf_Limit     64MB
    Skip_Long_Lines   On
    storage.type      filesystem
@@MULTILINE_FILTER@@
# 按需追加来源分类标签：采集端只打可信部署元数据，租户由服务端按
# namespace（容器日志文件名）判定，不在这里注入 tenant_id。
[FILTER]
    Name              record_modifier
    Match             kube.*
    Record            log_class    business
    Record            source_type  app
    Record            host         ${NODE_NAME}
    Record            cluster_id   ${CLUSTER_ID}

# ---- 管控心跳：hostname 在生成时写死，与 Agent 管理页一一对应 ----
[INPUT]
    Name         dummy
    Tag          agent.heartbeat
    Dummy        {"hostname":"@@HOSTNAME@@","tenant":"@@TENANT@@","version":"@@VERSION@@","os":"linux","status":"online"}
    Interval_Sec 30

[OUTPUT]
    Name         http
    Match        agent.heartbeat
    Host         ${AGENT_HOST}
    Port         ${AGENT_PORT}
    URI          /heartbeat
    Format       json
    Header       Content-Type application/json
    Retry_Limit  False

[OUTPUT]
    Name              http
    Match             kube.*
    Host              ${INGEST_HOST}
    Port              ${INGEST_PORT}
    URI               @@INGEST_URI@@
    Format            json
    Header            Content-Type application/json
    Header            X-Collector-Key ${COLLECTOR_KEY}
    Retry_Limit       False
"""

_MULTILINE_BLOCK = """\
# 多行分帧必须在 CRI 拆包之后：Buffer 不开时跨行堆栈拆不开（实测踩坑）。
[FILTER]
    Name                  multiline
    Match                 kube.*
    Buffer                On
    multiline.key_content log
    multiline.parser      @@MULTILINE_PARSER@@

"""

_HOST_ENV = """\
# RuiLog 主机采集 Agent 环境变量
# 权限：chmod 600；本文件含采集器凭证，不要提交到代码库。
# 改完执行：systemctl restart ruilog-agent

COLLECTOR_KEY=@@COLLECTOR_KEY@@
HOST_NAME=@@HOSTNAME@@
INGEST_HOST=@@INGEST_HOST@@
INGEST_PORT=@@INGEST_PORT@@
AGENT_HOST=@@AGENT_HOST@@
AGENT_PORT=@@AGENT_PORT@@
APP_NAME=@@APP_NAME@@
CLUSTER_ID=@@CLUSTER_ID@@
"""

_INSTALL_SH = """\
#!/bin/sh
# RuiLog 主机采集 Agent 安装脚本（由平台接入包生成）
# 前置：已安装 Fluent Bit 5.x（用离线包里的 deb/rpm，本包不含二进制）
# 用法：sudo sh install.sh
set -eu

SRC_DIR=$(cd "$(dirname "$0")" && pwd)
CONF_DIR=/etc/ruilog
STATE_DIR=/var/lib/ruilog
UNIT=/etc/systemd/system/ruilog-agent.service

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root 或 sudo 执行" >&2
    exit 2
fi

if ! command -v fluent-bit >/dev/null 2>&1; then
    echo "未找到 fluent-bit，请先安装离线包里的 deb/rpm，再执行本脚本" >&2
    exit 2
fi

mkdir -p "$CONF_DIR" "$STATE_DIR/buffer"

# 已有配置先备份，避免重复安装把现场改动冲掉
STAMP=$(date +%Y%m%d-%H%M%S)
for f in fluent-bit.conf parsers.conf agent.env; do
    if [ -f "$CONF_DIR/$f" ]; then
        cp "$CONF_DIR/$f" "$CONF_DIR/$f.bak-$STAMP"
    fi
done

cp "$SRC_DIR/fluent-bit.conf" "$CONF_DIR/fluent-bit.conf"
cp "$SRC_DIR/parsers.conf" "$CONF_DIR/parsers.conf"
cp "$SRC_DIR/agent.env" "$CONF_DIR/agent.env"
chmod 600 "$CONF_DIR/agent.env"

cat > "$UNIT" <<'UNIT_EOF'
[Unit]
Description=RuiLog host log collector (Fluent Bit)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/ruilog/agent.env
ExecStart=/usr/bin/fluent-bit -c /etc/ruilog/fluent-bit.conf
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
systemctl enable ruilog-agent >/dev/null 2>&1 || true
systemctl restart ruilog-agent
sleep 2
systemctl --no-pager --lines=0 status ruilog-agent || true
echo "[ok] 已启动 ruilog-agent；日志查看：journalctl -u ruilog-agent -f"
"""


@dataclass
class PackageParams:
    """一次接入包生成的全部输入。默认值对应集群内采集器的常见形态。"""

    hostname: str
    tenant: str
    variant: str = "host"                 # host | container
    ingest_host: str = "ingest-service"
    ingest_port: int = 8000
    agent_host: str = "agent-service"
    agent_port: int = 8000
    ingest_uri: str = "/ingest/collector"
    app_name: str = ""
    cluster_id: str = "default"
    app_log_json: str = "/var/log/app/*.json"
    app_log_text: str = "/var/log/app/*.log"
    syslog_path: str = "/var/log/syslog"
    nginx_access_path: str = "/var/log/nginx/access.log"
    nginx_error_path: str = "/var/log/nginx/error.log"
    container_log_glob: str = "/var/log/containers/*.log"
    multiline: str = "ruilog-java"        # ruilog-java / ruilog-go / ruilog-python / none
    collector_key: str = "REPLACE_ME_WITH_COLLECTOR_KEY"
    collector_code: Optional[str] = None
    host_ledger: str = "skipped"           # ok / skipped / failed / not_admin
    generated_at: str = ""
    version: str = "5.2.0"
    extra_warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _sub(text: str, params: PackageParams, extra: Optional[Dict[str, str]] = None) -> str:
    repl = {
        "@@HOSTNAME@@": params.hostname,
        "@@TENANT@@": params.tenant,
        "@@GENERATED_AT@@": params.generated_at,
        "@@VERSION@@": params.version,
        "@@APP_LOG_JSON@@": params.app_log_json,
        "@@APP_LOG_TEXT@@": params.app_log_text,
        "@@SYSLOG_PATH@@": params.syslog_path,
        "@@NGINX_ACCESS_PATH@@": params.nginx_access_path,
        "@@NGINX_ERROR_PATH@@": params.nginx_error_path,
        "@@CONTAINER_LOG_GLOB@@": params.container_log_glob,
        "@@INGEST_URI@@": params.ingest_uri,
        "@@COLLECTOR_KEY@@": params.collector_key,
        "@@APP_NAME@@": params.app_name or params.hostname,
        "@@CLUSTER_ID@@": params.cluster_id,
        "@@INGEST_HOST@@": params.ingest_host,
        "@@INGEST_PORT@@": str(params.ingest_port),
        "@@AGENT_HOST@@": params.agent_host,
        "@@AGENT_PORT@@": str(params.agent_port),
        "@@MULTILINE_PARSER@@": params.multiline,
    }
    if extra:
        repl.update(extra)
    for k, v in repl.items():
        text = text.replace(k, str(v))
    return text


def render_conf(params: PackageParams) -> str:
    """按形态渲染 fluent-bit.conf。"""
    if params.variant == "container":
        multiline = ""
        if params.multiline and params.multiline != "none":
            multiline = _sub(_MULTILINE_BLOCK, params)
        return _sub(_CONTAINER_CONF, params, {"@@MULTILINE_FILTER@@": multiline})
    return _sub(_HOST_CONF, params)


def render_parsers(params: PackageParams) -> str:
    return _PARSERS_CONTAINER if params.variant == "container" else _PARSERS_HOST


def render_env(params: PackageParams) -> str:
    return _sub(_HOST_ENV, params)


def render_install_sh(params: PackageParams) -> str:
    return _INSTALL_SH


def _readme(params: PackageParams) -> str:
    is_host = params.variant == "host"
    lines = [
        "# RuiLog 采集端接入包",
        "",
        f"- 目标主机：`{params.hostname}`",
        f"- 目标租户：`{params.tenant}`",
        f"- 接入形态：{'业务主机（集群外）' if is_host else '集群内容器'}",
        f"- 生成时间：{params.generated_at}",
        "",
        "## 包内容",
        "",
        "| 文件 | 用途 |",
        "| --- | --- |",
        "| `fluent-bit.conf` | 采集主配置（心跳 + 日志 + 上报） |",
        "| `parsers.conf` | 随包下发的 parser，目标机不需要平台镜像 |",
    ]
    if is_host:
        lines += [
            "| `agent.env` | 地址与凭证（`chmod 600`，不要入库） |",
            "| `install.sh` | 安装配置并注册 systemd 服务 |",
        ]
    lines += ["| `provision.json` | 本次生成的元数据（凭证编号、台账状态） |", ""]

    if is_host:
        lines += [
            "## 安装步骤",
            "",
            "```sh",
            "# 1) 先装 Fluent Bit 二进制（用离线包里的 deb/rpm，本包不含二进制）",
            "sudo dpkg -i fluent-bit_5.2.0_*_amd64.deb   # 或 rpm -ivh",
            "",
            "# 2) 解包并安装配置",
            f"tar -xzf {package_name(params)}",
            f"cd {package_dir(params)}",
            "sudo sh install.sh",
            "",
            "# 3) 确认心跳与上报",
            "systemctl status ruilog-agent --no-pager",
            "journalctl -u ruilog-agent -n 50 --no-pager",
            "```",
            "",
            "## 必看的三件事",
            "",
        ]
    else:
        lines += [
            "## 使用步骤",
            "",
            "```sh",
            "# 把两个配置挂进采集器容器（或烤进镜像）",
            "kubectl -n <ns> create configmap ruilog-agent-conf \\",
            "  --from-file=fluent-bit.conf --from-file=parsers.conf",
            "# 需要的环境变量：COLLECTOR_KEY / NODE_NAME（downward API）",
            "#                INGEST_HOST / INGEST_PORT / CLUSTER_ID",
            "```",
            "",
            "> `NODE_NAME` 必须由 downward API 注入（`fieldRef: metadata.name` 或",
            "> `spec.nodeName`），否则 host 字段为空，台账无法归属。",
            "",
            "## 必看的三件事",
            "",
        ]

    lines += [
        f"1. **日志路径**：本包按 `{params.app_log_json}`、`{params.app_log_text}`、"
        f"`{params.syslog_path}` 生成，按现场实际路径改 `fluent-bit.conf` 或 `agent.env` 后重启。",
        "2. **凭证**："
        + (
            f"已签发 `{params.collector_code}`，明文只在本页显示一次；泄露后在平台撤销重发。"
            if params.collector_code
            else "本包未签发凭证，`agent.env` 里是 `REPLACE_ME_WITH_COLLECTOR_KEY`，必须替换后才能上报。"
        ),
        "3. **归属**：日志归属由 ingest-service 推导，顺序是 "
        "`K8s namespace > hosts 台账 > 日志文件名`；注入的 `tenant_id` 不参与最终判定。",
        "",
        "## 归属检查表",
        "",
    ]
    if params.variant == "container":
        lines += [
            "- 容器日志：文件名里的 namespace 必须在 `namespace_tenant` 表里有映射，否则整条被拒收；",
            "- 平台自身组件：host 必须在 `hosts` 台账里。",
        ]
    else:
        ledger = {
            "ok": "已自动登记到 hosts 台账",
            "failed": "台账登记失败：请管理员在「数据接入」补登记，否则日志会被拒收",
            "not_admin": "当前账号无台账维护权限，请管理员登记 host → 租户",
            "skipped": "未登记台账：日志文件名必须为 `<租户code>.log` 才能归属",
        }.get(params.host_ledger, params.host_ledger)
        lines += [
            f"- 本次台账状态：**{ledger}**；",
            "- 台账登记后，`host` 字段等于本机主机名的日志才会归属到目标租户；",
            "- 文件名兜底只认 `<租户code>.log`，其他命名会被当作租户名而拒收。",
        ]

    if params.extra_warnings:
        lines += ["", "## 告警", ""] + [f"- {w}" for w in params.extra_warnings]

    lines += [
        "",
        "## 校验",
        "",
        "```sh",
        "# 采集端能否连到平台（把地址换成你的网关）",
        f"curl -s -o /dev/null -w '%{{http_code}}\\n' -X POST {params.ingest_host}:{params.ingest_port}{params.ingest_uri} \\",
        "  -H 'Content-Type: application/json' \\",
        f"  -H 'X-Collector-Key: {params.collector_key}' \\",
        "  -d '[]'",
        "# 期望 200；401 = 凭证无效；500 = 只可能发生在把中文占位符直接当 header 用时",
        "```",
        "",
        "上报成功后在「Agent 管理」能看到该主机心跳；在「数据源 / 检索」能看到日志。",
        "",
    ]
    return "\n".join(lines)


def _provision(params: PackageParams) -> str:
    return json.dumps({
        "hostname": params.hostname,
        "tenant": params.tenant,
        "variant": params.variant,
        "credentialCode": params.collector_code,
        "credentialIssued": bool(params.collector_code),
        "hostLedger": params.host_ledger,
        "ingest": f"{params.ingest_host}:{params.ingest_port}{params.ingest_uri}",
        "heartbeat": f"{params.agent_host}:{params.agent_port}/heartbeat",
        "generatedAt": params.generated_at,
        "warnings": params.extra_warnings,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------
def safe_name(value: str) -> str:
    """文件名只允许 ASCII 安全字符，避免 Content-Disposition / tar 头踩坑。"""
    keep = [c if (c.isalnum() or c in "-._") else "-" for c in value]
    out = "".join(keep).strip("-._")
    return out[:60] or "host"


def package_dir(params: PackageParams) -> str:
    return f"ruilog-agent-{safe_name(params.hostname)}-{params.variant}"


def package_name(params: PackageParams) -> str:
    return f"{package_dir(params)}.tar.gz"


def build_package(params: PackageParams) -> bytes:
    """生成 tar.gz。文件顺序固定，便于校验与人工检查。"""
    base = package_dir(params)
    files: Dict[str, bytes] = {
        "fluent-bit.conf": render_conf(params).encode("utf-8"),
        "parsers.conf": render_parsers(params).encode("utf-8"),
        "README.md": _readme(params).encode("utf-8"),
        "provision.json": _provision(params).encode("utf-8"),
    }
    if params.variant == "host":
        files["agent.env"] = render_env(params).encode("utf-8")
        files["install.sh"] = render_install_sh(params).encode("utf-8")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, content in files.items():
            info = tarfile.TarInfo(name=f"{base}/{rel}")
            info.size = len(content)
            info.mtime = 0                       # 固定 mtime，包可复现
            info.mode = 0o600 if rel == "agent.env" else (
                0o755 if rel == "install.sh" else 0o644)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()
