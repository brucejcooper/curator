{
  # Packaging for the NixOS host that runs curator (git.8bitcloud.com/bruce/homelab,
  # hosts/curator), which pins this repo as a flake input. uv2nix builds the
  # server from uv.lock (binary wheels only, as `uv sync` installs them), so a
  # lock change is a dependency change there too. The Dockerfile stays for
  # anyone running it as a container.
  description = "curator: AI-curated news feed for Mastorss";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

      pythonSet =
        (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python313; }).overrideScope
          (
            nixpkgs.lib.composeManyExtensions [
              pyproject-build-systems.overlays.wheel
              overlay
              (final: prev: {
                # The app finds its web files next to the package
                # (server.py: WEB = parent.parent / "web"); the wheel has only
                # the Python packages, so put them alongside.
                curator = prev.curator.overrideAttrs (old: {
                  postInstall = (old.postInstall or "") + ''
                    cp -r ${./web} $out/${final.python.sitePackages}/web
                  '';
                });
                # Binary wheels link against these; autoPatchelf needs them to
                # find the libraries (the Docker image has them from Debian).
                onnxruntime = prev.onnxruntime.overrideAttrs (old: {
                  buildInputs = (old.buildInputs or [ ]) ++ [ pkgs.stdenv.cc.cc.lib ];
                });
              })
            ]
          );

      # The server and everything it imports (`uv sync --extra server`).
      venv = pythonSet.mkVirtualEnv "curator-env" { curator = [ "server" ]; };
    in
    {
      packages.${system}.default = venv;
      # `curator` runs the service; `curator-refresh-profile` is its CLI.
      apps.${system}.default = {
        type = "app";
        program = "${venv}/bin/curator";
      };
    };
}
