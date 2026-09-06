{
  lib,
  python3Packages,
  ruff,
}:
let
  project = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project;
in
python3Packages.buildPythonApplication {
  pname = project.name;
  inherit (project) version;
  pyproject = true;
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./roomcast
      ./tests
      ./LICENSE
    ];
  };
  build-system = [ python3Packages.setuptools ];
  dependencies = with python3Packages; [
    aiohttp
    playwright
    mcp
  ];
  pythonImportsCheck = [
    "roomcast.server"
    "roomcast.cli"
  ];
  nativeCheckInputs = [ ruff ];
  checkPhase = ''
    runHook preCheck
    ruff check roomcast tests
    ruff format --check roomcast tests
    python -m unittest discover -s tests -v
    runHook postCheck
  '';
  meta = {
    inherit (project) description;
    mainProgram = "roomcast";
    platforms = lib.platforms.linux;
    license = lib.licenses.gpl3Only;
  };
}
