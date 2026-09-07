{
  lib,
  stdenvNoCC,
  fetchFromGitHub,
  zip,
  unzip,
}:
stdenvNoCC.mkDerivation {
  pname = "roomcast-roku-player";
  version = "1.4.0";
  src = fetchFromGitHub {
    owner = "MedievalApple";
    repo = "Media-Assistant";
    rev = "1335dd41aad175ad7494f01f7e2c21a582d06179";
    hash = "sha256-gDxVvX6EoozV7GncM55kste3Q2XLtCO2WmbE95obPXE=";
  };
  postPatch = ''
    rm -rf source components
    cp -r ${./source} source
    cp -r ${./components} components
    cp ${./manifest} manifest
    cp ${../LICENSE} ROOMCAST-LICENSE
    mkdir branding
    cp images/{fhd_poster,hd_poster,fhd_splash,hd_splash}.png branding/
    rm -rf images
    mv branding images
  '';
  nativeBuildInputs = [ zip ];
  nativeCheckInputs = [ unzip ];
  buildPhase = ''
    runHook preBuild
    zip -qr roomcast-player.zip manifest source components images LICENSE ROOMCAST-LICENSE
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
    description = "Roomcast native Roku video player";
    homepage = "https://git.harivan.sh/harivansh-afk/roomcast";
    license = [
      lib.licenses.asl20
      lib.licenses.gpl3Only
    ];
    platforms = lib.platforms.linux;
  };
}
