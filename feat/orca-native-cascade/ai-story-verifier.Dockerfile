FROM rust:1.100.0-bookworm@sha256:ade39a710ef85f3506cffcfec3e8a24ba090badef1dff2fb798f71d878833fea

COPY --chmod=0555 codex codex-code-mode-host /usr/local/bin/
RUN rustup component add --toolchain 1.100.0 rustfmt
RUN printf '%s\n' '#!/bin/sh' 'exec /usr/bin/python3 -m team_runner "$@"' \
    > /usr/local/bin/team-runner && chmod 0555 /usr/local/bin/team-runner \
    && for tool in cargo rustc rustdoc rustfmt cargo-fmt rustup; do \
        ln -s "/usr/local/cargo/bin/$tool" "/usr/local/bin/$tool"; \
    done
