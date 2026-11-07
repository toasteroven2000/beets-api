{ stdenv
, lib
, makeWrapper
, python3
, ffmpeg
, git
}:

let
  pythonEnv = python3.withPackages (ps: [
    ps.mutagen
    ps.musicbrainzngs
    ps.pyyaml
    ps.beets
  ]);
in
stdenv.mkDerivation {
  pname = "fake-beets-library";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = ./fake_beets_library.py;
  };

  dontBuild = true;
  dontConfigure = true;

  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    mkdir -p $out/bin $out/libexec
    cp fake_beets_library.py $out/libexec/fake_beets_library.py

    makeWrapper ${pythonEnv}/bin/python3 $out/bin/fake-beets-library \
      --add-flags "$out/libexec/fake_beets_library.py" \
      --prefix PATH : ${lib.makeBinPath [ ffmpeg git pythonEnv ]}
  '';

  meta = {
    description = "Generate a fake beets library from real MusicBrainz release metadata";
    mainProgram = "fake-beets-library";
  };
}
