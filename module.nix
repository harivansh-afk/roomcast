{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.roomcast;
  settings = pkgs.writeText "roomcast.json" (
    builtins.toJSON {
      roku_ip = cfg.rokuAddress;
      roku_serial = cfg.rokuSerial;
      public_base = "http://${cfg.lanAddress}:${toString cfg.port}";
      chromium = "${pkgs.chromium}/bin/chromium";
      ffmpeg = "${pkgs.ffmpeg-headless}/bin/ffmpeg";
      max_height = cfg.maxHeight;
    }
  );
  rule = "-s ${cfg.rokuAddress} -d ${cfg.lanAddress} -p tcp --dport ${toString cfg.port} -j ACCEPT";
in
{
  options.services.roomcast = {
    enable = lib.mkEnableOption "Roomcast Roku playback";
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
    };
    rokuAddress = lib.mkOption { type = lib.types.str; };
    rokuSerial = lib.mkOption { type = lib.types.str; };
    lanAddress = lib.mkOption { type = lib.types.str; };
    port = lib.mkOption {
      type = lib.types.port;
      default = 18795;
    };
    maxHeight = lib.mkOption {
      type = lib.types.enum [
        720
        1080
      ];
      default = 1080;
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion =
          builtins.match "[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+" cfg.rokuAddress != null
          && builtins.match "[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+" cfg.lanAddress != null;
        message = "Roomcast requires IPv4 LAN addresses";
      }
    ];
    users.groups.roomcast = { };
    users.users.roomcast = {
      isSystemUser = true;
      group = "roomcast";
    };
    environment.systemPackages = [ cfg.package ];
    systemd.services.roomcast = {
      description = "Roomcast playback relay";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment = {
        HOME = "/var/lib/roomcast";
        XDG_CACHE_HOME = "/var/cache/roomcast";
        SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
        PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1";
      };
      serviceConfig = {
        ExecStart = "${cfg.package}/bin/roomcast-service --config ${settings}";
        User = "roomcast";
        Group = "roomcast";
        RuntimeDirectory = "roomcast";
        RuntimeDirectoryMode = "0750";
        StateDirectory = "roomcast";
        CacheDirectory = "roomcast";
        UMask = "0007";
        Restart = "on-failure";
        RestartSec = 3;
        TimeoutStopSec = 15;
        MemoryMax = "2G";
        TasksMax = 256;
        CPUQuota = "200%";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        CapabilityBoundingSet = "";
        RestrictAddressFamilies = [
          "AF_UNIX"
          "AF_INET"
          "AF_INET6"
          "AF_NETLINK"
        ];
      };
    };
    systemd.sockets.roomcast-media = {
      description = "TV-only Roomcast media listener";
      wantedBy = [ "sockets.target" ];
      listenStreams = [ "${cfg.lanAddress}:${toString cfg.port}" ];
      socketConfig = {
        FreeBind = true;
        NoDelay = true;
      };
    };
    systemd.services.roomcast-media = {
      requires = [ "roomcast.service" ];
      after = [ "roomcast.service" ];
      serviceConfig = {
        ExecStart = "${pkgs.systemd}/lib/systemd/systemd-socket-proxyd 127.0.0.1:18796";
        DynamicUser = true;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
      };
    };
    networking.firewall.extraCommands = "iptables -I nixos-fw 1 ${rule}";
    networking.firewall.extraStopCommands = "iptables -D nixos-fw ${rule} 2>/dev/null || true";
  };
}
