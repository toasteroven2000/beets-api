# syntax=docker/dockerfile:1
# Minimal image: distroless Python (no shell, no pip) plus beets and the API.
# Versions match devenv.nix, which the tests run against.

# Must match the Python in the final stage (Debian 13 ships 3.13).
FROM python:3.13-slim-trixie AS build
# Plugin code ships with beets; extras add the libraries some plugins need.
ARG BEETS_EXTRAS=fetchart,embedart,web,lastgenre,discogs
RUN pip install --no-cache-dir --target /deps \
      "beets[${BEETS_EXTRAS}]==2.13.1" \
      fastapi==0.141.1 \
      uvicorn==0.51.0 \
      pydantic==2.13.4
COPY beets_api /deps/beets_api

FROM gcr.io/distroless/python3-debian13:nonroot
COPY --from=build /deps /deps
ENV PYTHONPATH=/deps \
    PYTHONUNBUFFERED=1 \
    BEETSDIR=/config
VOLUME /config
EXPOSE 8338
ENTRYPOINT ["python3", "-m", "uvicorn", "--factory", "beets_api.app:create_app", \
            "--host", "0.0.0.0", "--port", "8338"]
