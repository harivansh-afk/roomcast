{ lib, python3Packages }:
python3Packages.buildPythonApplication {
  pname = "roomcast";
  version = "0.1.0";
  pyproject = true;
  src = lib.cleanSource ./.;
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
  checkPhase = ''
    runHook preCheck
    python -m unittest discover -s tests -v
    runHook postCheck
  '';
  meta = {
    description = "Local Roku streaming relay and constrained playback API";
    mainProgram = "roomcast";
    platforms = lib.platforms.linux;
    license = lib.licenses.gpl3Only;
  };
}
