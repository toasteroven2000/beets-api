# Minimal image: distroless Python (no shell, no pip) plus beets and the API.
# Pins are the versions devenv.nix uses; update both together.

# Must match the Python in the final stage (Debian 13 ships 3.13).
FROM python:3.13-slim-trixie AS build
# Plugin code ships with beets; extras add the libraries some plugins need.
RUN pip install --no-cache-dir --target /deps \
      "beets[fetchart,embedart,lastgenre]==2.13.1" \
      fastapi==0.141.1 \
      uvicorn==0.51.0 \
      pydantic==2.13.4 \
 # Console scripts can't run without a shell, and test suites are never imported.
 && rm -rf /deps/bin \
 && find /deps -type d -name tests -prune -exec rm -rf {} +
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
