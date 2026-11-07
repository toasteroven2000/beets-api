{ pkgs, lib, config, inputs, ... }:

{
  packages = [
    pkgs.beets
    (pkgs.callPackage ./dev/fake-beets-library { })
  ];

  languages.python = {
    enable = true;
    # beets comes from nixpkgs so the API imports the same version as the `beet` CLI.
    package = pkgs.python3.withPackages (ps: [
      ps.beets
      ps.fastapi
      ps.uvicorn
      ps.pydantic
      ps.pytest
      ps.httpx
      ps.mutagen
    ]);
  };

  env.BEETS_WEB_URL = "http://127.0.0.1:8337";
  # Keeps development away from the default library in ~/.config/beets.
  env.BEETSDIR = "${config.devenv.root}/db/foobar";

  # Extra arguments go to uvicorn, e.g. `api --reload` or `api --port 9000`.
  scripts.api.exec = ''
    exec python -m uvicorn --factory beets_api.app:create_app \
      --app-dir "$DEVENV_ROOT" --host 127.0.0.1 --port 8338 "$@"
  '';
  processes.api.exec = "api";

  cachix.enable = false;
}
