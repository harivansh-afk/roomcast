{
  description = "Roomcast: local Roku playback";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/3e41b24abd260e8f71dbe2f5737d24122f972158";
  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "aarch64-linux"
        "x86_64-linux"
      ];
      each = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = each (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.callPackage ./package.nix { };
          roku-player = pkgs.callPackage ./roku/package.nix { };
        }
      );
      checks = each (system: {
        package = self.packages.${system}.default;
        roku-player = self.packages.${system}.roku-player;
      });
      nixosModules.default = import ./module.nix;
    };
}
