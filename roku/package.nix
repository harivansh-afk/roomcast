{
  lib,
  stdenvNoCC,
  fetchFromGitHub,
  zip,
  unzip,
}:
stdenvNoCC.mkDerivation {
  pname = "roomcast-roku-player";
  version = "1.3.5";
  src = fetchFromGitHub {
    owner = "MedievalApple";
    repo = "Media-Assistant";
    rev = "1335dd41aad175ad7494f01f7e2c21a582d06179";
    hash = "sha256-gDxVvX6EoozV7GncM55kste3Q2XLtCO2WmbE95obPXE=";
  };
  patches = [
    ./timestamp-seeking.patch
    ./subtitle-controls.patch
  ];
  postPatch = ''
    cp ${../LICENSE} ROOMCAST-LICENSE
    cp ${./components}/* components/
  '';
  nativeBuildInputs = [ zip ];
  nativeCheckInputs = [ unzip ];
  buildPhase = ''
    runHook preBuild
    zip -qr roomcast-player.zip manifest source components images locale LICENSE ROOMCAST-LICENSE
    runHook postBuild
  '';
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    unzip -t roomcast-player.zip
    runHook postCheck
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p $out
    cp roomcast-player.zip $out/
    runHook postInstall
  '';
  meta = {
    description = "Optional Media Assistant build with Roomcast timestamp controls";
    homepage = "https://github.com/MedievalApple/Media-Assistant";
    license = [
      lib.licenses.asl20
      lib.licenses.gpl3Only
    ];
    platforms = lib.platforms.linux;
  };
}
