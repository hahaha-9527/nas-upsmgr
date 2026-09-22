# -*- coding: utf-8 -*-
# upsmgr（电源管理）镜像：python:3.12-slim + 构建期装好 NUT
# 与应用中心 tpk 版同源（app/run.sh、upsmgr.py、panel.html），
# 区别：NUT 在构建期装好，首次启动免 apt（run.sh 里有 command -v upsc 检查，装过就跳过）。
#
# 国内构建慢可换源：
#   docker build --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn -t upsmon:0.1.3 .
FROM python:3.12-slim-bookworm

ARG APT_MIRROR=deb.debian.org

RUN if [ "$APT_MIRROR" != "deb.debian.org" ]; then \
      sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null \
        || sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" /etc/apt/sources.list; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends nut-server nut-client procps \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app
COPY app/ /opt/app/

# 端口由 /data/upsmgr.json 的 "port" 决定（默认 9750）；host 网络下 EXPOSE 仅作说明
EXPOSE 9750

CMD ["sh", "/opt/app/run.sh"]
