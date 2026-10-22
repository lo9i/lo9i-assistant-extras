# The factory's agent image (lo9i's factory plugin, sandbox.py): the coding agents and the usual tools, so
# the agent can build and test most repositories. It installs what else it needs: the container has the
# network. Only the task's worktree is mounted, at /work.
FROM node:22-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git python3 python3-pip python3-venv build-essential curl ca-certificates ripgrep jq \
    && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
RUN npm install -g @anthropic-ai/claude-code @openai/codex && npm cache clean --force
# The container runs as the user's uid, which has no home of its own: /tmp is one, writable by anyone.
ENV HOME=/tmp
WORKDIR /work
