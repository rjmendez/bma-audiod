# syntax=docker/dockerfile:1
# Test / contracts image: the CPU analyzer image (same Python, same pinned packages) + jsonschema + the whole source tree.
# Build the CPU analyzer image first (docker compose --profile cpu build analyzer-cpu), then:
#   docker build -f docker/test.Dockerfile -t bma-test .
#   docker run --rm bma-test            # every suite, in order; a failing suite does not hide the others
#   docker run --rm bma-test contracts  # one suite: analysis classify pc ui tools contracts docker registry
ARG ANALYZER_IMAGE=bma-analyzer:cpu
FROM ${ANALYZER_IMAGE}
USER root
COPY docker/requirements-test.txt /tmp/requirements-test.txt
RUN pip install -r /tmp/requirements-test.txt && rm /tmp/requirements-test.txt
WORKDIR /src
COPY . /src/
RUN chmod 0555 /src/docker/run-tests.sh
# tests write only under TMPDIR; the source tree stays read-only for the unprivileged user
USER bma
ENV BMA_DATA_DIR=/tmp/bma-test-data
ENTRYPOINT ["/src/docker/run-tests.sh"]
CMD ["all"]
