{
  # Packaging for the NixOS host that runs curator (git.8bitcloud.com/bruce/homelab,
  # hosts/curator), which pins this repo as a flake input. uv2nix builds the
  # server from uv.lock (binary wheels only, as `uv sync` installs them), so a
  # lock change is a dependency change there too. The Dockerfile stays for
  # anyone running it as a container.
  description = "curator: a self-hosted, AI-curated news reader";

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
      # The same, with the test dependencies (`uv sync --extra server`, which
      # adds the dev group).
      devVenv = pythonSet.mkVirtualEnv "curator-dev-env" {
        curator = [
          "server"
          "dev"
        ];
      };

      # The app's end-to-end test (web/test/e2e.mjs): serves web/ and runs it
      # against the browsers nixpkgs' Playwright was built for.
      e2e = pkgs.writeShellApplication {
        name = "curator-e2e";
        runtimeInputs = [
          pkgs.git
          pkgs.nodejs
          pkgs.http-server
        ];
        text = ''
          cd "$(git rev-parse --show-toplevel)/web"
          http-server -p 8124 -c-1 -s . &
          server=$!
          trap 'kill "$server"' EXIT
          until node -e "fetch('http://localhost:8124/').catch(() => process.exit(1))"; do sleep 0.2; done
          APP_URL=http://localhost:8124/ node test/e2e.mjs
        '';
      };
    in
    {
      packages.${system}.default = venv;
      # `curator` runs the service; `curator-refresh-profile` is its CLI.
      apps.${system}.default = {
        type = "app";
        program = "${venv}/bin/curator";
      };

      # `nix develop -c python -m pytest -q` and `nix develop -c curator-e2e`.
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          devVenv
          pkgs.git
          pkgs.nodejs
          pkgs.http-server
          e2e
        ];
        # Playwright's node package and its browsers, from one nixpkgs
        # release, so they match (npm's would want browsers of its own).
        PLAYWRIGHT_BROWSERS_PATH = pkgs.playwright-driver.browsers;
        PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
        # The shell's service-worker test builds the shell with this Python.
        CURATOR_PYTHON = "${devVenv}/bin/python";
        shellHook = ''
          # The checkout's code, not the copy in the venv.
          root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
          export PYTHONPATH="$root:$root/agent''${PYTHONPATH:+:$PYTHONPATH}"
          # e2e.mjs imports `playwright`, found from web/test/node_modules;
          # left alone if npm put one there.
          if [ -d "$root/web/test" ] && { [ -L "$root/web/test/node_modules" ] || [ ! -e "$root/web/test/node_modules" ]; }; then
            ln -sfn ${pkgs.playwright-test}/lib/node_modules "$root/web/test/node_modules"
          fi
        '';
      };
    };
}
