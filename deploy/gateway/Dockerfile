# Hand-rolled IB Gateway + IBC, headless. No third-party image — every layer is here
# to audit. IB Gateway is a Java Swing app, so it needs a virtual X display (Xvfb);
# IBC (IbcAlpha/IBC) automates the login and the daily auto-restart re-login; socat
# relays the API off localhost so the sibling `app` container can reach it without
# loosening Gateway's own localhost-only API binding.
#
# ⚠️ UNVERIFIED IN CI: this image cannot be built/run where it was authored (no Docker,
# no network to IBKR, no creds). Treat the version ARGs and the installer/IBC flags as
# the things to confirm on first `docker compose build` — see notes/deployment.md.
FROM debian:bookworm-slim

# Pin these; bump deliberately (a Gateway/IBC upgrade is a deploy, like a model bump).
# Find the current IBC release at https://github.com/IbcAlpha/IBC/releases
ARG IBC_VERSION=3.21.2
ARG IBC_URL=https://github.com/IbcAlpha/IBC/releases/download/${IBC_VERSION}/IBCLinux-${IBC_VERSION}.zip
# IBKR's "standalone" Gateway bundles its own JRE (no separate Java install needed).
ARG IBGW_URL=https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh

ENV DEBIAN_FRONTEND=noninteractive
# xvfb: virtual display. socat: API relay. gettext-base: envsubst for the config.
# libx*/libgtk: Swing/AWT runtime deps the bundled JRE needs even headless.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates unzip socat xvfb gettext-base procps \
        libxtst6 libxrender1 libxi6 libxext6 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

# --- IB Gateway (unattended install to /opt/ibgateway) ---
# VERIFY: the standalone installer is an InstallBuilder script; `-q -dir` is the usual
# unattended form. If the build hangs or ignores the dir, try `--mode unattended
# --prefix /opt/ibgateway` instead.
RUN curl -fsSL "$IBGW_URL" -o /tmp/ibgw.sh \
    && chmod +x /tmp/ibgw.sh \
    && /tmp/ibgw.sh -q -dir /opt/ibgateway \
    && rm /tmp/ibgw.sh

# --- IBC ---
RUN curl -fsSL "$IBC_URL" -o /tmp/ibc.zip \
    && mkdir -p /opt/ibc \
    && unzip -q /tmp/ibc.zip -d /opt/ibc \
    && chmod +x /opt/ibc/*.sh /opt/ibc/scripts/*.sh \
    && rm /tmp/ibc.zip

# --- Doppler CLI (runtime secret injection; see ENTRYPOINT) ---
# VERIFY: the install script drops the binary at /usr/local/bin/doppler.
RUN curl -Ls https://cli.doppler.com/install.sh | sh

# --- Lay the install out the way IBC expects ---
# The standalone installer flattens everything into /opt/ibgateway, but IBC looks for the
# jars at ${TWS_PATH}/ibgateway/<version>/jars. Detect the version from the jar name, symlink
# the expected path to our flat install, and record the version for the entrypoint. Detected
# (not hardcoded) so a `stable` channel bump just works on the next rebuild.
RUN VER="$(ls /opt/ibgateway/jars/jts4launch-*.jar | sed -E 's|.*/jts4launch-([0-9]+)\.jar|\1|')" \
    && mkdir -p /opt/Jts/ibgateway \
    && ln -s /opt/ibgateway "/opt/Jts/ibgateway/${VER}" \
    && echo "${VER}" > /opt/ibc/.twsversion \
    && echo "IBC layout: TWS_PATH=/opt/Jts, version=${VER}"

COPY config.ini.tmpl /opt/ibc/config.ini.tmpl
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# The API relay port the `app` container connects to (see entrypoint: socat
# 0.0.0.0:4004 -> 127.0.0.1:4002). Never publish this to the public internet.
EXPOSE 4004

# Liveness: the relay port must be accepting connections (Gateway up + logged in).
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD socat -u OPEN:/dev/null TCP:127.0.0.1:4004 2>/dev/null || exit 1

# Wrap in `doppler run` so TWS creds are fetched from Doppler at runtime and injected as
# env (consumed by entrypoint.sh's envsubst) — never stored on the project disk. Needs
# DOPPLER_TOKEN in the env (passed by compose). Doppler caches an encrypted fallback so a
# transient Doppler-API outage at restart doesn't block the Gateway coming up.
ENTRYPOINT ["doppler", "run", "--", "/usr/local/bin/entrypoint.sh"]
